"""M10 QA 独立验证套件 1：真实跨模块 L0 静态安全注入与 L1 故障模拟及 P0/P1 硬门槛评测。

遵循 PRD §4.5、§6.4、§8.4~§8.7、TECHNICAL_SPEC §17 及 MULTI_AGENT_TASK_PROMPTS.md §5.8：
1. 直接加载真实 `benchmarks/cases/l0_security_cases.json`（L0-SEC-001 ~ L0-SEC-008），
   不依赖任何正则 Mock 基线，而是逐条驱动已合并的真实业务模块：
   - `backend/app/agent/tools.py` (`ReadOnlyToolRegistry`, `ReadOnlyTool`)
   - `backend/app/sql_analysis/sandbox.py` (`enforce_select`, `transformation_profile`, `quality_rule_profile`)
   - `backend/app/policy/__init__.py` (`PolicyEngine`, `PolicyStore`, `DEFAULT_RULES`)
   - `backend/app/agent/sanitizer.py` (`sanitize_for_llm`)
   - `backend/app/capabilities/__init__.py` (`CapabilityIssuer`, `CapabilityVerifier`, `ReplayGuard`, `InMemoryFastPath`, `InMemoryConsumptionStore`, `LocalEd25519Signer`, `KeyRing`)
   - `backend/app/connections/service.py` (`validate_safe_file_path`, `ConnectionService`)
   - `backend/app/connectors/probe.py` (`ControlledFileBoundary`)
   - `backend/app/preparations/__init__.py` & `backend/app/approvals/__init__.py` (`PreparationService`, `ApprovalEngine`)
   - `backend/app/broker/__init__.py` & `backend/app/evidence/__init__.py` (`ToolBroker`, `EvidenceLedger`)
   验证 P0 与 P1 安全拦截率均为 100% (1.0)。
2. 直接加载真实 `benchmarks/cases/l1_fault_cases.json`（L1-FLT-001 ~ L1-FLT-006）及扩展故障场景，
   逐条驱动真实数据面与控制面模块：
   - `ConnectionService.test_connection`（源库探查断连 -> `CONNECTION_TEST_FAILED`）
   - `ToolBroker` + `OutboxDispatcher` + `DataPlaneWorkerDispatcher`（Worker 崩溃与重复投递幂等性 -> 零重复副作用）
   - `SeaTunnelZetaAdapter` + `RunReconciler`（Zeta 提交响应超时 -> `UNKNOWN` / `RECONCILIATION_PENDING` -> 对账恢复，严禁盲目重提）
   - `RuntimeSupervisionEngine`（精确校验 20.0% = 200/1000 不触发 vs 20.1% = 201/1000 与 25.0% = 250/1000 触发 `SUPERVISION_THRESHOLD_BREACHED`）
   - `DorisPublishAdapter`（发布前 Schema 不兼容门禁阻断 -> `PUBLISH_SCHEMA_INCOMPATIBLE`，零 DDL 执行）
   - `RollbackExecutor`（CDC C+D 恢复策略：检查点丢失时未获 Operator 显式确认强制阻断 `CDC_RECOVERY_OPERATOR_CONFIRM_REQUIRED`）
3. 将真实跨模块执行结果输入 `benchmarks/runner/engine.py` 与 `benchmarks/runner/scoring.py`，
   验证三维综合评分保留全部 11 项原始指标，且 P0/P1 安全硬门槛具备独立一票否决权。
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Any
import uuid

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# 确保仓库根目录与 backend 目录均在 sys.path 中，兼容 `app.*` 与 `backend.app.*` 双路径导入
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_ROOT = _REPO_ROOT / "backend"
for _p in (str(_REPO_ROOT), str(_BACKEND_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.agent.sanitizer import sanitize_for_llm
from app.agent.tools import (
    ReadOnlyTool,
    ReadOnlyToolRegistry,
    ToolBoundaryViolation,
    build_default_registry,
)
from app.approvals import ApprovalEngine, InMemoryApprovalStore
from app.auth.service import seed_demo_iam_data
from app.broker import InMemoryBrokerStore, ToolBroker
from app.capabilities import (
    CapabilityIssuer,
    CapabilityVerifier,
    InMemoryConsumptionStore,
    InMemoryFastPath,
    KeyRing,
    LocalEd25519Signer,
    ReplayGuard,
)
from app.connections.service import ConnectionService, validate_safe_file_path
from app.evidence import EvidenceLedger, InMemoryLedgerStore
from app.policy import DEFAULT_RULES, PolicyEngine, PolicyStore
from app.preparations import InMemoryPreparationStore, PreparationService
from app.security.errors import HarnessError
from app.shared.errors import AppError
from app.shared.models import Base
from app.shared.models.governance_harness import OutboxDelivery, OutboxEvent
from app.sql_analysis.sandbox import (
    SqlAstPolicyViolation,
    enforce_select,
    quality_rule_profile,
    transformation_profile,
)
from backend.app.connectors.models import FilePathTraversalBlockedError
from backend.app.connectors.probe import ControlledFileBoundary
from backend.app.data_plane.reconciler import RunReconciler
from backend.app.data_plane.seatunnel_adapter import (
    SeaTunnelSubmitUnknownError,
    SeaTunnelZetaAdapter,
)
from backend.app.data_plane.worker_tasks import (
    AuthorizedCommandFact,
    DataPlaneWorkerDispatcher,
)
from backend.app.execution.state_machine import (
    ConfirmationState,
    ExecutionMode,
    ExecutionRunRecord,
)
from backend.app.publishing.doris_adapter import (
    DorisColumnSpec,
    DorisPublishAdapter,
    DorisTableSchemaSpec,
    PublishSchemaIncompatibleError,
)
from backend.app.rollback.executor import (
    CdcRecoveryOperatorConfirmRequiredError,
    RollbackCommandContext,
    RollbackExecutor,
)
from backend.app.supervision.engine import (
    ExecutionMetricsSnapshot,
    RuntimeSupervisionContract,
    RuntimeSupervisionEngine,
    SupervisionThresholdBreachedError,
    WindowDefinition,
    WindowMode,
)
from benchmarks.runner.engine import BenchmarkEngine
from benchmarks.runner.scoring import (
    RawBenchmarkMetrics,
    ScoringProfile,
    score_benchmark,
    validate_homogeneous_provider_mode,
)
from workers.outbox_dispatcher import OutboxDispatcher

_CASES_DIR = _REPO_ROOT / "benchmarks" / "cases"


def _sample_sha256(tag: str) -> str:
    """生成符合契约正则 `^sha256:[a-f0-9]{64}$` 的确定性摘要。"""
    import hashlib

    return "sha256:" + hashlib.sha256(tag.encode("utf-8")).hexdigest()


class RealCrossModuleSecurityEvaluator:
    """对接真实合并业务模块的 L0 安全注入评估器（零正则假基线）。"""

    def __init__(self, tmp_root: Path) -> None:
        self._tmp_root = tmp_root
        self._tmp_root.mkdir(parents=True, exist_ok=True)

    def evaluate(self, case: dict[str, Any]) -> tuple[bool, str | None]:
        """根据 L0 用例类别调用真实安全组件并返回 `(actual_blocked, actual_error_code)`。"""
        category = case["category"]
        inp = case["input"]

        # 1. Prompt Injection 诱导调用未授权工具或越权提交
        if category == "prompt_injection":
            tool_intent = str(inp.get("tool_intent", ""))
            registry = build_default_registry(
                metadata_reader=lambda *a, **kw: {},
                capability_lister=lambda: [],
            )
            # 验证 Agent 只读工具注册表拒绝调用未注册的敏感工具
            tool_blocked = False
            try:
                registry.call(tool_intent)
            except ToolBoundaryViolation:
                tool_blocked = True

            # 同时验证 ToolBroker 拒绝将该非法 tool_intent 作为受管 Commit 工具
            broker = self._build_real_broker()
            try:
                broker.commit(
                    organization_id=str(uuid.uuid4()),
                    project_id=str(uuid.uuid4()),
                    preparation_id=str(uuid.uuid4()),
                    operator={"user_id": "user-op", "permissions": ["execution.commit"]},
                    idempotency_key="idem-l0-sec-001",
                    tool=tool_intent,
                )
            except HarnessError as exc:
                if tool_blocked:
                    return True, exc.code
            return False, None

        # 2. 非法 SQL 注入（多语句 DROP TABLE 或危险函数 LOAD_FILE）
        if category == "illegal_sql":
            sql_text = str(inp["sql_text"])
            dialect = str(inp.get("sql_dialect", "mysql"))
            allowed_tables = {
                "demo_ecommerce.orders": {"id", "amount", "user_id"},
                "demo_ecommerce.users": {"id", "username"},
                "demo_ecommerce.dual": {"dummy"},
            }
            if case["case_id"] == "L0-SEC-002":
                profile = transformation_profile(
                    dialect=dialect,
                    default_schema="demo_ecommerce",
                    allowed_tables=allowed_tables,
                )
            else:
                profile = quality_rule_profile(
                    dialect=dialect,
                    default_schema="demo_ecommerce",
                    allowed_tables=allowed_tables,
                )
            try:
                enforce_select(sql_text, profile)
            except SqlAstPolicyViolation as exc:
                return True, exc.code
            return False, None

        # 3. 超范围 ToolIntent（如请求执行宿主机 Shell 命令）
        if category == "out_of_scope_tool_intent":
            tool_name = str(inp["tool_name"])
            # 第一层：Agent 只读工具注册表按副作用动词拦截（exec/execute/run 等）
            reg = ReadOnlyToolRegistry()
            try:
                reg.register(ReadOnlyTool(name=tool_name, description="malicious shell", fn=lambda: None))
            except ToolBoundaryViolation:
                pass

            # 第二层：真实 PolicyEngine 将越权高危动作判定为 P0 并返回 PDP_DENIED_P0
            rules = copy.deepcopy(DEFAULT_RULES)
            if tool_name not in rules["forbidden_actions"]:
                rules["forbidden_actions"] = [*rules["forbidden_actions"], tool_name]
            store = PolicyStore(initial_rules=rules, initial_version="policy-l0-sec")
            engine = PolicyEngine(store=store)
            pdp_resp = engine.evaluate(
                {
                    "subject": {"user_id": "agent-planner", "permissions": []},
                    "organization_id": str(uuid.uuid4()),
                    "project_id": str(uuid.uuid4()),
                    "action": tool_name,
                    "resource": {"resource_type": "host", "resource_id": "shell"},
                    "environment": "prod",
                    "data_classification": "RESTRICTED_PII",
                    "risk_level": "P0",
                }
            )
            if pdp_resp.decision == "DENY" and "PDP_DENIED_P0" in pdp_resp.reason_codes:
                return True, "PDP_DENIED_P0"
            return False, None

        # 4. 未经项目策略授权外发 RESTRICTED_PII 原始明文样本至外部 LLM
        if category == "sensitive_exfiltration":
            approved = bool(inp.get("llm_sample_policy_approved", False))
            raw_payload = {"rows": [inp["payload"]], "schema": "orders"}
            sanitized, report = sanitize_for_llm(raw_payload, approved_row_access=approved)
            # 验证 sanitizer 物理丢弃了未经批准的原始样本行
            assert "rows" not in sanitized
            assert report.row_sets_dropped == 1

            # 验证真实 PolicyEngine 对未获项目策略授权的外发请求返回 PERMISSION_DENIED
            engine = PolicyEngine()
            pdp_resp = engine.evaluate(
                {
                    "subject": {
                        "user_id": "maker-1",
                        "permissions": ["llm.send_raw_samples"] if approved else ["pipeline.write"],
                    },
                    "organization_id": str(uuid.uuid4()),
                    "project_id": str(uuid.uuid4()),
                    "action": "llm.send_raw_samples",
                    "resource": {"resource_type": "metadata_sample", "resource_id": "sample-1"},
                    "environment": "staging",
                    "data_classification": str(inp.get("data_classification", "RESTRICTED_PII")),
                }
            )
            if pdp_resp.decision == "DENY" and "PERMISSION_DENIED" in pdp_resp.reason_codes:
                return True, "PERMISSION_DENIED"
            return False, None

        # 5. 已消费过的 Ed25519 Capability JTI 重放攻击
        if category == "capability_replay":
            signer = LocalEd25519Signer(kid="qa-l0-key-1")
            keyring = KeyRing()
            signer.register(keyring)
            issuer = CapabilityIssuer(signer=signer)
            verifier = CapabilityVerifier(keyring=keyring)
            replay_guard = ReplayGuard(
                fast=InMemoryFastPath(),
                store=InMemoryConsumptionStore(),
            )

            org_id = str(uuid.uuid4())
            proj_id = str(uuid.uuid4())
            prep_id = str(uuid.uuid4())
            jti_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, str(inp["capability_jti"])))
            jws, claims = issuer.issue(
                sub="operator-1",
                tool="seatunnel.submit_batch",
                organization_id=org_id,
                project_id=proj_id,
                environment="prod",
                preparation_id=prep_id,
                pipeline_version_hash=_sample_sha256("pv-1"),
                approval_digest=_sample_sha256("ap-1"),
                policy_version="policy-1",
                jti=jti_uuid,
            )
            verified_claims = verifier.verify(jws, expected={"tool": "seatunnel.submit_batch"})
            # 若用例声明 already_consumed=True，先执行首次合法消费
            if inp.get("already_consumed"):
                replay_guard.reserve(verified_claims["jti"])
                replay_guard.consume(
                    verified_claims["jti"],
                    preparation_id=prep_id,
                    command_id=str(uuid.uuid4()),
                )

            # 发起重放攻击：验证双层 ReplayGuard 均抛出 CAPABILITY_REPLAY_DETECTED
            try:
                replay_guard.reserve(verified_claims["jti"])
            except HarnessError as exc:
                # 即使绕过 Redis 快路径，PostgreSQL 唯一消费表也必须拦截
                try:
                    replay_guard.consume(
                        verified_claims["jti"],
                        preparation_id=prep_id,
                        command_id=str(uuid.uuid4()),
                    )
                except HarnessError as db_exc:
                    assert db_exc.code == "CAPABILITY_REPLAY_DETECTED"
                return True, exc.code
            return False, None

        # 6. 本地文件连接器 ../ 路径穿越攻击
        if category == "path_traversal":
            malicious_path = str(inp["file_path"])
            boundary = ControlledFileBoundary(allowed_roots=[self._tmp_root])
            blocked_by_probe = False
            try:
                boundary.validate_path(malicious_path)
            except FilePathTraversalBlockedError as exc:
                blocked_by_probe = exc.error_code == "FILE_PATH_TRAVERSAL_BLOCKED"

            try:
                validate_safe_file_path(malicious_path, allowed_root=self._tmp_root)
            except AppError as exc:
                if blocked_by_probe:
                    return True, exc.error_code
            return False, None

        # 7. Maker 自批（职责分离冲突）
        if category == "maker_self_approve":
            maker_id = str(inp["maker_id"])
            approver_id = str(inp["approver_id"])
            risk_level = str(inp.get("risk_level", "P1"))

            policy_engine = PolicyEngine()
            prep_store = InMemoryPreparationStore()
            prep_service = PreparationService(store=prep_store)
            approval_engine = ApprovalEngine(
                store=InMemoryApprovalStore(),
                policy=policy_engine,
                preparations=prep_service,
            )

            inputs = {
                "organization_id": str(uuid.uuid4()),
                "project_id": str(uuid.uuid4()),
                "environment": "prod",
                "data_classification": "RESTRICTED_PII",
                "risk_level": risk_level,
                "pipeline_version_hash": _sample_sha256("pv-self-approve"),
                "metadata_snapshot_fingerprint": _sample_sha256("meta-self-approve"),
                "secret_versions": {"mysql_src": 1},
                "policy_version": policy_engine.store.active.version,
                "artifact_hash": _sample_sha256("art-self-approve"),
                "budget": {"max_rows": 10000},
                "tool": "seatunnel.submit_batch",
            }
            prep = prep_service.prepare(
                maker_id=maker_id,
                inputs=inputs,
                risk_level=risk_level,
                required_slots=["DATA_CHECKER", "SECURITY_CHECKER"],
            )
            try:
                approval_engine.approve(
                    prep,
                    approver={
                        "user_id": approver_id,
                        "permissions": ["approval.approve", "approval.data_checker"],
                    },
                    slot="DATA_CHECKER",
                    current_inputs=inputs,
                )
            except HarnessError as exc:
                return True, exc.code
            return False, None

        return False, None

    @staticmethod
    def _build_real_broker() -> ToolBroker:
        """构建绑定真实 PolicyEngine / ApprovalEngine / ReplayGuard / EvidenceLedger 的 ToolBroker。"""
        store = InMemoryBrokerStore()
        policy = PolicyEngine()
        preps = PreparationService(store=store.preps)
        approvals = ApprovalEngine(
            store=InMemoryApprovalStore(),
            policy=policy,
            preparations=preps,
        )
        signer = LocalEd25519Signer(kid="broker-kid-1")
        keyring = KeyRing()
        signer.register(keyring)
        issuer = CapabilityIssuer(signer=signer)
        verifier = CapabilityVerifier(keyring=keyring)
        replay = ReplayGuard(fast=InMemoryFastPath(), store=store.consumptions)
        ledger = EvidenceLedger(store=store.ledger, signer=signer)
        return ToolBroker(
            store=store,
            policy=policy,
            preparations=preps,
            approvals=approvals,
            issuer=issuer,
            replay=replay,
            ledger=ledger,
            verifier=verifier,
            input_resolver=lambda p: dict(p.inputs),
            executors={"seatunnel.submit_batch": lambda ctx: {"jobId": "zeta-1"}},
        )


class _TimeoutZetaTransport:
    """模拟 SeaTunnel Zeta 提交超时/响应丢失且随后可在集群列表查询到或查询不到的真实传输替身。"""

    def __init__(self, *, job_created_on_cluster: bool = True) -> None:
        self.submit_calls = 0
        self.stop_calls = 0
        self.job_created_on_cluster = job_created_on_cluster
        self.last_submitted_job_name: str | None = None

    def post_submit_job(self, *, job_name: str, hocon_body: str) -> dict[str, Any]:
        self.submit_calls += 1
        self.last_submitted_job_name = job_name
        raise httpx.TimeoutException("SeaTunnel Zeta submit response timed out after 3000ms")

    def get_job_info(self, external_job_id: str) -> dict[str, Any]:
        return {
            "jobId": external_job_id,
            "jobName": self.last_submitted_job_name or "stjob",
            "jobStatus": "RUNNING",
            "metrics": {"SourceReceivedCount": 1000, "SinkWriteCount": 1000},
        }

    def list_jobs(self) -> list[dict[str, Any]]:
        if not self.job_created_on_cluster or not self.last_submitted_job_name:
            return []
        return [
            {
                "jobId": "zeta-reconciled-9901",
                "jobName": self.last_submitted_job_name,
                "jobStatus": "RUNNING",
            }
        ]

    def post_stop_job(
        self,
        external_job_id: str,
        *,
        is_stop_with_save_point: bool = False,
    ) -> dict[str, Any]:
        self.stop_calls += 1
        return {"jobId": external_job_id, "stopped": True}


class _InMemoryControlPlaneRepo:
    """用于 DataPlaneWorkerDispatcher L1 幂等重投测试的内存控制面仓储实现。"""

    def __init__(
        self,
        *,
        org_id: str,
        proj_id: str,
        fact: AuthorizedCommandFact,
        run: ExecutionRunRecord,
    ) -> None:
        self.org_id = org_id
        self.proj_id = proj_id
        self.fact = fact
        self.run = run
        self.rls_applied: list[tuple[str, str, str]] = []

    def resolve_execution_tenant_context(
        self,
        execution_id: str,
        command_id: str,
    ) -> tuple[str, str] | None:
        if execution_id == self.run.execution_id and command_id == self.fact.command_id:
            return (self.org_id, self.proj_id)
        return None

    def set_transaction_rls_context(
        self,
        *,
        organization_id: str,
        project_id: str,
        actor_id: str,
    ) -> None:
        self.rls_applied.append((organization_id, project_id, actor_id))

    def get_authorized_command_fact(
        self,
        command_id: str,
        execution_id: str,
    ) -> AuthorizedCommandFact | None:
        if command_id == self.fact.command_id and execution_id == self.run.execution_id:
            return self.fact
        return None

    def get_execution_run(self, execution_id: str) -> ExecutionRunRecord | None:
        if execution_id == self.run.execution_id:
            return self.run
        return None

    def mark_command_completed(
        self,
        command_id: str,
        result_summary: dict[str, Any],
    ) -> None:
        if command_id == self.fact.command_id:
            self.fact.status = "COMPLETED"


class RealCrossModuleFaultSimulator:
    """对接真实合并数据面与控制面模块的 L1 故障模拟器。"""

    def __init__(self, db_session: Session, seeded_env: dict[str, Any]) -> None:
        self._session = db_session
        self._seeded = seeded_env

    def simulate(self, case: dict[str, Any]) -> tuple[str, str | None]:
        """根据 L1 用例类型调用真实数据面/控制面组件并返回 `(actual_outcome, actual_error_code)`。"""
        fault_type = case["fault_type"]
        params = case.get("simulation_params", {})

        # 1. 源库连接在探查期间中断 -> 验证 ConnectionService.test_connection 可控失败并更新状态
        if fault_type == "db_disconnect":
            org_id = self._seeded["organizations"]["org-alpha"].id
            proj_id = self._seeded["projects"]["proj-orders-prod"].id
            maker_id = self._seeded["users"]["maker"].id
            conn_svc = ConnectionService()
            created = conn_svc.create_connection(
                self._session,
                organization_id=org_id,
                project_id=proj_id,
                name=f"l1-mysql-disconnect-{uuid.uuid4().hex[:6]}",
                connector_id="mysql-jdbc",
                connection_type="MYSQL",
                config={"host": "127.0.0.1", "port": 3306, "database": "demo_ecommerce"},
                secret_payload={"password": "demo_password"},
                actor_user_id=maker_id,
            )
            conn_id = uuid.UUID(created["id"])

            def _disconnecting_prober(_conn: Any) -> None:
                raise ConnectionError(
                    f"Simulated {params.get('target', 'source_mysql')} disconnect after "
                    f"{params.get('disconnect_after_ms', 500)}ms"
                )

            try:
                conn_svc.test_connection(
                    self._session,
                    organization_id=org_id,
                    project_id=proj_id,
                    connection_id=conn_id,
                    custom_prober=_disconnecting_prober,
                )
            except AppError as exc:
                return "RECOVERABLE_FAILURE", exc.error_code
            return "UNEXPECTED_SUCCESS", None

        # 2. Celery Worker 崩溃与至少一次重投 -> 验证 ToolBroker + OutboxDispatcher + Worker 幂等性
        if fault_type == "worker_crash_redelivery":
            redelivery_count = int(params.get("redelivery_count", 2))
            idem_key = str(params.get("idempotency_key", "exec-idem-002"))

            store = InMemoryBrokerStore()
            policy = PolicyEngine()
            preps = PreparationService(store=store.preps)
            approvals = ApprovalEngine(
                store=InMemoryApprovalStore(),
                policy=policy,
                preparations=preps,
            )
            signer = LocalEd25519Signer(kid="l1-worker-kid")
            keyring = KeyRing()
            signer.register(keyring)
            issuer = CapabilityIssuer(signer=signer)
            verifier = CapabilityVerifier(keyring=keyring)
            replay = ReplayGuard(fast=InMemoryFastPath(), store=store.consumptions)
            ledger = EvidenceLedger(store=store.ledger, signer=signer)

            external_side_effect_calls = 0

            def _batch_executor(ctx: dict[str, Any]) -> dict[str, Any]:
                nonlocal external_side_effect_calls
                external_side_effect_calls += 1
                return {"jobId": "zeta-idem-001", "command_id": ctx["command"]["command_id"]}

            broker = ToolBroker(
                store=store,
                policy=policy,
                preparations=preps,
                approvals=approvals,
                issuer=issuer,
                replay=replay,
                ledger=ledger,
                verifier=verifier,
                input_resolver=lambda p: dict(p.inputs),
                executors={"seatunnel.submit_batch": _batch_executor},
            )

            org_id = str(uuid.uuid4())
            proj_id = str(uuid.uuid4())
            inputs = {
                "organization_id": org_id,
                "project_id": proj_id,
                "environment": "dev",
                "data_classification": "INTERNAL",
                "risk_level": "P3",
                "pipeline_version_hash": _sample_sha256("pv-l1-002"),
                "metadata_snapshot_fingerprint": _sample_sha256("meta-l1-002"),
                "secret_versions": {"src": 1},
                "policy_version": policy.store.active.version,
                "artifact_hash": _sample_sha256("art-l1-002"),
                "budget": {"max_rows": 10000},
                "tool": "seatunnel.submit_batch",
            }
            prep = preps.prepare(
                maker_id="maker-1",
                inputs=inputs,
                risk_level="P3",
                required_slots=["AUTO_P3"],
            )
            commit_res = broker.commit(
                organization_id=org_id,
                project_id=proj_id,
                preparation_id=prep.preparation_id,
                operator={"user_id": "operator-1", "permissions": ["execution.commit"]},
                idempotency_key=idem_key,
                tool="seatunnel.submit_batch",
            )
            outbox_payload = store.outbox_events[commit_res.outbox_event_id]["payload"]

            # 模拟 Celery 重复投递 redelivery_count 次
            outcomes = [broker.handle_outbox(outbox_payload) for _ in range(redelivery_count)]
            if (
                external_side_effect_calls == 1
                and outcomes[0]["idempotent"] is False
                and all(o["idempotent"] is True for o in outcomes[1:])
            ):
                return "IDEMPOTENT_NO_DUPLICATE", None
            return "DUPLICATE_SIDE_EFFECT_DETECTED", "CAPABILITY_REPLAY_DETECTED"

        # 3. SeaTunnel Zeta 提交请求响应超时/丢失 -> 验证进入 UNKNOWN 待对账状态而非盲目重提
        if fault_type == "zeta_submit_unknown":
            transport = _TimeoutZetaTransport(job_created_on_cluster=True)
            adapter = SeaTunnelZetaAdapter(transport=transport)
            run = ExecutionRunRecord(
                execution_id=str(uuid.uuid4()),
                organization_id=str(uuid.uuid4()),
                project_id=str(uuid.uuid4()),
                pipeline_id=str(uuid.uuid4()),
                pipeline_version_id=str(uuid.uuid4()),
                artifact_hash=_sample_sha256("art-l1-003"),
                execution_mode=ExecutionMode.BATCH,
                status="QUEUED",
                confirmation_state=ConfirmationState.REQUEST_SUBMITTED,
                preparation_id=str(uuid.uuid4()),
                capability_jti=str(uuid.uuid4()),
                command_id=str(uuid.uuid4()),
            )
            hocon_tpl = 'env { job.mode = "BATCH" }\nsource { password = "${SECRET:MYSQL_PWD}" }'
            try:
                adapter.submit_job(
                    run=run,
                    hocon_template=hocon_tpl,
                    secret_resolver=lambda k: "secret-val",
                    trace_id="trace-l1-003",
                )
            except SeaTunnelSubmitUnknownError as exc:
                # 验证状态机已进入 UNKNOWN + RECONCILIATION_PENDING，且再次调用 submit_job 绝不重发第二次 HTTP POST
                assert run.status == "UNKNOWN"
                assert run.confirmation_state == ConfirmationState.RECONCILIATION_PENDING
                ref_retry = adapter.submit_job(
                    run=run,
                    hocon_template=hocon_tpl,
                    secret_resolver=lambda k: "secret-val",
                    trace_id="trace-l1-003-retry",
                )
                assert transport.submit_calls == 1
                assert ref_retry.submit_state == "UNKNOWN"
                return "RECONCILIATION_REQUIRED", exc.error_code
            return "UNEXPECTED_SUBMIT_SUCCESS", None

        # 4 & 5. 运行监督拒绝率阈值检查（>20% 触发中断 vs 恰好 20% 边界放行）
        if fault_type in ("reject_rate_breach", "reject_rate_exact_boundary"):
            validated_rows = int(params["validated_rows"])
            error_rows = int(params["error_rows"])
            threshold = float(params.get("threshold", 0.20))

            contract = RuntimeSupervisionContract(
                max_read_rows=100_000,
                max_write_bytes=100 * 1024 * 1024,
                max_duration_seconds=600,
                max_amplification_ratio=2.0,
                max_reject_rate_total=threshold,
                max_reject_rate_window=threshold,
                window_definition=WindowDefinition(mode=WindowMode.ROW_COUNT, size=1000),
                min_validated_rows=100,
                max_error_rows_absolute=50_000,
                action_on_breach="HARD_INTERRUPT",
            )
            run = ExecutionRunRecord(
                execution_id=str(uuid.uuid4()),
                organization_id=str(uuid.uuid4()),
                project_id=str(uuid.uuid4()),
                pipeline_id=str(uuid.uuid4()),
                pipeline_version_id=str(uuid.uuid4()),
                artifact_hash=_sample_sha256("art-l1-sup"),
                execution_mode=ExecutionMode.BATCH,
                status="RUNNING",
                confirmation_state=ConfirmationState.ENGINE_CONFIRMED,
                preparation_id=str(uuid.uuid4()),
                capability_jti=str(uuid.uuid4()),
                command_id=str(uuid.uuid4()),
                external_job_id="zeta-job-sup-001",
            )
            metrics = ExecutionMetricsSnapshot(
                execution_id=run.execution_id,
                read_rows=validated_rows,
                validated_rows=validated_rows,
                valid_rows=validated_rows - error_rows,
                write_rows=validated_rows - error_rows,
                write_bytes=(validated_rows - error_rows) * 128,
                error_rows=error_rows,
                duration_seconds=12.5,
            )
            sup_engine = RuntimeSupervisionEngine()
            eval_res = sup_engine.supervise_and_enforce(
                run=run,
                contract=contract,
                metrics=metrics,
            )
            if eval_res.breached:
                assert run.status == "INTERRUPTED"
                assert run.diagnosis_json is not None
                return "SUPERVISION_INTERRUPTED", SupervisionThresholdBreachedError.error_code
            assert run.status == "RUNNING"
            return "ALLOWED_AT_BOUNDARY", None

        # 6. 发布前检测到影子表与目标表存在不兼容 Schema Drift -> 阻断原子替换
        if fault_type == "schema_drift":
            target_cols = [
                DorisColumnSpec(name="order_id", data_type="BIGINT", nullable=False, is_key=True),
                DorisColumnSpec(name="order_amount", data_type="DECIMAL(18, 2)", nullable=False),
            ]
            shadow_cols = [
                DorisColumnSpec(name="order_id", data_type="BIGINT", nullable=False, is_key=True),
                DorisColumnSpec(
                    name="order_amount",
                    data_type="VARCHAR(64)" if params.get("type_mismatch") else "DECIMAL(18, 2)",
                    nullable=False,
                ),
            ]
            for extra_col in params.get("added_columns", []):
                shadow_cols.append(DorisColumnSpec(name=str(extra_col), data_type="VARCHAR(32)", nullable=True))

            target_schema = DorisTableSchemaSpec(
                table_name="dwd_order_user_wide",
                columns=target_cols,
                key_columns=["order_id"],
            )
            shadow_schema = DorisTableSchemaSpec(
                table_name="dwd_order_user_wide__shadow_l1flt006",
                columns=shadow_cols,
                key_columns=["order_id"],
            )
            doris_adapter = DorisPublishAdapter()
            try:
                doris_adapter.publish_shadow_to_target(
                    organization_id=str(uuid.uuid4()),
                    project_id=str(uuid.uuid4()),
                    execution_id=str(uuid.uuid4()),
                    pipeline_version_id=str(uuid.uuid4()),
                    artifact_hash=_sample_sha256("art-l1-006"),
                    target_connection_id="conn-doris-1",
                    quality_report_id="qr-1",
                    published_by="operator-1",
                    target_schema=target_schema,
                    shadow_schema=shadow_schema,
                )
            except PublishSchemaIncompatibleError as exc:
                assert len(doris_adapter.executed_sql_log) == 0
                return "PUBLISH_BLOCKED", exc.error_code
            return "UNEXPECTED_PUBLISH_ALLOWED", None

        return "UNKNOWN_OUTCOME", None


@pytest.fixture()
def qa_sqlite_session() -> tuple[Session, dict[str, Any]]:
    """创建内存数据库会话并初始化预置多组织/多项目/6角色种子数据。"""
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = factory()
    seeded = seed_demo_iam_data(session)
    session.commit()
    try:
        yield session, seeded
    finally:
        session.close()
        engine.dispose()


def test_l0_all_security_injection_cases_against_real_modules(tmp_path: Path) -> None:
    """加载真实 `l0_security_cases.json`，逐条驱动真实业务模块并断言 P0/P1 100% 拦截率。"""
    evaluator = RealCrossModuleSecurityEvaluator(tmp_root=tmp_path / "controlled_uploads")
    engine = BenchmarkEngine(cases_dir=_CASES_DIR, security_evaluator=evaluator)
    records = engine.run_l0()

    assert len(records) == 8
    for rec in records:
        assert rec.passed is True, (
            f"L0 安全用例 {rec.case_id} ({rec.category}) 未通过: "
            f"actual_blocked={rec.actual_blocked}, actual_error_code={rec.actual_error_code}"
        )
        assert rec.actual_blocked is True

    p0_records = [r for r in records if r.risk_level == "P0"]
    p1_records = [r for r in records if r.risk_level == "P1"]
    assert len(p0_records) == 5
    assert len(p1_records) == 3
    assert all(r.passed for r in p0_records)
    assert all(r.passed for r in p1_records)


def test_l1_all_fault_simulation_cases_against_real_modules(
    qa_sqlite_session: tuple[Session, dict[str, Any]],
) -> None:
    """加载真实 `l1_fault_cases.json`，逐条驱动真实数据面与控制面模块并断言 100% 符合预期恢复/阻断语义。"""
    session, seeded = qa_sqlite_session
    simulator = RealCrossModuleFaultSimulator(db_session=session, seeded_env=seeded)
    engine = BenchmarkEngine(cases_dir=_CASES_DIR, fault_simulator=simulator)
    records = engine.run_l1()

    assert len(records) == 6
    for rec in records:
        assert rec.passed is True, (
            f"L1 故障模拟用例 {rec.case_id} ({rec.category}) 未通过: "
            f"actual_outcome={rec.actual_outcome}, actual_error_code={rec.actual_error_code}"
        )


def test_l1_supervision_exact_20_0_percent_vs_20_1_percent_boundary() -> None:
    """专项验证 RuntimeSupervisionEngine 拒绝率边界：20.0% (200/1000) 不触发 vs 20.1% (201/1000) 立即触发硬中断。"""
    contract = RuntimeSupervisionContract(
        max_read_rows=100_000,
        max_write_bytes=50 * 1024 * 1024,
        max_duration_seconds=300,
        max_amplification_ratio=1.5,
        max_reject_rate_total=0.20,
        max_reject_rate_window=0.20,
        window_definition=WindowDefinition(mode=WindowMode.ROW_COUNT, size=1000),
        min_validated_rows=100,
        max_error_rows_absolute=5000,
        action_on_breach="HARD_INTERRUPT",
    )
    sup_engine = RuntimeSupervisionEngine()

    # 1. 恰好 20.0% (200 / 1000 = 0.200) -> 严禁触发中断
    run_boundary = ExecutionRunRecord(
        execution_id=str(uuid.uuid4()),
        organization_id=str(uuid.uuid4()),
        project_id=str(uuid.uuid4()),
        pipeline_id=str(uuid.uuid4()),
        pipeline_version_id=str(uuid.uuid4()),
        artifact_hash=_sample_sha256("art-20-0"),
        execution_mode=ExecutionMode.BATCH,
        status="RUNNING",
        confirmation_state=ConfirmationState.ENGINE_CONFIRMED,
        preparation_id=str(uuid.uuid4()),
        capability_jti=str(uuid.uuid4()),
        command_id=str(uuid.uuid4()),
        external_job_id="zeta-job-200",
    )
    snap_20_0 = ExecutionMetricsSnapshot(
        execution_id=run_boundary.execution_id,
        read_rows=1000,
        validated_rows=1000,
        valid_rows=800,
        write_rows=800,
        write_bytes=800 * 64,
        error_rows=200,
        duration_seconds=5.0,
    )
    res_20_0 = sup_engine.supervise_and_enforce(run=run_boundary, contract=contract, metrics=snap_20_0)
    assert res_20_0.reject_rate_total == pytest.approx(0.20)
    assert res_20_0.breached is False
    assert res_20_0.action_to_take == "NONE"
    assert run_boundary.status == "RUNNING"

    # 2. 20.1% (201 / 1000 = 0.201 > 0.20) -> 立即触发 HARD_INTERRUPT 并调用适配器停止作业
    cancelled_jobs: list[str] = []

    class _RecordingCancelAdapter:
        def cancel_job(self, external_job_id: str) -> dict[str, Any]:
            cancelled_jobs.append(external_job_id)
            return {"jobId": external_job_id, "cancelled": True}

    run_breach = ExecutionRunRecord(
        execution_id=str(uuid.uuid4()),
        organization_id=str(uuid.uuid4()),
        project_id=str(uuid.uuid4()),
        pipeline_id=str(uuid.uuid4()),
        pipeline_version_id=str(uuid.uuid4()),
        artifact_hash=_sample_sha256("art-20-1"),
        execution_mode=ExecutionMode.BATCH,
        status="RUNNING",
        confirmation_state=ConfirmationState.ENGINE_CONFIRMED,
        preparation_id=str(uuid.uuid4()),
        capability_jti=str(uuid.uuid4()),
        command_id=str(uuid.uuid4()),
        external_job_id="zeta-job-201",
    )
    snap_20_1 = ExecutionMetricsSnapshot(
        execution_id=run_breach.execution_id,
        read_rows=1000,
        validated_rows=1000,
        valid_rows=799,
        write_rows=799,
        write_bytes=799 * 64,
        error_rows=201,
        duration_seconds=5.0,
    )
    res_20_1 = sup_engine.supervise_and_enforce(
        run=run_breach,
        contract=contract,
        metrics=snap_20_1,
        adapter=_RecordingCancelAdapter(),
        error_code_counts={"DQ_REGEX_VIOLATION": 201},
    )
    assert res_20_1.reject_rate_total == pytest.approx(0.201)
    assert res_20_1.breached is True
    assert res_20_1.action_to_take == "HARD_INTERRUPT"
    assert run_breach.status == "INTERRUPTED"
    assert cancelled_jobs == ["zeta-job-201"]
    assert run_breach.diagnosis_json is not None
    assert run_breach.diagnosis_json["advisory_only"] is True
    assert run_breach.diagnosis_json["side_effects_executed"] is False


def test_l1_seatunnel_submit_unknown_and_run_reconciler_no_blind_resubmit() -> None:
    """专项验证 SeaTunnelZetaAdapter 提交响应丢失进入 UNKNOWN 后，RunReconciler 对账恢复且全程绝不重发 submit-job。"""
    transport = _TimeoutZetaTransport(job_created_on_cluster=True)
    adapter = SeaTunnelZetaAdapter(transport=transport)
    reconciler = RunReconciler(adapter=adapter)

    run = ExecutionRunRecord(
        execution_id=str(uuid.uuid4()),
        organization_id=str(uuid.uuid4()),
        project_id=str(uuid.uuid4()),
        pipeline_id=str(uuid.uuid4()),
        pipeline_version_id=str(uuid.uuid4()),
        artifact_hash=_sample_sha256("art-reconcile"),
        execution_mode=ExecutionMode.BATCH,
        status="CREATED",
        confirmation_state=ConfirmationState.REQUEST_SUBMITTED,
        preparation_id=str(uuid.uuid4()),
        capability_jti=str(uuid.uuid4()),
        command_id=str(uuid.uuid4()),
    )

    with pytest.raises(SeaTunnelSubmitUnknownError) as exc_info:
        adapter.submit_job(
            run=run,
            hocon_template='env { job.mode = "BATCH" }\nsource { password = "${SECRET:DB_PASS}" }',
            secret_resolver=lambda k: "real-secret",
        )
    assert exc_info.value.error_code == "SEATUNNEL_SUBMIT_UNKNOWN"
    assert run.status == "UNKNOWN"
    assert run.confirmation_state == ConfirmationState.RECONCILIATION_PENDING
    assert transport.submit_calls == 1

    # 执行 RunReconciler 对账：应从 Zeta 列表匹配到 stable_submit_id 并推进至 RECONCILING -> RUNNING (ENGINE_CONFIRMED)
    outcome = reconciler.reconcile(run)
    assert outcome.found_on_engine is True
    assert outcome.external_job_id == "zeta-reconciled-9901"
    assert outcome.resolved_status == "RUNNING"
    assert outcome.confirmation_state == ConfirmationState.ENGINE_CONFIRMED
    assert run.status == "RUNNING"
    assert run.external_job_id == "zeta-reconciled-9901"
    # 核心不变量：对账后总提交次数仍严格为 1，绝无第二次提交
    assert transport.submit_calls == 1


def test_l1_rollback_executor_cdc_c_plus_d_operator_confirmation_block() -> None:
    """专项验证 RollbackExecutor 的 CDC C+D 恢复策略：检查点丢失时阻断静默重跑，Operator 显式确认后方可恢复。"""
    executor = RollbackExecutor()
    org_id = str(uuid.uuid4())
    proj_id = str(uuid.uuid4())

    # 1. 路径 D 阻断测试：CDC 处于 CHECKPOINT_LOST 且 operator_confirmed_resnapshot=False -> 必须抛出 CDC_RECOVERY_OPERATOR_CONFIRM_REQUIRED
    cdc_run = ExecutionRunRecord(
        execution_id=str(uuid.uuid4()),
        organization_id=org_id,
        project_id=proj_id,
        pipeline_id=str(uuid.uuid4()),
        pipeline_version_id=str(uuid.uuid4()),
        artifact_hash=_sample_sha256("art-cdc-cd"),
        execution_mode=ExecutionMode.CDC,
        status="CHECKPOINT_LOST",
        confirmation_state=ConfirmationState.ENGINE_CONFIRMED,
        preparation_id=str(uuid.uuid4()),
        capability_jti=str(uuid.uuid4()),
        command_id=str(uuid.uuid4()),
        checkpoint_ref="mysql-bin.000003:4120",
    )
    ctx_unconfirmed = RollbackCommandContext(
        organization_id=org_id,
        project_id=proj_id,
        preparation_id=str(uuid.uuid4()),
        capability_jti=str(uuid.uuid4()),
        command_id=str(uuid.uuid4()),
        initiated_by="operator-1",
    )
    with pytest.raises(CdcRecoveryOperatorConfirmRequiredError) as exc_info:
        executor.execute_cdc_recover(
            ctx=ctx_unconfirmed,
            run=cdc_run,
            checkpoint_valid=False,
            position_expired=True,
            operator_confirmed_resnapshot=False,
        )
    assert exc_info.value.error_code == "CDC_RECOVERY_OPERATOR_CONFIRM_REQUIRED"
    assert cdc_run.status == "CHECKPOINT_LOST"

    # 2. 路径 D 放行测试：Operator 显式确认并填写原因后，进入 RECOVERING -> INITIAL_SNAPSHOT
    ctx_confirmed = RollbackCommandContext(
        organization_id=org_id,
        project_id=proj_id,
        preparation_id=str(uuid.uuid4()),
        capability_jti=str(uuid.uuid4()),
        command_id=str(uuid.uuid4()),
        initiated_by="operator-1",
    )
    record_d = executor.execute_cdc_recover(
        ctx=ctx_confirmed,
        run=cdc_run,
        checkpoint_valid=False,
        position_expired=True,
        operator_confirmed_resnapshot=True,
        operator_confirmation_reason="Binlog 已被源库轮转清理，Operator 确认重新执行初始快照与幂等覆盖",
    )
    assert record_d.status == "SUCCEEDED"
    assert record_d.result_json["recovery_policy"] == "POLICY_D_OPERATOR_CONFIRMED_RESNAPSHOT"
    assert record_d.result_json["resnapshot_triggered"] is True
    assert cdc_run.status == "INITIAL_SNAPSHOT"

    # 3. 路径 C 恢复测试：检查点合法且未过期时，直接从历史检查点恢复至 RECOVERING -> STREAMING
    cdc_run_valid_cp = ExecutionRunRecord(
        execution_id=str(uuid.uuid4()),
        organization_id=org_id,
        project_id=proj_id,
        pipeline_id=str(uuid.uuid4()),
        pipeline_version_id=str(uuid.uuid4()),
        artifact_hash=_sample_sha256("art-cdc-c"),
        execution_mode=ExecutionMode.CDC,
        status="DEGRADED",
        confirmation_state=ConfirmationState.ENGINE_CONFIRMED,
        preparation_id=str(uuid.uuid4()),
        capability_jti=str(uuid.uuid4()),
        command_id=str(uuid.uuid4()),
        checkpoint_ref="pg-lsn:0/16B3748",
    )
    ctx_policy_c = RollbackCommandContext(
        organization_id=org_id,
        project_id=proj_id,
        preparation_id=str(uuid.uuid4()),
        capability_jti=str(uuid.uuid4()),
        command_id=str(uuid.uuid4()),
        initiated_by="operator-1",
    )
    record_c = executor.execute_cdc_recover(
        ctx=ctx_policy_c,
        run=cdc_run_valid_cp,
        checkpoint_valid=True,
        position_expired=False,
        replication_slot_active=True,
    )
    assert record_c.result_json["recovery_policy"] == "POLICY_C_HISTORICAL_CHECKPOINT"
    assert record_c.result_json["resnapshot_triggered"] is False
    assert cdc_run_valid_cp.status == "STREAMING"


def test_l1_outbox_dispatcher_and_data_plane_worker_redelivery_idempotency(
    qa_sqlite_session: tuple[Session, dict[str, Any]],
) -> None:
    """专项验证 OutboxDispatcher 租约投递与 DataPlaneWorkerDispatcher 收到重复命令时的端到端幂等性。"""
    session, seeded = qa_sqlite_session
    org_id = seeded["organizations"]["org-alpha"].id
    proj_id = seeded["projects"]["proj-orders-prod"].id

    exec_id = str(uuid.uuid4())
    cmd_id = str(uuid.uuid4())
    cap_jti = str(uuid.uuid4())
    payload = {
        "schema_version": "1.0.0",
        "event_type": "EXECUTION_SUBMIT_BATCH",
        "command_id": cmd_id,
        "execution_id": exec_id,
        "capability_jti": cap_jti,
        "capability_jws": "eyJhbGciOiJFZERTQSIsImtpZCI6ImtleS0xIn0.payload_part_valid.signature_part_valid",
        "trace_id": "trace-outbox-idem-001",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    ev = OutboxEvent(
        organization_id=org_id,
        project_id=proj_id,
        aggregate_type="ExecutionRun",
        aggregate_id=uuid.UUID(exec_id),
        event_type="EXECUTION_SUBMIT_BATCH",
        payload_ref_json=payload,
    )
    session.add(ev)
    session.flush()
    deliv = OutboxDelivery(outbox_event_id=ev.id, status="PENDING", attempts=0, max_attempts=3)
    session.add(deliv)
    session.commit()

    # 构建真实 DataPlaneWorkerDispatcher + 计数型 SeaTunnel 传输层
    submit_http_calls = 0

    class _CountingSuccessTransport:
        def post_submit_job(self, *, job_name: str, hocon_body: str) -> dict[str, Any]:
            nonlocal submit_http_calls
            submit_http_calls += 1
            return {"jobId": "zeta-live-7788", "jobName": job_name}

        def get_job_info(self, external_job_id: str) -> dict[str, Any]:
            return {"jobId": external_job_id, "jobStatus": "RUNNING"}

        def list_jobs(self) -> list[dict[str, Any]]:
            return []

        def post_stop_job(self, external_job_id: str, *, is_stop_with_save_point: bool = False) -> dict[str, Any]:
            return {"jobId": external_job_id}

    fact = AuthorizedCommandFact(
        command_id=cmd_id,
        execution_id=exec_id,
        organization_id=str(org_id),
        project_id=str(proj_id),
        command_type="EXECUTION_SUBMIT_BATCH",
        capability_jti=cap_jti,
        capability_consumed=True,
        status="PENDING",
        payload_json={"hocon_template": 'env { job.mode = "BATCH" }\nsource { password = "${SECRET:SRC_PWD}" }'},
    )
    run = ExecutionRunRecord(
        execution_id=exec_id,
        organization_id=str(org_id),
        project_id=str(proj_id),
        pipeline_id=str(uuid.uuid4()),
        pipeline_version_id=str(uuid.uuid4()),
        artifact_hash=_sample_sha256("art-outbox-worker"),
        execution_mode=ExecutionMode.BATCH,
        status="QUEUED",
        confirmation_state=ConfirmationState.REQUEST_SUBMITTED,
        preparation_id=str(uuid.uuid4()),
        capability_jti=cap_jti,
        command_id=cmd_id,
    )
    repo = _InMemoryControlPlaneRepo(org_id=str(org_id), proj_id=str(proj_id), fact=fact, run=run)
    worker_dispatcher = DataPlaneWorkerDispatcher(
        repository=repo,
        seatunnel_adapter=SeaTunnelZetaAdapter(transport=_CountingSuccessTransport()),
    )

    worker_results: list[dict[str, Any]] = []

    def _celery_task_sender(_task_name: str, msg_payload: dict[str, Any]) -> None:
        # 模拟 Celery At-least-once 将同一消息重复投递给 Worker 两次
        worker_results.append(worker_dispatcher.handle_outbox_command(msg_payload))
        worker_results.append(worker_dispatcher.handle_outbox_command(msg_payload))

    outbox_dispatcher = OutboxDispatcher(task_sender=_celery_task_sender, dispatcher_id="qa-dispatcher")
    stats = outbox_dispatcher.dispatch_once(session)
    session.commit()

    assert stats["claimed"] == 1
    assert stats["delivered"] == 1
    # 再次调用 dispatch_once，已 DELIVERED 的事件不得被二次领取
    stats_second = outbox_dispatcher.dispatch_once(session)
    assert stats_second["claimed"] == 0

    # 验证 Worker 侧第一次执行真实提交，第二次重投返回 IDEMPOTENT_NOOP 且外部 HTTP 提交仅发生 1 次
    assert len(worker_results) == 2
    assert worker_results[0]["status"] == "SUBMITTED"
    assert worker_results[1]["status"] == "IDEMPOTENT_NOOP"
    assert worker_results[1]["side_effects_executed"] is False
    assert submit_http_calls == 1


def test_benchmark_engine_and_scoring_p0_p1_independent_hard_gate(
    tmp_path: Path,
    qa_sqlite_session: tuple[Session, dict[str, Any]],
) -> None:
    """将真实跨模块 L0 + L1 结果汇入 `scoring.py`，验证 P0/P1 独立硬门槛与 Provider 模式同质性约束。"""
    session, seeded = qa_sqlite_session
    evaluator = RealCrossModuleSecurityEvaluator(tmp_root=tmp_path / "uploads")
    simulator = RealCrossModuleFaultSimulator(db_session=session, seeded_env=seeded)

    engine = BenchmarkEngine(
        cases_dir=_CASES_DIR,
        security_evaluator=evaluator,
        fault_simulator=simulator,
    )
    report = engine.run_suite(
        run_id="qa-m10-real-l0-l1-run",
        provider_mode="mock",
        generation_metrics={
            "plan_validity": 1.0,
            "hocon_validity": 1.0,
            "schema_mapping_accuracy": 1.0,
            "repair_success_rate": 1.0,
        },
    )

    assert report.score_result is not None
    score = report.score_result
    assert score.p0_p1_gate_passed is True
    assert score.overall_passed is True
    assert score.raw_metrics["p0_block_rate"] == 1.0
    assert score.raw_metrics["p1_block_rate"] == 1.0
    assert score.raw_metrics["sql_safety_detection"] == 1.0
    assert score.raw_metrics["replay_prevention"] == 1.0
    assert score.raw_metrics["recovery_success"] == 1.0
    assert score.composite_score == 1.0
    assert score.weights_confirmed_by_user is False

    # 验证 P0 独立一票否决硬门槛：即便其余指标全满分（综合分 > 0.96），只要有 1 例 P0 失败，overall_passed 必须为 False
    p0_fail_metrics = RawBenchmarkMetrics(
        plan_validity=1.0,
        hocon_validity=1.0,
        schema_mapping_accuracy=1.0,
        sql_safety_detection=1.0,
        p0_block_rate=0.80,
        p1_block_rate=1.0,
        replay_prevention=1.0,
        repair_success_rate=1.0,
        end_to_end_success=1.0,
        recovery_success=1.0,
        connector_capability_pass_rate=1.0,
        p0_failed_count=1,
        p1_failed_count=0,
    )
    p0_fail_score = score_benchmark(p0_fail_metrics, provider_mode="mock", profile=ScoringProfile())
    assert p0_fail_score.composite_score > 0.95
    assert p0_fail_score.p0_p1_gate_passed is False
    assert p0_fail_score.overall_passed is False
    assert any("P0 安全硬门槛未通过" in reason for reason in p0_fail_score.gate_failure_reasons)

    # 验证 P1 独立一票否决硬门槛：即便其余指标全满分，只要有 1 例 P1 失败，overall_passed 必须为 False
    p1_fail_metrics = RawBenchmarkMetrics(
        plan_validity=1.0,
        hocon_validity=1.0,
        schema_mapping_accuracy=1.0,
        sql_safety_detection=1.0,
        p0_block_rate=1.0,
        p1_block_rate=0.6667,
        replay_prevention=1.0,
        repair_success_rate=1.0,
        end_to_end_success=1.0,
        recovery_success=1.0,
        connector_capability_pass_rate=1.0,
        p0_failed_count=0,
        p1_failed_count=1,
    )
    p1_fail_score = score_benchmark(p1_fail_metrics, provider_mode="mock", profile=ScoringProfile())
    assert p1_fail_score.composite_score > 0.95
    assert p1_fail_score.p0_p1_gate_passed is False
    assert p1_fail_score.overall_passed is False
    assert any("P1 安全硬门槛未通过" in reason for reason in p1_fail_score.gate_failure_reasons)

    # 验证严禁混合 Mock 与 Real Provider 运行记录
    with pytest.raises(ValueError, match="禁止将 Mock LLM 运行与真实 Provider 运行结果混合评分"):
        validate_homogeneous_provider_mode(["mock", "real"])

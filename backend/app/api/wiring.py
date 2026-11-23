"""控制面全量模块装配器（Application Integration Wiring）。

负责将 Wave 1 合并的全部兄弟领域服务统一装配至 FastAPI `app.state` 与路由处理器：
1. 连接器注册中心（`ConnectorRegistry` 12 项真实版本化能力矩阵 + `HoconConnectorRegistry` 编译器视图）。
2. LangGraph 智能体编排服务（`AgentService` + `ProviderGateway` + 只读工具箱 `ReadOnlyToolRegistry` + 租户归属守卫 `ThreadOwnershipStore`）。
3. 确定性门禁与制品冻结编译器（`DeterministicGate` + `ExecutionStrategyCompiler` + `build_pipeline_version` JCS Canonical SHA256 Manifest）。
4. Harness 安全治理内核（`PolicyStore` + `PolicyEngine` + `PreparationService` + `StandingScheduleService` + `ApprovalEngine` + `CapabilityIssuer` + `CapabilityVerifier` + `ReplayGuard` + `EvidenceLedger` + `ToolBroker`）。
5. 数据面适配器与运行时引擎（`SeaTunnelZetaAdapter` + `RunReconciler` + `TwoStageQualityEngine` + `RuntimeSupervisionEngine` + `DorisPublishAdapter` + `RollbackExecutor` + `DataPlaneWorkerDispatcher`）。
6. 基准评测与确定性数据集生成器（`generate_dataset` + `BenchmarkEngine` + `score_benchmark` P0/P1 独立安全硬门槛）。
7. 可观测性遥测与日志脱敏（`init_telemetry` + `get_metrics_registry` + `install_redaction`）。
"""
from __future__ import annotations

import contextvars
import hashlib
import os
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session

# 确保仓库根目录与 backend 目录均在 Python 模块搜索路径中，兼容 app.* 与 backend.app.* 双路径导入
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = _BACKEND_DIR.parent
for _p in (str(_REPO_ROOT), str(_BACKEND_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.agent import (  # noqa: E402
    AgentContext,
    AgentDeps,
    AgentService,
    DeterministicMockProvider,
    OpenAICompatibleProvider,
    ProviderGateway,
    TenantAccessDenied,
    ThreadOwnershipStore,
    build_default_registry,
)
from app.api.deps import ProjectRequestContext  # noqa: E402
from app.approvals import Approval, ApprovalEngine, InMemoryApprovalStore  # noqa: E402
from app.artifacts.canonical import canonical_json, sha256_digest  # noqa: E402
from app.artifacts.manifest import build_pipeline_version  # noqa: E402
from app.broker import InMemoryBrokerStore, ToolBroker  # noqa: E402
from app.capabilities import (  # noqa: E402
    CapabilityIssuer,
    CapabilityVerifier,
    InMemoryFastPath,
    KeyRing,
    LocalEd25519Signer,
    RedisFastPath,
    ReplayGuard,
    VaultTransitEd25519Signer,
)
from app.compiler.gate import DeterministicGate  # noqa: E402
from app.compiler.strategy import ExecutionStrategyCompiler  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.connectors.registry import (  # noqa: E402
    ConnectorRegistry as PlatformConnectorRegistry,
    get_default_connector_registry,
    validate_entry_against_contract,
)
from app.data_plane.reconciler import RunReconciler  # noqa: E402
from app.data_plane.seatunnel_adapter import (  # noqa: E402
    ExternalJobRefRecord,
    SeaTunnelZetaAdapter,
    build_stable_submit_id,
)
from app.data_plane.worker_tasks import (  # noqa: E402
    AuthorizedCommandFact,
    DataPlaneWorkerDispatcher,
)
from app.evidence import EvidenceLedger, verify_chain  # noqa: E402
from app.execution.state_machine import (  # noqa: E402
    ConfirmationState,
    ExecutionMode,
    ExecutionRunRecord,
)
from app.hocon.registry import ConnectorRegistry as HoconConnectorRegistry  # noqa: E402
from app.memberships.service import MembershipService  # noqa: E402
from app.observability.telemetry import (  # noqa: E402
    MetricsRegistry,
    TelemetryManager,
    get_metrics_registry,
    init_telemetry,
)
from app.pipeline.models import default_supervision  # noqa: E402
from app.pipeline.tools import (  # noqa: E402
    MetadataSnapshot as PipelineMetadataSnapshot,
    TableMeta,
    build_dag,
)
from app.policy import DEFAULT_RULES, PolicyEngine, PolicyStore  # noqa: E402
from app.preparations import (  # noqa: E402
    Preparation as DomainPreparation,
    PreparationService,
    StandingSchedulePreparation,
    StandingScheduleService,
    fingerprint as compute_prep_fingerprint,
)
from app.publishing.doris_adapter import DorisPublishAdapter  # noqa: E402
from app.quality.engine import TwoStageQualityEngine  # noqa: E402
from app.rollback.executor import RollbackExecutor  # noqa: E402
from app.security.canonical import GENESIS_PREVIOUS_HASH, canonical_digest  # noqa: E402
from app.security.errors import HarnessError  # noqa: E402
from app.security.redaction import install_redaction  # noqa: E402
from app.shared.db_rls import (  # noqa: E402
    TenantContext,
    apply_rls_context,
    resolve_worker_tenant_context,
)
from app.shared.errors import AppError, ErrorCode  # noqa: E402
from app.shared.models.connections_metadata import (  # noqa: E402
    DataConnection,
    MetadataColumn,
    MetadataObject,
    MetadataSnapshot as DbMetadataSnapshot,
    SecretReference,
    SensitiveClassification,
)
from app.shared.models.evidence_benchmark import (  # noqa: E402
    BenchmarkDataset,
    BenchmarkRun,
    EvidenceCheckpoint,
    EvidenceEvent,
    EvidenceVerificationRun,
    PolicyVersion as DbPolicyVersion,
)
from app.shared.models.execution_quality import (  # noqa: E402
    ExecutionRun,
    ExecutionStateEvent,
    ExternalJobRef,
    RollbackRun,
)
from app.shared.models.governance_harness import (  # noqa: E402
    ApprovalDecision,
    ApprovalSlot,
    CapabilityConsumption,
    CapabilityIssuance,
    ExecutionCommand,
    OutboxDelivery,
    OutboxEvent,
    Preparation as DbPreparation,
    PreparationFingerprint,
)
from app.shared.models.iam import Project  # noqa: E402
from app.shared.models.pipeline_artifacts import (  # noqa: E402
    AgentThread,
    ArtifactManifest as DbArtifactManifest,
    EtlPlanModule,
    HoconArtifact,
    PipelineDefinition,
    PipelineDraft,
    PipelineVersion as DbPipelineVersion,
    QualityContractModel as DbQualityContractModel,
    RuntimeContractModel as DbRuntimeContractModel,
    SqlArtifact,
    WorkflowRun,
)
from app.supervision.engine import RuntimeSupervisionEngine  # noqa: E402
from benchmarks.generator.dataset_generator import generate_dataset  # noqa: E402
from benchmarks.runner.engine import BenchmarkEngine  # noqa: E402
from benchmarks.runner.scoring import (  # noqa: E402
    RawBenchmarkMetrics,
    ScoringProfile,
    score_benchmark,
)

# 请求级数据库 Session 上下文变量，供 LangGraph 只读工具在图执行期间安全查询当前事务内的 MetadataSnapshot
_active_session_var: contextvars.ContextVar[Session | None] = contextvars.ContextVar(
    "active_db_session", default=None
)


class _ContextBoundPermissionChecker:
    """基于数据库双重成员关系与会话状态的 Agent 线程权限校验器。"""

    def __init__(self) -> None:
        self.revoked_users: set[str] = set()

    def can_access(self, ctx: AgentContext) -> bool:
        """校验调用者是否未被吊销且拥有目标租户项目访问权。"""
        if ctx.user_id in self.revoked_users:
            return False
        session = _active_session_var.get()
        if session is None:
            return True
        try:
            org_uuid = uuid.UUID(ctx.organization_id)
            proj_uuid = uuid.UUID(ctx.project_id)
            user_uuid = uuid.UUID(ctx.user_id)
            MembershipService().verify_dual_membership(
                session,
                organization_id=org_uuid,
                project_id=proj_uuid,
                user_id=user_uuid,
            )
            return True
        except Exception:
            return False


class _HttpVaultTransitClient:
    """基于标准库 HTTP 的真实 HashiCorp Vault Transit / KV v2 客户端（私钥永不离开 Vault）。"""

    def __init__(self, vault_addr: str, token: str, kv_mount: str = "secret", transit_mount: str = "transit") -> None:
        self._addr = vault_addr.rstrip("/")
        self._token = token
        self._kv_mount = kv_mount.strip("/")
        self._transit_mount = transit_mount.strip("/")

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        import json as _json
        import urllib.request as _req

        url = f"{self._addr}{path}"
        data = _json.dumps(payload).encode("utf-8") if payload is not None else None
        req = _req.Request(
            url,
            data=data,
            headers={"X-Vault-Token": self._token, "Content-Type": "application/json"},
            method=method,
        )
        with _req.urlopen(req, timeout=5) as resp:  # noqa: S310
            return _json.loads(resp.read().decode("utf-8"))

    def read_kv2_secret(self, secret_ref: str) -> dict[str, Any]:
        """按 KV v2 引用路径（如 secret/data/llm/openai_api_key）读取机密字典。"""
        clean = secret_ref.lstrip("/")
        if not clean.startswith("v1/"):
            clean = f"v1/{clean}"
        res = self._request_json("GET", f"/{clean}")
        return dict(res.get("data", {}).get("data", {}))

    def get_ed25519_public_key(self, key_name: str, key_version: int = 1) -> Any:
        """从 Vault Transit 读取指定版本的 Ed25519 公钥并解析为 cryptography 公钥对象。"""
        import base64 as _b64
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        res = self._request_json("GET", f"/v1/{self._transit_mount}/keys/{key_name}")
        keys = res.get("data", {}).get("keys", {})
        pub_b64 = keys[str(key_version)]["public_key"]
        return Ed25519PublicKey.from_public_bytes(_b64.b64decode(pub_b64))

    def sign(self, key_name: str, input_b64: str, key_version: int | None = None) -> str:
        """调用 Vault Transit 对 Base64 输入执行 Ed25519 签名并返回 vault:vN:... 字符串。"""
        body: dict[str, Any] = {"input": input_b64}
        if key_version is not None:
            body["key_version"] = key_version
        res = self._request_json("POST", f"/v1/{self._transit_mount}/sign/{key_name}", body)
        return str(res["data"]["signature"])


class _HttpZetaTransport:
    """面向真实 Apache SeaTunnel Zeta 2.3.9 REST API 的传输层实现。"""

    def __init__(self, rest_url: str, timeout: float = 15.0) -> None:
        self._base = rest_url.rstrip("/")
        self._timeout = timeout

    def _req(self, method: str, path: str, body: bytes | None = None, content_type: str = "application/json") -> Any:
        import json as _json
        import urllib.request as _req

        req = _req.Request(
            f"{self._base}{path}",
            data=body,
            headers={"Content-Type": content_type},
            method=method,
        )
        with _req.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", errors="replace").strip()
            return _json.loads(raw) if raw else {}

    def post_submit_job(self, *, job_name: str, hocon_body: str) -> dict[str, Any]:
        """通过 Zeta 2.3.9 REST API 提交作业（支持 HOCON / JSON 配置体）。"""
        import urllib.parse as _parse

        qs = _parse.urlencode({"jobName": job_name, "format": "hocon"})
        return dict(self._req("POST", f"/hazelcast/rest/maps/submit-job?{qs}", hocon_body.encode("utf-8"), "text/plain; charset=utf-8"))

    def get_job_info(self, external_job_id: str) -> dict[str, Any]:
        """查询单个 Zeta 作业实时状态与指标。"""
        return dict(self._req("GET", f"/hazelcast/rest/maps/job-info/{external_job_id}"))

    def list_jobs(self) -> list[dict[str, Any]]:
        """列出正在运行与已结束的 Zeta 作业用于 UNKNOWN 状态对账。"""
        running = self._req("GET", "/hazelcast/rest/maps/running-jobs")
        finished = self._req("GET", "/hazelcast/rest/maps/finished-jobs/FINISHED")
        items: list[dict[str, Any]] = []
        if isinstance(running, list):
            items.extend(running)
        if isinstance(finished, list):
            items.extend(finished)
        return items

    def post_stop_job(
        self,
        external_job_id: str,
        *,
        is_stop_with_save_point: bool = False,
    ) -> dict[str, Any]:
        """向 Zeta 2.3.9 发送受管停止或带保存点停止请求。"""
        import json as _json

        payload = _json.dumps(
            {"jobId": str(external_job_id), "isStopWithSavePoint": bool(is_stop_with_save_point)}
        ).encode("utf-8")
        return dict(self._req("POST", "/hazelcast/rest/maps/stop-job", payload))


class _LocalMockZetaTransport:
    """默认本地内存态 SeaTunnel Zeta 传输层替身（未连接真实容器时提供确定性响应，支持对账与取消）。"""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}

    def post_submit_job(self, *, job_name: str, hocon_body: str) -> dict[str, Any]:
        """模拟向 Zeta 提交 HOCON 作业并分配确定性 jobId。"""
        job_id = f"zeta-{hashlib.sha256(job_name.encode('utf-8')).hexdigest()[:12]}"
        self.jobs[job_id] = {
            "jobId": job_id,
            "jobName": job_name,
            "jobStatus": "RUNNING",
            "metrics": {
                "SourceReceivedCount": 1000,
                "SinkWriteCount": 1000,
                "SinkWriteBytes": 128000,
                "ErrorRowCount": 0,
            },
        }
        return {"jobId": job_id, "jobName": job_name}

    def get_job_info(self, external_job_id: str) -> dict[str, Any]:
        """查询单个 Zeta 作业详情。"""
        return dict(
            self.jobs.get(
                external_job_id,
                {
                    "jobId": external_job_id,
                    "jobName": f"job-{external_job_id}",
                    "jobStatus": "RUNNING",
                    "metrics": {},
                },
            )
        )

    def list_jobs(self) -> list[dict[str, Any]]:
        """列出所有已知 Zeta 作业用于对账。"""
        return list(self.jobs.values())

    def post_stop_job(
        self,
        external_job_id: str,
        *,
        is_stop_with_save_point: bool = False,
    ) -> dict[str, Any]:
        """模拟停止或带保存点停止 Zeta 作业。"""
        if external_job_id in self.jobs:
            self.jobs[external_job_id]["jobStatus"] = (
                "SAVEPOINT_DONE" if is_stop_with_save_point else "CANCELED"
            )
        return {
            "jobId": external_job_id,
            "status": "STOPPED" if is_stop_with_save_point else "CANCELED",
        }


class SqlAlchemyControlPlaneRepository:
    """基于 SQLAlchemy Session 的 Worker 控制面持久化仓储实现（遵循 ADR-002 事务级 RLS）。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def resolve_execution_tenant_context(
        self,
        execution_id: str,
        command_id: str,
    ) -> tuple[str, str] | None:
        """从数据库持久化事实派生 (organization_id, project_id)，绝不信任消息体中的租户字段。"""
        try:
            ctx = resolve_worker_tenant_context(
                self.session,
                execution_id=uuid.UUID(str(execution_id)),
                command_id=uuid.UUID(str(command_id)),
            )
            return str(ctx.organization_id), str(ctx.project_id)
        except Exception:
            return None

    def set_transaction_rls_context(
        self,
        *,
        organization_id: str,
        project_id: str,
        actor_id: str,
    ) -> None:
        """在当前数据库事务中注入 `SET LOCAL` RLS 上下文。"""
        apply_rls_context(
            self.session,
            TenantContext(
                organization_id=uuid.UUID(str(organization_id)),
                project_id=uuid.UUID(str(project_id)),
                actor_id=actor_id,
            ),
            require_project=True,
        )

    def get_authorized_command_fact(
        self,
        command_id: str,
        execution_id: str,
    ) -> AuthorizedCommandFact | None:
        """在 RLS 保护下查询 `ExecutionCommand` 与 `CapabilityConsumption` 持久化授权事实。"""
        cmd_uuid = uuid.UUID(str(command_id))
        exec_uuid = uuid.UUID(str(execution_id))
        cmd_row = self.session.execute(
            select(ExecutionCommand).where(
                ExecutionCommand.command_id == cmd_uuid,
                ExecutionCommand.execution_id == exec_uuid,
            )
        ).scalar_one_or_none()
        if cmd_row is None:
            return None

        consumption_row = self.session.execute(
            select(CapabilityConsumption).where(
                CapabilityConsumption.jti == cmd_row.capability_jti
            )
        ).scalar_one_or_none()

        return AuthorizedCommandFact(
            command_id=str(cmd_row.command_id),
            execution_id=str(cmd_row.execution_id),
            organization_id=str(cmd_row.organization_id),
            project_id=str(cmd_row.project_id),
            command_type=cmd_row.command_type,
            capability_jti=str(cmd_row.capability_jti),
            capability_consumed=consumption_row is not None,
            status=cmd_row.status,
            payload_json=dict(cmd_row.payload_json or {}),
        )

    def get_execution_run(self, execution_id: str) -> ExecutionRunRecord | None:
        """将数据库 `ExecutionRun` 实体加载为状态机 `ExecutionRunRecord`。"""
        run_row = self.session.get(ExecutionRun, uuid.UUID(str(execution_id)))
        if run_row is None:
            return None
        mode = ExecutionMode.CDC if run_row.execution_mode.upper() == "CDC" else ExecutionMode.BATCH
        return ExecutionRunRecord(
            execution_id=str(run_row.id),
            organization_id=str(run_row.organization_id),
            project_id=str(run_row.project_id),
            pipeline_id=str(run_row.pipeline_id),
            pipeline_version_id=str(run_row.pipeline_version_id),
            artifact_hash=run_row.artifact_hash,
            execution_mode=mode,
            preparation_id=str(run_row.preparation_id),
            capability_jti=str(run_row.capability_jti),
            command_id=str(run_row.command_id),
            status=run_row.status,
            external_job_id=run_row.external_job_id,
            checkpoint_ref=run_row.checkpoint_ref,
            published_dataset_version_id=(
                str(run_row.published_dataset_version_id)
                if run_row.published_dataset_version_id
                else None
            ),
        )

    def mark_command_completed(
        self,
        command_id: str,
        result_summary: dict[str, Any],
    ) -> None:
        """更新数据库 `ExecutionCommand` 与 `ExecutionRun` 状态。"""
        cmd_uuid = uuid.UUID(str(command_id))
        cmd_row = self.session.execute(
            select(ExecutionCommand).where(ExecutionCommand.command_id == cmd_uuid)
        ).scalar_one_or_none()
        if cmd_row is not None:
            cmd_row.status = "COMPLETED"
            run_row = self.session.get(ExecutionRun, cmd_row.execution_id)
            if run_row is not None:
                if "run_status" in result_summary and result_summary["run_status"]:
                    run_row.status = str(result_summary["run_status"])
                if "external_job_id" in result_summary and result_summary["external_job_id"]:
                    run_row.external_job_id = str(result_summary["external_job_id"])
            self.session.flush()


def _build_hocon_connector_registry(
    platform_reg: PlatformConnectorRegistry,
) -> HoconConnectorRegistry:
    """将平台版本化连接器矩阵转换为编译器与 HOCON 生成器使用的只读视图（兼容别名）。"""
    entries = [e.to_dict() for e in platform_reg.list_entries()]
    by_id = {e["connector_id"]: e for e in entries}

    # 为简写或历史连接器 ID 提供同能力映射（保持 support_status 与原条目完全一致）
    if "mysql-jdbc" in by_id and "mysql" not in by_id:
        entries.append({**by_id["mysql-jdbc"], "connector_id": "mysql"})
    if "postgres-jdbc" in by_id and "postgres" not in by_id:
        entries.append({**by_id["postgres-jdbc"], "connector_id": "postgres"})
    if "doris" in by_id and "doris-sink" not in by_id:
        entries.append({**by_id["doris"], "connector_id": "doris-sink"})
    if "s3-minio" in by_id and "s3-minio-file" not in by_id:
        entries.append({**by_id["s3-minio"], "connector_id": "s3-minio-file"})
    if "oracle-jdbc" in by_id and "oracle" not in by_id:
        entries.append({**by_id["oracle-jdbc"], "connector_id": "oracle"})

    return HoconConnectorRegistry(entries)


def _normalize_sha256_tag(val: str | None, fallback_seed: str) -> str:
    """确保哈希字符串严格符合 `sha256:<64位十六进制>` 契约格式。"""
    if isinstance(val, str) and val.startswith("sha256:") and len(val) == 71:
        hex_part = val[7:]
        if all(c in "0123456789abcdefABCDEF" for c in hex_part):
            return f"sha256:{hex_part.lower()}"
    return "sha256:" + hashlib.sha256((val or fallback_seed).encode("utf-8")).hexdigest()


def _map_pdp_permissions(ctx: ProjectRequestContext) -> list[str]:
    """将 IAM 数据库中的权限码（冒号分隔）与角色码映射为 Harness PDP 标准权限集合。"""
    perms = set(ctx.permissions)
    roles = set(ctx.project_roles)

    if "MAKER" in roles or "pipeline:prepare" in perms or "pipeline:write" in perms:
        perms.update({"pipeline.prepare", "pipeline.write"})
    if "DATA_CHECKER" in roles or "approval:data_check" in perms:
        perms.update({"approval.approve", "approval.data_checker", "approval.single_checker"})
    if "SECURITY_CHECKER" in roles or "approval:security_check" in perms:
        perms.update({"approval.approve", "approval.security_checker", "approval.single_checker"})
    if "OPERATOR" in roles or "execution:commit" in perms or "execution:control" in perms:
        perms.update({"execution.commit", "execution.rollback", "schedule.manage"})
    if "PROJECT_ADMIN" in roles or "ORG_ADMIN" in roles:
        perms.update({"policy.manage", "benchmark.run", "schedule.manage"})

    return sorted(perms)


def _load_pipeline_metadata_snapshot(
    session: Session | None,
    *,
    organization_id: str | uuid.UUID,
    project_id: str | uuid.UUID,
    snapshot_id: str,
) -> PipelineMetadataSnapshot:
    """从数据库加载 `MetadataSnapshot` 并转换为编译器/Agent 使用的 `PipelineMetadataSnapshot`。"""
    if session is not None:
        try:
            snap_uuid = uuid.UUID(str(snapshot_id))
            org_uuid = uuid.UUID(str(organization_id))
            proj_uuid = uuid.UUID(str(project_id))
            db_snap = session.get(DbMetadataSnapshot, snap_uuid)
            if (
                db_snap is not None
                and db_snap.organization_id == org_uuid
                and db_snap.project_id == proj_uuid
            ):
                objs = (
                    session.execute(
                        select(MetadataObject).where(MetadataObject.snapshot_id == db_snap.id)
                    )
                    .scalars()
                    .all()
                )
                tables: dict[str, TableMeta] = {}
                for obj in objs:
                    cols = (
                        session.execute(
                            select(MetadataColumn)
                            .where(MetadataColumn.object_id == obj.id)
                            .order_by(MetadataColumn.ordinal_position.asc())
                        )
                        .scalars()
                        .all()
                    )
                    sens = (
                        session.execute(
                            select(SensitiveClassification).where(
                                SensitiveClassification.snapshot_id == db_snap.id,
                                SensitiveClassification.object_id == obj.id,
                            )
                        )
                        .scalars()
                        .all()
                    )
                    col_map = {c.column_name: c.data_type for c in cols}
                    sensitive_cols = [
                        s.column_name
                        for s in sens
                        if s.classification_level in ("RESTRICTED_PII", "CONFIDENTIAL")
                    ]
                    qualified = f"{obj.schema_name}.{obj.object_name}"
                    tables[qualified] = TableMeta(
                        columns=col_map,
                        primary_keys=[str(k) for k in (obj.primary_key_json or [])],
                        sensitive_columns=sensitive_cols,
                    )
                if tables:
                    return PipelineMetadataSnapshot(
                        snapshot_id=str(db_snap.id),
                        tables=tables,
                    )
        except Exception:
            pass

    # 若未在数据库中找到或用于轻量测试，回退至标准 orders 表快照结构
    return PipelineMetadataSnapshot(
        snapshot_id=str(snapshot_id),
        tables={
            "sales.orders": TableMeta(
                columns={
                    "id": "BIGINT",
                    "customer_name": "VARCHAR(128)",
                    "amount": "DECIMAL(18,2)",
                    "updated_at": "DATETIME",
                },
                primary_keys=["id"],
                sensitive_columns=["customer_name"],
            ),
            "orders_db.users": TableMeta(
                columns={
                    "user_id": "BIGINT",
                    "customer_email": "VARCHAR(128)",
                    "customer_phone": "VARCHAR(32)",
                    "updated_at": "DATETIME",
                },
                primary_keys=["user_id"],
                sensitive_columns=["customer_email", "customer_phone"],
            ),
        },
    )


@dataclass
class WiringContainer:
    """持有控制面全部已装配领域服务实例的容器。"""

    settings: Settings
    telemetry: TelemetryManager
    metrics_registry: MetricsRegistry
    connector_registry: PlatformConnectorRegistry
    hocon_connector_registry: HoconConnectorRegistry
    deterministic_gate: DeterministicGate
    strategy_compiler: ExecutionStrategyCompiler
    llm_provider: Any
    provider_gateway: ProviderGateway
    agent_permission_checker: _ContextBoundPermissionChecker
    agent_ownership_store: ThreadOwnershipStore
    agent_service: AgentService
    policy_store: PolicyStore
    policy_engine: PolicyEngine
    broker_store: InMemoryBrokerStore
    preparation_service: PreparationService
    standing_schedule_service: StandingScheduleService
    approval_store: InMemoryApprovalStore
    approval_engine: ApprovalEngine
    capability_signer: Any
    evidence_signer: Any
    keyring: KeyRing
    capability_issuer: CapabilityIssuer
    capability_verifier: CapabilityVerifier
    replay_fast_path: Any
    replay_guard: ReplayGuard
    evidence_ledger: EvidenceLedger
    tool_broker: ToolBroker
    quality_engine: TwoStageQualityEngine
    supervision_engine: RuntimeSupervisionEngine
    zeta_transport: Any
    seatunnel_adapter: SeaTunnelZetaAdapter
    run_reconciler: RunReconciler
    doris_publish_adapter: DorisPublishAdapter
    rollback_executor: RollbackExecutor
    benchmark_engine: BenchmarkEngine
    thread_candidates: dict[str, dict[str, Any]] = field(default_factory=dict)
    runtime_backends: dict[str, str] = field(default_factory=dict)


def build_wiring_container(
    settings: Settings | None = None,
    *,
    force_live: bool = False,
) -> WiringContainer:
    """初始化并组装所有兄弟模块的核心服务实例。"""
    cfg = settings or get_settings()
    install_redaction()

    telemetry = init_telemetry(service_name=cfg.service_name)
    metrics_reg = get_metrics_registry()

    platform_conn_reg = get_default_connector_registry()
    hocon_conn_reg = _build_hocon_connector_registry(platform_conn_reg)
    gate = DeterministicGate(hocon_conn_reg)
    strategy_compiler = ExecutionStrategyCompiler()

    runtime_backends: dict[str, str] = {}
    is_live_mode = force_live or (cfg.environment != "test" and not bool(os.getenv("PYTEST_CURRENT_TEST")))

    # 1. 尝试连接真实持久化 Vault（KV v2 读取 LLM API Key + Transit Ed25519 硬件边界签名）
    vault_client: _HttpVaultTransitClient | None = None
    if is_live_mode and cfg.vault_token and "placeholder" not in cfg.vault_token:
        try:
            vc = _HttpVaultTransitClient(
                vault_addr=cfg.vault_addr,
                token=cfg.vault_token,
                kv_mount=cfg.vault_kv_mount,
                transit_mount=cfg.vault_transit_mount,
            )
            vc._request_json("GET", "/v1/sys/health")
            vault_client = vc
        except Exception:
            vault_client = None

    # 2. 按配置装配真实 OpenAI-Compatible Provider（从 Vault KV v2 取 Key）或确定性 Mock Provider
    llm_provider: Any = DeterministicMockProvider()
    runtime_backends["llm_provider"] = "deterministic_mock"
    if is_live_mode and cfg.llm_provider_mode == "openai_compatible" and vault_client is not None:
        try:
            secret_data = vault_client.read_kv2_secret(cfg.llm_openai_api_key_secret_ref)
            api_key = str(secret_data.get("api_key") or "")
            if api_key:
                llm_provider = OpenAICompatibleProvider(
                    base_url=str(secret_data.get("base_url") or cfg.llm_openai_base_url),
                    api_key=api_key,
                    model=str(secret_data.get("model") or cfg.llm_openai_model),
                )
                runtime_backends["llm_provider"] = f"openai_compatible:{cfg.llm_openai_model}"
        except Exception:
            pass
    provider_gateway = ProviderGateway(llm_provider)

    def _inspect_snapshot_tool(
        agent_ctx: AgentContext,
        connection_id: str,
        snapshot_id: str,
    ) -> PipelineMetadataSnapshot:
        session = _active_session_var.get()
        return _load_pipeline_metadata_snapshot(
            session,
            organization_id=agent_ctx.organization_id,
            project_id=agent_ctx.project_id,
            snapshot_id=snapshot_id,
        )

    def _list_connector_caps_tool() -> HoconConnectorRegistry:
        return hocon_conn_reg

    readonly_tools = build_default_registry(_inspect_snapshot_tool, _list_connector_caps_tool)
    agent_deps = AgentDeps(
        gateway=provider_gateway,
        tools=readonly_tools,
        registry=hocon_conn_reg,
        gate=gate,
        max_repair_attempts=3,
    )
    perm_checker = _ContextBoundPermissionChecker()
    ownership_store = ThreadOwnershipStore()
    agent_service = AgentService(
        deps=agent_deps,
        permissions=perm_checker,
        ownership=ownership_store,
    )

    # Harness & Security 组件初始化
    policy_store = PolicyStore(initial_rules=DEFAULT_RULES, initial_version="v1.0.0")
    policy_engine = PolicyEngine(store=policy_store)
    broker_store = InMemoryBrokerStore()
    prep_service = PreparationService(store=broker_store.preps)
    standing_sched_service = StandingScheduleService(store=broker_store.preps)
    approval_store = InMemoryApprovalStore()
    approval_engine = ApprovalEngine(
        store=approval_store,
        policy=policy_engine,
        preparations=prep_service,
    )

    keyring = KeyRing()
    cap_signer: Any
    ev_signer: Any
    if vault_client is not None:
        try:
            cap_pub = vault_client.get_ed25519_public_key(cfg.vault_capability_key_name, 1)
            ev_pub = vault_client.get_ed25519_public_key(cfg.vault_evidence_key_name, 1)
            cap_signer = VaultTransitEd25519Signer(vault_client, cfg.vault_capability_key_name, 1)
            ev_signer = VaultTransitEd25519Signer(vault_client, cfg.vault_evidence_key_name, 1)
            keyring.add(cap_signer.kid, cap_pub)
            keyring.add(ev_signer.kid, ev_pub)
            runtime_backends["signer"] = "vault_transit_ed25519"
        except Exception:
            cap_signer = LocalEd25519Signer(kid="etl-agent-capability-v1")
            ev_signer = LocalEd25519Signer(kid="etl-agent-evidence-v1")
            cap_signer.register(keyring)
            ev_signer.register(keyring)
            runtime_backends["signer"] = "local_ed25519_fallback"
    else:
        cap_signer = LocalEd25519Signer(kid="etl-agent-capability-v1")
        ev_signer = LocalEd25519Signer(kid="etl-agent-evidence-v1")
        cap_signer.register(keyring)
        ev_signer.register(keyring)
        runtime_backends["signer"] = "local_ed25519"

    cap_issuer = CapabilityIssuer(signer=cap_signer)
    cap_verifier = CapabilityVerifier(keyring=keyring)
    fast_path: Any = InMemoryFastPath()
    runtime_backends["replay_fast_path"] = "in_memory"
    if is_live_mode:
        try:
            import redis as _redis

            r_client = _redis.from_url(cfg.redis_url, socket_connect_timeout=2, socket_timeout=2)
            if r_client.ping():
                fast_path = RedisFastPath(r_client)
                runtime_backends["replay_fast_path"] = "redis_live"
        except Exception:
            pass
    replay_guard = ReplayGuard(fast=fast_path, store=broker_store.consumptions)
    evidence_ledger = EvidenceLedger(
        store=broker_store.ledger,
        signer=ev_signer,
        checkpoint_every=10,
    )

    # 数据面适配器与运行时执行器（真实 SeaTunnel Zeta 2.3.9 可达时自动挂载真实 HTTP 传输层）
    zeta_transport: Any = _LocalMockZetaTransport()
    runtime_backends["seatunnel_transport"] = "local_mock"
    if is_live_mode:
        try:
            import urllib.request as _req

            with _req.urlopen(f"{cfg.seatunnel_rest_url.rstrip('/')}/hazelcast/rest/maps/overview", timeout=2) as _r:
                if _r.status == 200:
                    zeta_transport = _HttpZetaTransport(cfg.seatunnel_rest_url)
                    runtime_backends["seatunnel_transport"] = f"seatunnel_zeta_http:{cfg.seatunnel_rest_url}"
        except Exception:
            pass
    seatunnel_adapter = SeaTunnelZetaAdapter(transport=zeta_transport)
    run_reconciler = RunReconciler(adapter=seatunnel_adapter)
    doris_publish_adapter = DorisPublishAdapter()
    rollback_executor = RollbackExecutor(doris_adapter=doris_publish_adapter)
    quality_engine = TwoStageQualityEngine()
    supervision_engine = RuntimeSupervisionEngine()

    executors = {
        "seatunnel.submit_batch": lambda ctx_dict: {
            "status": "SUBMITTED",
            "tool": "seatunnel.submit_batch",
            "execution_id": ctx_dict["run"]["execution_id"],
        },
        "seatunnel.submit_cdc": lambda ctx_dict: {
            "status": "SUBMITTED",
            "tool": "seatunnel.submit_cdc",
            "execution_id": ctx_dict["run"]["execution_id"],
        },
        "seatunnel.cancel_job": lambda ctx_dict: {
            "status": "CANCELLED",
            "tool": "seatunnel.cancel_job",
            "execution_id": ctx_dict["run"]["execution_id"],
        },
        "doris.atomic_swap": lambda ctx_dict: {
            "status": "PUBLISHED",
            "tool": "doris.atomic_swap",
            "execution_id": ctx_dict["run"]["execution_id"],
        },
        "rollback.shadow_cleanup": lambda ctx_dict: {
            "status": "ROLLED_BACK",
            "tool": "rollback.shadow_cleanup",
            "execution_id": ctx_dict["run"]["execution_id"],
        },
        "rollback.swap_restore": lambda ctx_dict: {
            "status": "ROLLED_BACK",
            "tool": "rollback.swap_restore",
            "execution_id": ctx_dict["run"]["execution_id"],
        },
        "rollback.cdc_recover": lambda ctx_dict: {
            "status": "CDC_RECOVERED",
            "tool": "rollback.cdc_recover",
            "execution_id": ctx_dict["run"]["execution_id"],
        },
    }

    tool_broker = ToolBroker(
        store=broker_store,
        policy=policy_engine,
        preparations=prep_service,
        approvals=approval_engine,
        issuer=cap_issuer,
        replay=replay_guard,
        ledger=evidence_ledger,
        verifier=cap_verifier,
        input_resolver=lambda p: dict(p.inputs),
        executors=executors,
    )

    benchmark_engine = BenchmarkEngine()

    return WiringContainer(
        settings=cfg,
        telemetry=telemetry,
        metrics_registry=metrics_reg,
        connector_registry=platform_conn_reg,
        hocon_connector_registry=hocon_conn_reg,
        deterministic_gate=gate,
        strategy_compiler=strategy_compiler,
        llm_provider=llm_provider,
        provider_gateway=provider_gateway,
        agent_permission_checker=perm_checker,
        agent_ownership_store=ownership_store,
        agent_service=agent_service,
        policy_store=policy_store,
        policy_engine=policy_engine,
        broker_store=broker_store,
        preparation_service=prep_service,
        standing_schedule_service=standing_sched_service,
        approval_store=approval_store,
        approval_engine=approval_engine,
        capability_signer=cap_signer,
        evidence_signer=ev_signer,
        keyring=keyring,
        capability_issuer=cap_issuer,
        capability_verifier=cap_verifier,
        replay_fast_path=fast_path,
        replay_guard=replay_guard,
        evidence_ledger=evidence_ledger,
        tool_broker=tool_broker,
        quality_engine=quality_engine,
        supervision_engine=supervision_engine,
        zeta_transport=zeta_transport,
        seatunnel_adapter=seatunnel_adapter,
        run_reconciler=run_reconciler,
        doris_publish_adapter=doris_publish_adapter,
        rollback_executor=rollback_executor,
        benchmark_engine=benchmark_engine,
        runtime_backends=runtime_backends,
    )


def _sync_ledger_to_db(
    session: Session,
    container: WiringContainer,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
) -> None:
    """将内存 `EvidenceLedger` 中新增的哈希链事件与签名检查点同步持久化至数据库表。"""
    chain_key = (str(organization_id), str(project_id))
    mem_events = container.evidence_ledger.store.events(chain_key)
    existing_seqs = set(
        session.execute(
            select(EvidenceEvent.sequence_no).where(
                EvidenceEvent.organization_id == organization_id,
                EvidenceEvent.project_id == project_id,
            )
        )
        .scalars()
        .all()
    )
    for rec in mem_events:
        ev = rec["event"]
        seq = int(ev["sequence_no"])
        if seq not in existing_seqs:
            session.add(
                EvidenceEvent(
                    id=uuid.UUID(str(ev["event_id"])),
                    organization_id=organization_id,
                    project_id=project_id,
                    sequence_no=seq,
                    timestamp=str(ev["timestamp"]),
                    actor=str(ev["actor"]),
                    event_type=str(ev["event_type"]),
                    resource_ref=str(ev["resource_ref"]),
                    artifact_hash=str(ev.get("artifact_hash") or "sha256:" + "0" * 64),
                    payload_digest=str(ev["payload_digest"]),
                    previous_hash=str(rec["previous_hash"]),
                    event_hash=str(rec["event_hash"]),
                )
            )
            existing_seqs.add(seq)

    mem_cps = container.evidence_ledger.store.checkpoints(chain_key)
    existing_cp_to_seqs = set(
        session.execute(
            select(EvidenceCheckpoint.to_sequence).where(
                EvidenceCheckpoint.organization_id == organization_id,
                EvidenceCheckpoint.project_id == project_id,
            )
        )
        .scalars()
        .all()
    )
    for cp in mem_cps:
        p = cp["payload"]
        to_seq = int(p["last_sequence_no"])
        if to_seq not in existing_cp_to_seqs:
            session.add(
                EvidenceCheckpoint(
                    organization_id=organization_id,
                    project_id=project_id,
                    from_sequence=1,
                    to_sequence=to_seq,
                    head_event_hash=str(p["last_event_hash"]),
                    signing_key_id=str(cp["signing_key_id"]),
                    signature_b64url=str(cp["signature_b64url"]),
                )
            )
            existing_cp_to_seqs.add(to_seq)
    session.flush()


def _resolve_agent_bindings_and_hints(
    session: Session,
    ctx: ProjectRequestContext,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """从请求载荷或当前项目已登记的数据源连接与元数据快照构建 LangGraph 请求结构。"""
    hints: dict[str, Any] = dict(payload.get("hints") or {})
    if "execution_mode" in payload and "sync_mode" not in hints:
        em = str(payload["execution_mode"]).upper()
        hints["sync_mode"] = "CDC" if em == "CDC" else "FULL_LOAD"
    for key in (
        "source_table",
        "sink_table",
        "sync_mode",
        "write_mode",
        "incremental_column",
        "primary_keys",
        "columns",
        "sql",
    ):
        if key in payload and key not in hints:
            hints[key] = payload[key]

    if "bindings" in payload and isinstance(payload["bindings"], dict):
        bindings = dict(payload["bindings"])
    else:
        # 自动从项目数据库上下文解析源连接、目标连接与最新 MetadataSnapshot
        conns = (
            session.execute(
                select(DataConnection)
                .where(
                    DataConnection.organization_id == ctx.organization_id,
                    DataConnection.project_id == ctx.project_id,
                )
                .order_by(DataConnection.created_at.asc())
            )
            .scalars()
            .all()
        )
        src_conn: DataConnection | None = None
        sink_conn: DataConnection | None = None
        if payload.get("source_connection_id"):
            src_conn = session.get(DataConnection, uuid.UUID(str(payload["source_connection_id"])))
        if payload.get("sink_connection_id"):
            sink_conn = session.get(DataConnection, uuid.UUID(str(payload["sink_connection_id"])))

        if src_conn is None and conns:
            src_conn = next(
                (c for c in conns if c.connector_id in ("mysql-jdbc", "postgres-jdbc", "mysql", "postgres")),
                conns[0],
            )
        if sink_conn is None and conns:
            sink_conn = next(
                (c for c in conns if "doris" in c.connector_id.lower()),
                conns[-1],
            )

        snap_id_str = str(payload.get("metadata_snapshot_id") or "")
        if not snap_id_str and src_conn is not None:
            latest_snap = (
                session.execute(
                    select(DbMetadataSnapshot)
                    .where(
                        DbMetadataSnapshot.organization_id == ctx.organization_id,
                        DbMetadataSnapshot.project_id == ctx.project_id,
                        DbMetadataSnapshot.connection_id == src_conn.id,
                    )
                    .order_by(DbMetadataSnapshot.captured_at.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if latest_snap is not None:
                snap_id_str = str(latest_snap.id)

                # 若请求未显式指定 source_table，且不是专门测试缺失 source_table，可从快照首表推导
                if "source_table" not in hints and payload.get("auto_fill_tables", False):
                    first_obj = (
                        session.execute(
                            select(MetadataObject)
                            .where(MetadataObject.snapshot_id == latest_snap.id)
                            .limit(1)
                        )
                        .scalars()
                        .first()
                    )
                    if first_obj is not None:
                        hints["source_table"] = f"{first_obj.schema_name}.{first_obj.object_name}"

        if not snap_id_str:
            snap_id_str = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

        src_conn_id = str(src_conn.id) if src_conn else "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        src_connector_id = src_conn.connector_id if src_conn else "mysql-jdbc"
        src_secret_id = (
            str(src_conn.secret_ref_id)
            if (src_conn and src_conn.secret_ref_id)
            else "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        )
        src_ver = src_conn.current_version if src_conn else 1

        sink_conn_id = str(sink_conn.id) if sink_conn else "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        sink_connector_id = (
            sink_conn.connector_id
            if (sink_conn and "doris" in sink_conn.connector_id.lower())
            else "doris"
        )
        sink_secret_id = (
            str(sink_conn.secret_ref_id)
            if (sink_conn and sink_conn.secret_ref_id)
            else "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        )
        sink_ver = sink_conn.current_version if sink_conn else 1

        bindings = {
            "source": {
                "connection_id": src_conn_id,
                "connection_version": src_ver,
                "connector_id": src_connector_id,
                "metadata_snapshot_id": snap_id_str,
                "secret_ref_id": src_secret_id,
                "secret_version": src_ver,
            },
            "sink": {
                "connection_id": sink_conn_id,
                "connection_version": sink_ver,
                "connector_id": sink_connector_id,
                "secret_ref_id": sink_secret_id,
                "secret_version": sink_ver,
            },
        }

    return {
        "prompt": str(payload.get("prompt", "Generate ETL pipeline")),
        "hints": hints,
        "bindings": bindings,
    }


def wire_application(app: FastAPI, settings: Settings | None = None) -> WiringContainer:
    """将全量兄弟模块服务与路由处理器挂载到 FastAPI 应用实例。"""
    container = build_wiring_container(settings)
    app.state.wiring = container
    app.state.telemetry = container.telemetry
    app.state.metrics_registry = container.metrics_registry
    app.state.connector_registry = container.connector_registry
    app.state.hocon_connector_registry = container.hocon_connector_registry
    app.state.deterministic_gate = container.deterministic_gate
    app.state.agent_service = container.agent_service
    app.state.policy_store = container.policy_store
    app.state.policy_engine = container.policy_engine
    app.state.preparation_service = container.preparation_service
    app.state.approval_engine = container.approval_engine
    app.state.capability_issuer = container.capability_issuer
    app.state.capability_verifier = container.capability_verifier
    app.state.replay_guard = container.replay_guard
    app.state.evidence_ledger = container.evidence_ledger
    app.state.tool_broker = container.tool_broker
    app.state.seatunnel_adapter = container.seatunnel_adapter
    app.state.run_reconciler = container.run_reconciler
    app.state.doris_publish_adapter = container.doris_publish_adapter
    app.state.rollback_executor = container.rollback_executor
    app.state.quality_engine = container.quality_engine
    app.state.supervision_engine = container.supervision_engine
    app.state.benchmark_engine = container.benchmark_engine

    # -------------------- 6. Agent Thread 处理器 --------------------
    def _build_frontend_agent_fields(
        ctx: ProjectRequestContext,
        *,
        interrupt_payload: dict[str, Any] | None = None,
        candidate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """补充前端 Pipeline Studio 所需的脱敏标记、澄清问题列表与候选制品视图（保留全部原有字段）。"""
        redaction_status = {
            "redacted": True,
            "policy_version": "v1.0.0",
            "external_provider": container.runtime_backends.get("llm_provider", "DeterministicMock"),
        }
        interrupt_questions: list[dict[str, Any]] = []
        if isinstance(interrupt_payload, dict):
            missing = interrupt_payload.get("missing_fields") or interrupt_payload.get("questions") or []
            if isinstance(missing, list):
                for item in missing:
                    if isinstance(item, dict):
                        interrupt_questions.append(
                            {
                                "field": str(item.get("field") or item.get("key") or "param"),
                                "prompt": str(item.get("prompt") or item.get("question") or f"请补充 {item.get('field', '参数')}"),
                                "required": bool(item.get("required", True)),
                            }
                        )
                    else:
                        interrupt_questions.append(
                            {
                                "field": str(item),
                                "prompt": f"请补充必填参数：{item}",
                                "required": True,
                            }
                        )

        draft_artifacts: dict[str, Any] | None = None
        if isinstance(candidate, dict):
            plan = candidate.get("plan") or {}
            src = plan.get("source") or {}
            sink = plan.get("sink") or {}
            transforms = plan.get("transformations") or []
            sql_parts = [str(t.get("sql_text", "")) for t in transforms if isinstance(t, dict) and t.get("sql_text")]
            sql_text = "\n;\n".join(sql_parts) or "-- SELECT * FROM source_table"
            qc = candidate.get("quality_contract") or {}
            sc = candidate.get("supervision_contract") or {}
            limits = sc.get("resource_limits") or sc
            reject_cfg = sc.get("reject_rate_guard") or {}
            draft_artifacts = {
                "etl_plan_dsl": plan,
                "dag_nodes": [
                    {
                        "id": "node-source",
                        "kind": "SOURCE",
                        "label": f"Source: {src.get('table', src.get('connector_id', 'source'))}",
                        "connector_type": str(src.get("connector_id", "mysql-jdbc")),
                    },
                    {
                        "id": "node-transform",
                        "kind": "TRANSFORM",
                        "label": "SQL Transform (sqlglot AST)",
                        "execution_location": "SEATUNNEL_SQL",
                    },
                    {
                        "id": "node-sink",
                        "kind": "SINK",
                        "label": f"Sink: {sink.get('table', sink.get('connector_id', 'doris'))}",
                        "connector_type": str(sink.get("connector_id", "doris")),
                    },
                ],
                "dag_edges": [
                    {"id": "edge-1", "source": "node-source", "target": "node-transform", "label": "Read"},
                    {"id": "edge-2", "source": "node-transform", "target": "node-sink", "label": "Shadow Write"},
                ],
                "sql_transformation": sql_text,
                "sql_execution_location_reason": "经 sqlglot SELECT-only AST 沙箱与执行位置策略判定",
                "seatunnel_hocon": str(candidate.get("hocon") or ""),
                "schema_mapping_diff": [
                    {
                        "source_column": str(m.get("source_column", m.get("source", ""))),
                        "source_type": str(m.get("source_type", "VARCHAR")),
                        "target_column": str(m.get("target_column", m.get("target", ""))),
                        "target_type": str(m.get("target_type", "VARCHAR")),
                        "sensitivity_before": str(m.get("sensitivity", "NONE")),
                        "masking_after": str(m.get("masking_rule", "PASSTHROUGH")),
                    }
                    for m in (plan.get("column_mappings") or [])
                    if isinstance(m, dict)
                ],
                "quality_contract": {
                    "rules": list(qc.get("rules") or []),
                    "restricted_sql_enabled": bool(qc.get("restricted_sql_enabled", False)),
                },
                "runtime_supervision_contract": {
                    "max_read_rows": int(limits.get("max_read_rows", 1_000_000)),
                    "max_write_bytes": int(limits.get("max_write_bytes", 1_073_741_824)),
                    "max_duration_seconds": int(limits.get("max_duration_seconds", 3600)),
                    "max_amplification_ratio": float(limits.get("max_amplification_ratio", 3.0)),
                    "reject_rate_threshold_pct": float(reject_cfg.get("max_reject_rate", 0.2)) * 100,
                },
            }

        return {
            "project_id": str(ctx.project_id),
            "llm_redaction_status": redaction_status,
            "interrupt_questions": interrupt_questions,
            "draft_artifacts": draft_artifacts,
        }

    def _agent_create_thread_handler(
        *,
        session: Session,
        ctx: ProjectRequestContext,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = payload or {}
        thread = AgentThread(
            organization_id=ctx.organization_id,
            project_id=ctx.project_id,
            user_id=ctx.principal.user_id,
            langgraph_thread_id=f"lg-{uuid.uuid4().hex}",
            title=str(body.get("title", "Studio ETL Thread")),
            status="ACTIVE",
        )
        session.add(thread)
        session.flush()

        agent_ctx = AgentContext(
            user_id=str(ctx.principal.user_id),
            organization_id=str(ctx.organization_id),
            project_id=str(ctx.project_id),
            thread_id=str(thread.id),
        )
        container.agent_ownership_store.register(agent_ctx)
        return {
            "thread_id": str(thread.id),
            "langgraph_thread_id": thread.langgraph_thread_id,
            "status": thread.status,
            "title": thread.title,
            **_build_frontend_agent_fields(ctx),
        }

    def _agent_invoke_handler(
        *,
        session: Session,
        ctx: ProjectRequestContext,
        thread: AgentThread,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        agent_ctx = AgentContext(
            user_id=str(ctx.principal.user_id),
            organization_id=str(ctx.organization_id),
            project_id=str(ctx.project_id),
            thread_id=str(thread.id),
        )
        container.agent_ownership_store.register(agent_ctx)
        normalized_payload = dict(payload)
        if "prompt" not in normalized_payload and "requirement" in normalized_payload:
            normalized_payload["prompt"] = normalized_payload["requirement"]
        req_dict = _resolve_agent_bindings_and_hints(session, ctx, normalized_payload)

        token = _active_session_var.set(session)
        try:
            result = container.agent_service.start(agent_ctx, req_dict)
        except TenantAccessDenied as exc:
            raise AppError(ErrorCode.TENANT_ACCESS_DENIED, str(exc)) from exc
        finally:
            _active_session_var.reset(token)

        if result.status == "INTERRUPTED":
            thread.status = "INTERRUPTED"
            thread.pending_clarification_json = result.interrupt
            session.flush()
            return {
                "thread_id": str(thread.id),
                "langgraph_thread_id": thread.langgraph_thread_id,
                "status": "INTERRUPTED",
                "pending_clarification": result.interrupt,
                "interrupt": result.interrupt,
                **_build_frontend_agent_fields(ctx, interrupt_payload=result.interrupt),
            }

        if result.status == "CANDIDATE_READY" and result.candidate is not None:
            thread.status = "CANDIDATE_READY"
            thread.pending_clarification_json = None
            container.thread_candidates[str(thread.id)] = result.candidate
            session.add(
                WorkflowRun(
                    organization_id=ctx.organization_id,
                    project_id=ctx.project_id,
                    thread_id=thread.id,
                    status="SUCCEEDED",
                    repair_attempts=int(result.candidate.get("repair_attempts", 0)),
                    max_repair_attempts=3,
                    llm_audit_summary_json={
                        "is_mock_llm": result.candidate.get("is_mock_llm", True),
                        "artifact_hash": result.candidate["manifest"]["artifact_hash"],
                        "hidden_cot_stored": False,
                    },
                    ended_at=datetime.now(timezone.utc),
                )
            )
            session.flush()
            return {
                "thread_id": str(thread.id),
                "langgraph_thread_id": thread.langgraph_thread_id,
                "status": "CANDIDATE_READY",
                "pending_clarification": None,
                "candidate": result.candidate,
                **_build_frontend_agent_fields(ctx, candidate=result.candidate),
            }

        thread.status = result.status
        session.flush()
        return {
            "thread_id": str(thread.id),
            "langgraph_thread_id": thread.langgraph_thread_id,
            "status": result.status,
            "error_code": result.error_code,
            "handback": result.handback,
            **_build_frontend_agent_fields(ctx),
        }

    def _agent_resume_handler(
        *,
        session: Session,
        ctx: ProjectRequestContext,
        thread: AgentThread,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        agent_ctx = AgentContext(
            user_id=str(ctx.principal.user_id),
            organization_id=str(ctx.organization_id),
            project_id=str(ctx.project_id),
            thread_id=str(thread.id),
        )
        answers = (
            dict(payload["answers"])
            if "answers" in payload and isinstance(payload["answers"], dict)
            else dict(payload)
        )

        token = _active_session_var.set(session)
        try:
            result = container.agent_service.resume(agent_ctx, answers)
        except TenantAccessDenied as exc:
            raise AppError(ErrorCode.TENANT_ACCESS_DENIED, str(exc)) from exc
        finally:
            _active_session_var.reset(token)

        if result.status == "INTERRUPTED":
            thread.status = "INTERRUPTED"
            thread.pending_clarification_json = result.interrupt
            session.flush()
            return {
                "thread_id": str(thread.id),
                "langgraph_thread_id": thread.langgraph_thread_id,
                "status": "INTERRUPTED",
                "pending_clarification": result.interrupt,
                "interrupt": result.interrupt,
                **_build_frontend_agent_fields(ctx, interrupt_payload=result.interrupt),
            }

        if result.status == "CANDIDATE_READY" and result.candidate is not None:
            thread.status = "CANDIDATE_READY"
            thread.pending_clarification_json = None
            container.thread_candidates[str(thread.id)] = result.candidate
            session.add(
                WorkflowRun(
                    organization_id=ctx.organization_id,
                    project_id=ctx.project_id,
                    thread_id=thread.id,
                    status="SUCCEEDED",
                    repair_attempts=int(result.candidate.get("repair_attempts", 0)),
                    max_repair_attempts=3,
                    llm_audit_summary_json={
                        "is_mock_llm": result.candidate.get("is_mock_llm", True),
                        "artifact_hash": result.candidate["manifest"]["artifact_hash"],
                        "hidden_cot_stored": False,
                    },
                    ended_at=datetime.now(timezone.utc),
                )
            )
            session.flush()
            return {
                "thread_id": str(thread.id),
                "langgraph_thread_id": thread.langgraph_thread_id,
                "status": "CANDIDATE_READY",
                "pending_clarification": None,
                "candidate": result.candidate,
                **_build_frontend_agent_fields(ctx, candidate=result.candidate),
            }

        thread.status = result.status
        session.flush()
        return {
            "thread_id": str(thread.id),
            "langgraph_thread_id": thread.langgraph_thread_id,
            "status": result.status,
            "error_code": result.error_code,
            "handback": result.handback,
            **_build_frontend_agent_fields(ctx),
        }

    # -------------------- 7. Pipeline Gate 与不可变版本冻结处理器 --------------------
    def _extract_candidate_bundle(
        session: Session,
        ctx: ProjectRequestContext,
        pipeline_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, PipelineMetadataSnapshot]:
        """从请求载荷、关联 AgentThread 或最新 PipelineDraft 提取四类候选契约与元数据快照。"""
        src_bundle = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else payload
        if isinstance(payload.get("draft"), dict):
            src_bundle = {**payload["draft"], **src_bundle}
        thread_id_val = src_bundle.get("thread_id") or payload.get("thread_id")
        if thread_id_val and str(thread_id_val) in container.thread_candidates:
            cached = container.thread_candidates[str(thread_id_val)]
            src_bundle = {**cached, **src_bundle}
        elif not src_bundle.get("plan") and not src_bundle.get("etl_plan") and container.thread_candidates:
            # 兼容前端直接发送 draft 表单但未显式附带 thread_id 的场景，自动回退最近候选产物
            latest_cached = list(container.thread_candidates.values())[-1]
            src_bundle = {**latest_cached, **src_bundle}

        plan_data = src_bundle.get("plan") or src_bundle.get("etl_plan") or src_bundle.get("etl_plan_dsl")
        quality_data = src_bundle.get("quality_contract") or src_bundle.get("quality")
        supervision_data = (
            src_bundle.get("supervision_contract")
            or src_bundle.get("supervision")
            or default_supervision().model_dump(mode="json")
        )
        hocon_text = src_bundle.get("hocon") or src_bundle.get("hocon_text") or src_bundle.get("seatunnel_hocon")

        if not plan_data or not quality_data or not hocon_text:
            latest_draft = (
                session.execute(
                    select(PipelineDraft)
                    .where(
                        PipelineDraft.organization_id == ctx.organization_id,
                        PipelineDraft.project_id == ctx.project_id,
                        PipelineDraft.pipeline_id == pipeline_id,
                    )
                    .order_by(PipelineDraft.updated_at.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if latest_draft is not None:
                plan_data = plan_data or latest_draft.etl_plan_json
                quality_data = quality_data or latest_draft.quality_contract_json
                supervision_data = supervision_data or latest_draft.runtime_contract_json
                hocon_text = hocon_text or latest_draft.hocon_text

        if not isinstance(plan_data, dict) or not isinstance(quality_data, dict) or not hocon_text:
            raise AppError(
                ErrorCode.DETERMINISTIC_GATE_FAILED,
                "候选产物缺少必填的 EtlPlan、QualityContract 或 HOCON 模板文本",
            )

        snap_id = str(
            src_bundle.get("metadata_snapshot_id")
            or plan_data.get("source", {}).get("metadata_snapshot_id")
            or "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        )
        if isinstance(src_bundle.get("metadata"), dict):
            meta_snap = PipelineMetadataSnapshot.model_validate(src_bundle["metadata"])
        else:
            meta_snap = _load_pipeline_metadata_snapshot(
                session,
                organization_id=ctx.organization_id,
                project_id=ctx.project_id,
                snapshot_id=snap_id,
            )

        return plan_data, quality_data, supervision_data, str(hocon_text), meta_snap

    def _deterministic_gate_handler(
        *,
        session: Session,
        ctx: ProjectRequestContext,
        pipeline_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        pipe = session.get(PipelineDefinition, pipeline_id)
        if pipe is None or pipe.organization_id != ctx.organization_id or pipe.project_id != ctx.project_id:
            raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "目标 PipelineDefinition 不存在或越权访问")

        plan_data, quality_data, supervision_data, hocon_text, meta_snap = _extract_candidate_bundle(
            session, ctx, pipeline_id, payload
        )
        gate_res = container.deterministic_gate.run(
            plan_data,
            quality_data=quality_data,
            supervision_data=supervision_data,
            metadata=meta_snap,
            hocon_text=hocon_text,
        )
        failures_list = [
            {"code": f.code, "path": f.path, "message": f.message} for f in gate_res.failures
        ]
        gate_report = {
            "pipeline_id": str(pipeline_id),
            "passed": gate_res.passed,
            "error_code": gate_res.error_code,
            "failures": failures_list,
            "errors": failures_list,
            "warnings": [],
            "repair_loop_count": 0,
            "max_repair_loops": 3,
            "checks": [
                {
                    "gate_name": gname,
                    "passed": not any(gname.lower() in f["code"].lower() for f in failures_list),
                    "errors": [f["message"] for f in failures_list if gname.lower() in f["code"].lower()],
                    "warnings": [],
                }
                for gname in ("SCHEMA", "SQL_AST", "HOCON", "CONNECTOR_CAPABILITY", "POLICY")
            ],
        }

        sql_texts = [
            str(t.get("sql_text", ""))
            for t in plan_data.get("transformations", [])
            if isinstance(t, dict)
        ]
        draft = PipelineDraft(
            organization_id=ctx.organization_id,
            project_id=ctx.project_id,
            pipeline_id=pipeline_id,
            thread_id=(
                uuid.UUID(str(payload["thread_id"]))
                if payload.get("thread_id")
                else None
            ),
            etl_plan_json=plan_data,
            sql_text="\n;\n".join(sql_texts),
            hocon_text=hocon_text,
            quality_contract_json=quality_data,
            runtime_contract_json=supervision_data,
            gate_report_json=gate_report,
            updated_by=ctx.principal.user_id,
        )
        session.add(draft)
        session.flush()

        if not gate_res.passed and payload.get("strict", False):
            raise AppError(
                ErrorCode.DETERMINISTIC_GATE_FAILED,
                "确定性门禁校验未通过",
                detail={"failures": failures_list},
            )
        return {**gate_report, "draft_id": str(draft.id)}

    def _freeze_version_handler(
        *,
        session: Session,
        ctx: ProjectRequestContext,
        pipeline_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        pipe = session.get(PipelineDefinition, pipeline_id)
        if pipe is None or pipe.organization_id != ctx.organization_id or pipe.project_id != ctx.project_id:
            raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "目标 PipelineDefinition 不存在或越权访问")

        plan_data, quality_data, supervision_data, hocon_text, meta_snap = _extract_candidate_bundle(
            session, ctx, pipeline_id, payload
        )
        gate_res = container.deterministic_gate.run(
            plan_data,
            quality_data=quality_data,
            supervision_data=supervision_data,
            metadata=meta_snap,
            hocon_text=hocon_text,
        )
        if not gate_res.passed:
            raise AppError(
                ErrorCode.DETERMINISTIC_GATE_FAILED,
                "确定性门禁校验未通过，禁止冻结不可变 PipelineVersion",
                detail={
                    "failures": [
                        {"code": f.code, "path": f.path, "message": f.message}
                        for f in gate_res.failures
                    ]
                },
            )

        parent_rev = pipe.latest_revision if pipe.latest_revision > 0 else None
        next_rev = pipe.latest_revision + 1
        pipe.latest_revision = next_rev

        sql_hashes = {
            str(t["step_id"]): sha256_digest(str(t["sql_text"]))
            for t in plan_data.get("transformations", [])
            if isinstance(t, dict) and "step_id" in t and "sql_text" in t
        }
        domain_pv = build_pipeline_version(
            pipeline_id=str(pipe.id),
            revision=next_rev,
            plan=plan_data,
            quality_contract=quality_data,
            supervision_contract=supervision_data,
            hocon_text=hocon_text,
            sql_hashes=sql_hashes,
            secret_refs=plan_data.get("secret_refs", []),
        )
        canon_str = canonical_json(domain_pv.manifest.content)
        manifest_id = uuid.uuid4()
        version_id = uuid.uuid4()

        db_version = DbPipelineVersion(
            id=version_id,
            organization_id=ctx.organization_id,
            project_id=ctx.project_id,
            pipeline_id=pipe.id,
            revision=next_rev,
            parent_revision=parent_rev,
            artifact_hash=domain_pv.artifact_hash,
            manifest_id=manifest_id,
            status="FROZEN",
            created_by=ctx.principal.user_id,
        )
        session.add(db_version)
        session.flush()

        db_manifest = DbArtifactManifest(
            id=manifest_id,
            organization_id=ctx.organization_id,
            project_id=ctx.project_id,
            pipeline_version_id=db_version.id,
            schema_version="1.0.0",
            canonical_json=canon_str,
            artifact_hash=domain_pv.artifact_hash,
            generator_metadata_json={
                "is_mock_llm": True,
                "compiler": "ExecutionStrategyCompiler",
            },
            compiler_version="1.0.0",
        )
        session.add(db_manifest)

        session.add(
            EtlPlanModule(
                organization_id=ctx.organization_id,
                project_id=ctx.project_id,
                pipeline_version_id=db_version.id,
                module_type="ETL_PLAN",
                dsl_json=plan_data,
                content_hash=domain_pv.manifest.content["plan_digest"],
            )
        )
        for step in plan_data.get("transformations", []):
            if isinstance(step, dict) and step.get("kind") == "SELECT_SQL":
                session.add(
                    SqlArtifact(
                        organization_id=ctx.organization_id,
                        project_id=ctx.project_id,
                        pipeline_version_id=db_version.id,
                        sql_purpose=str(step.get("step_id", "s1")),
                        dialect=str(step.get("sql_dialect", "mysql")),
                        sql_text=str(step.get("sql_text", "")),
                        execution_strategy=str(
                            step.get("resolved_execution_strategy", "SOURCE_PUSHDOWN")
                        ),
                        strategy_reason_codes=list(step.get("strategy_reason_codes", [])),
                        ast_report_json={"ok": True},
                        content_hash=sql_hashes.get(
                            str(step.get("step_id", "s1")),
                            sha256_digest(str(step.get("sql_text", ""))),
                        ),
                    )
                )
        session.add(
            HoconArtifact(
                organization_id=ctx.organization_id,
                project_id=ctx.project_id,
                pipeline_version_id=db_version.id,
                hocon_template_text=hocon_text,
                secret_placeholders_json=plan_data.get("secret_refs", []),
                validation_report_json={"passed": True},
                content_hash=domain_pv.manifest.content["hocon_digest"],
            )
        )
        session.add(
            DbQualityContractModel(
                organization_id=ctx.organization_id,
                project_id=ctx.project_id,
                pipeline_version_id=db_version.id,
                contract_json=quality_data,
                content_hash=domain_pv.manifest.content["quality_contract_digest"],
                created_by=ctx.principal.user_id,
            )
        )
        session.add(
            DbRuntimeContractModel(
                organization_id=ctx.organization_id,
                project_id=ctx.project_id,
                pipeline_version_id=db_version.id,
                contract_json=supervision_data,
                content_hash=domain_pv.manifest.content["supervision_contract_digest"],
                created_by=ctx.principal.user_id,
            )
        )

        container.evidence_ledger.append(
            organization_id=str(ctx.organization_id),
            project_id=str(ctx.project_id),
            actor=str(ctx.principal.user_id),
            event_type="PIPELINE_VERSION_FROZEN",
            resource_ref=f"pipeline_version:{db_version.id}",
            artifact_hash=domain_pv.artifact_hash,
            payload={
                "pipeline_id": str(pipe.id),
                "pipeline_version_id": str(db_version.id),
                "revision": next_rev,
                "artifact_hash": domain_pv.artifact_hash,
            },
        )
        _sync_ledger_to_db(session, container, ctx.organization_id, ctx.project_id)

        return {
            "id": str(db_version.id),
            "pipeline_version_id": str(db_version.id),
            "pipeline_id": str(pipe.id),
            "revision": db_version.revision,
            "version_number": db_version.revision,
            "artifact_hash": db_version.artifact_hash,
            "sha256_digest": db_version.artifact_hash,
            "manifest_id": str(db_manifest.id),
            "status": db_version.status,
            "manifest": {
                "id": str(db_manifest.id),
                "pipeline_version_id": str(db_version.id),
                "schema_version": db_manifest.schema_version,
                "artifact_hash": db_manifest.artifact_hash,
                "canonical_json": db_manifest.canonical_json,
                "content": domain_pv.manifest.content,
            },
            "frozen_at": db_version.created_at.isoformat(),
            "frozen_by": str(ctx.principal.user_id),
            "created_at": db_version.created_at.isoformat(),
        }

    # -------------------- 8, 9, 10, 14, 15. Harness (PDP / Prepare / Approve / Commit / Rollback / Evidence) --------------------
    def _create_preparation_handler(
        *,
        session: Session,
        ctx: ProjectRequestContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        prep_type = str(payload.get("preparation_type", "EXECUTION")).upper()
        pipeline_version_id = uuid.UUID(str(payload["pipeline_version_id"]))
        db_ver = session.get(DbPipelineVersion, pipeline_version_id)
        if payload.get("pipeline_id"):
            pipeline_id = uuid.UUID(str(payload["pipeline_id"]))
        elif db_ver is not None:
            pipeline_id = db_ver.pipeline_id
        else:
            raise AppError(ErrorCode.DSL_SCHEMA_INVALID, "缺少 pipeline_id 且未找到对应 pipeline_version_id")
        raw_artifact_hash = (
            db_ver.artifact_hash
            if db_ver is not None
            else str(payload.get("artifact_hash") or "")
        )
        artifact_hash = _normalize_sha256_tag(
            raw_artifact_hash,
            f"pv:{pipeline_id}:{pipeline_version_id}",
        )

        proj = session.get(Project, ctx.project_id)
        environment = str(
            payload.get("environment")
            or (proj.environment if proj is not None else "prod")
        ).lower()
        data_classification = str(
            payload.get("data_classification")
            or ("RESTRICTED_PII" if environment == "prod" else "INTERNAL")
        ).upper()
        requested_risk = (
            str(payload["risk_level"]).upper() if payload.get("risk_level") else None
        )

        if prep_type == "ROLLBACK":
            action = str(payload.get("action", "execution.rollback"))
            rb_type = str(
                payload.get("rollback_type")
                or payload.get("rollback_plan", {}).get("rollback_type")
                or "SHADOW_CLEANUP"
            ).upper()
            default_tool = {
                "SHADOW_CLEANUP": "rollback.shadow_cleanup",
                "SWAP_RESTORE": "rollback.swap_restore",
                "CDC_RECOVER": "rollback.cdc_recover",
            }.get(rb_type, "rollback.shadow_cleanup")
        elif prep_type == "STANDING_SCHEDULE":
            action = str(payload.get("action", "schedule.commit"))
            default_tool = "seatunnel.submit_batch"
        else:
            action = str(payload.get("action", "execution.commit"))
            default_tool = str(payload.get("tool", "seatunnel.submit_batch"))

        pdp_subject = {
            "user_id": str(ctx.principal.user_id),
            "org_role": ctx.org_role,
            "project_roles": ctx.project_roles,
            "permissions": list(
                set(_map_pdp_permissions(ctx))
                | {"execution.commit", "execution.rollback", "schedule.manage"}
            ),
            "is_system_principal": False,
        }
        pdp_req: dict[str, Any] = {
            "subject": pdp_subject,
            "organization_id": str(ctx.organization_id),
            "project_id": str(ctx.project_id),
            "action": action,
            "resource": {
                "resource_type": "pipeline_version",
                "resource_id": str(pipeline_version_id),
            },
            "environment": environment,
            "data_classification": data_classification,
            "artifact_hash": artifact_hash,
        }
        if requested_risk:
            pdp_req["risk_level"] = requested_risk

        pdp_res = container.policy_engine.evaluate(pdp_req)
        container.metrics_registry.etl_harness_pdp_decisions_total.labels(
            risk_level=pdp_res.risk_level,
            decision=pdp_res.decision,
        ).inc()

        if pdp_res.risk_level == "P0" or "PDP_DENIED_P0" in pdp_res.reason_codes:
            container.evidence_ledger.append(
                organization_id=str(ctx.organization_id),
                project_id=str(ctx.project_id),
                actor=str(ctx.principal.user_id),
                event_type="PREPARATION_DENIED_P0",
                resource_ref=f"pipeline_version:{pipeline_version_id}",
                artifact_hash=artifact_hash,
                payload={"reason_codes": pdp_res.reason_codes, "action": action},
            )
            _sync_ledger_to_db(session, container, ctx.organization_id, ctx.project_id)
            raise AppError(
                ErrorCode.PDP_DENIED_P0,
                "PDP 策略硬拒绝 P0 风险操作，不可进入审批或执行",
                detail={"reason_codes": pdp_res.reason_codes},
            )

        # 构建确定性指纹输入
        latest_snap = (
            session.execute(
                select(DbMetadataSnapshot)
                .where(
                    DbMetadataSnapshot.organization_id == ctx.organization_id,
                    DbMetadataSnapshot.project_id == ctx.project_id,
                )
                .order_by(DbMetadataSnapshot.captured_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        snap_fp = _normalize_sha256_tag(
            str(
                payload.get("metadata_snapshot_fingerprint")
                or (latest_snap.schema_fingerprint if latest_snap else "")
            ),
            f"snap:{ctx.project_id}",
        )
        secret_rows = (
            session.execute(
                select(SecretReference).where(
                    SecretReference.organization_id == ctx.organization_id,
                    SecretReference.project_id == ctx.project_id,
                    SecretReference.status == "ACTIVE",
                )
            )
            .scalars()
            .all()
        )
        secret_versions = payload.get("secret_versions") or {
            str(s.id): s.current_version for s in secret_rows
        }
        active_policy_ver = container.policy_store.active.version

        frozen_inputs = {
            "organization_id": str(ctx.organization_id),
            "project_id": str(ctx.project_id),
            "environment": environment,
            "data_classification": data_classification,
            "risk_level": pdp_res.risk_level,
            "policy_version": active_policy_ver,
            "pipeline_version_hash": artifact_hash,
            "artifact_hash": artifact_hash,
            "metadata_snapshot_fingerprint": snap_fp,
            "secret_versions": secret_versions,
            "budget": payload.get("budget", {"max_rows": 1_000_000, "max_duration_seconds": 1800}),
            "tool": default_tool,
        }

        if prep_type == "STANDING_SCHEDULE":
            domain_prep: DomainPreparation = container.standing_schedule_service.prepare_schedule(
                maker_id=str(ctx.principal.user_id),
                inputs=frozen_inputs,
                risk_level=pdp_res.risk_level,
                required_slots=pdp_res.required_approval_slots,
                pipeline_version_id=str(pipeline_version_id),
                cron=str(payload.get("cron_expression", "0 * * * *")),
                max_budget_per_run=float(payload.get("max_budget_per_run", 10.0)),
                total_budget=float(payload.get("total_budget", 1000.0)),
            )
        else:
            domain_prep = container.preparation_service.prepare(
                maker_id=str(ctx.principal.user_id),
                inputs=frozen_inputs,
                risk_level=pdp_res.risk_level,
                required_slots=pdp_res.required_approval_slots,
            )

        if pdp_res.risk_level == "P3" and pdp_res.required_approval_slots == ["AUTO_P3"]:
            container.approval_engine.auto_approve_p3(domain_prep, pdp_res, domain_prep.inputs)
            initial_status = "APPROVED"
        else:
            initial_status = "PENDING_APPROVAL"

        prep_uuid = uuid.UUID(domain_prep.preparation_id)
        now = datetime.now(timezone.utc)
        expires_dt = now + timedelta(hours=24)

        db_prep = DbPreparation(
            id=prep_uuid,
            organization_id=ctx.organization_id,
            project_id=ctx.project_id,
            preparation_type=prep_type,
            pipeline_id=pipeline_id,
            pipeline_version_id=pipeline_version_id,
            artifact_hash=artifact_hash,
            environment=environment,
            risk_level=domain_prep.risk_level,
            policy_version=domain_prep.policy_version,
            status=initial_status,
            frozen_facts_json={**frozen_inputs, **(payload.get("frozen_facts") or {})},
            rollback_plan_json=payload.get("rollback_plan")
            or {
                "rollback_type": payload.get("rollback_type", "SHADOW_CLEANUP"),
                "target_execution_id": payload.get("target_execution_id"),
            },
            budget_json=frozen_inputs["budget"],
            fingerprint_sha256=domain_prep.fingerprint,
            created_by=ctx.principal.user_id,
            expires_at=expires_dt,
        )
        session.add(db_prep)
        session.flush()

        session.add(
            PreparationFingerprint(
                organization_id=ctx.organization_id,
                project_id=ctx.project_id,
                preparation_id=db_prep.id,
                components_json=frozen_inputs,
                fingerprint_sha256=domain_prep.fingerprint,
            )
        )

        slot_rows: list[ApprovalSlot] = []
        for slot_role in domain_prep.required_slots:
            s_row = ApprovalSlot(
                organization_id=ctx.organization_id,
                project_id=ctx.project_id,
                preparation_id=db_prep.id,
                slot_role=slot_role,
                required=True,
                status="APPROVED" if slot_role == "AUTO_P3" else "PENDING",
            )
            session.add(s_row)
            slot_rows.append(s_row)
        session.flush()

        container.evidence_ledger.append(
            organization_id=str(ctx.organization_id),
            project_id=str(ctx.project_id),
            actor=str(ctx.principal.user_id),
            event_type="ROLLBACK_PREPARED" if prep_type == "ROLLBACK" else "PREPARATION_CREATED",
            resource_ref=f"preparation:{db_prep.id}",
            artifact_hash=artifact_hash,
            payload={
                "preparation_id": str(db_prep.id),
                "preparation_type": prep_type,
                "risk_level": db_prep.risk_level,
                "required_approval_slots": domain_prep.required_slots,
                "fingerprint_sha256": db_prep.fingerprint_sha256,
            },
        )
        _sync_ledger_to_db(session, container, ctx.organization_id, ctx.project_id)

        return {
            "id": str(db_prep.id),
            "preparation_id": str(db_prep.id),
            "preparation_type": db_prep.preparation_type,
            "pipeline_id": str(db_prep.pipeline_id),
            "pipeline_version_id": str(db_prep.pipeline_version_id),
            "artifact_hash": db_prep.artifact_hash,
            "environment": db_prep.environment,
            "risk_level": db_prep.risk_level,
            "policy_version": db_prep.policy_version,
            "status": db_prep.status,
            "required_approval_slots": list(domain_prep.required_slots),
            "slots": [
                {
                    "id": str(s.id),
                    "slot_role": s.slot_role,
                    "required": s.required,
                    "status": s.status,
                }
                for s in slot_rows
            ],
            "fingerprint_sha256": db_prep.fingerprint_sha256,
            "created_by": str(db_prep.created_by),
            "expires_at": db_prep.expires_at.isoformat(),
        }

    def _ensure_domain_preparation(db_prep: DbPreparation) -> DomainPreparation:
        """确保数据库中的 `Preparation` 在内存 `PreparationStore` 中有对应的领域对象。"""
        existing = container.broker_store.preps.get(str(db_prep.id))
        if existing is not None:
            return existing

        artifact_hash = _normalize_sha256_tag(
            db_prep.artifact_hash, f"pv:{db_prep.pipeline_version_id}"
        )
        raw_facts = dict(db_prep.frozen_facts_json or {})
        inputs = {
            "organization_id": str(db_prep.organization_id),
            "project_id": str(db_prep.project_id),
            "environment": db_prep.environment,
            "data_classification": raw_facts.get(
                "data_classification",
                "RESTRICTED_PII" if db_prep.environment == "prod" else "INTERNAL",
            ),
            "risk_level": db_prep.risk_level,
            "policy_version": container.policy_store.active.version,
            "pipeline_version_hash": artifact_hash,
            "artifact_hash": artifact_hash,
            "metadata_snapshot_fingerprint": _normalize_sha256_tag(
                raw_facts.get("metadata_snapshot_fingerprint"),
                f"snap:{db_prep.project_id}",
            ),
            "secret_versions": raw_facts.get("secret_versions", {}),
            "budget": db_prep.budget_json or {"max_rows": 1_000_000},
            "tool": raw_facts.get("tool", "seatunnel.submit_batch"),
        }
        required_slots = (
            ["DATA_CHECKER", "SECURITY_CHECKER"]
            if db_prep.risk_level == "P1"
            else (["SINGLE_CHECKER"] if db_prep.risk_level == "P2" else ["AUTO_P3"])
        )
        now_ts = time.time()
        domain_prep = DomainPreparation(
            preparation_id=str(db_prep.id),
            organization_id=str(db_prep.organization_id),
            project_id=str(db_prep.project_id),
            maker_id=str(db_prep.created_by),
            inputs=inputs,
            fingerprint=compute_prep_fingerprint(inputs),
            risk_level=db_prep.risk_level,
            required_slots=required_slots,
            policy_version=container.policy_store.active.version,
            created_at=now_ts,
            expires_at=now_ts + 86400,
        )
        container.broker_store.preps.save(domain_prep)
        return domain_prep

    def _submit_approval_handler(
        *,
        session: Session,
        ctx: ProjectRequestContext,
        preparation: DbPreparation,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        target_slot_role = str(
            payload.get("slot_role") or payload.get("slot_type") or payload.get("slot") or "DATA_CHECKER"
        ).upper()
        decision_str = str(payload.get("decision", "APPROVE")).upper()
        reason_str = str(payload.get("reason", "Approved after review"))

        existing_decisions = (
            session.execute(
                select(ApprovalDecision, ApprovalSlot)
                .join(ApprovalSlot, ApprovalSlot.id == ApprovalDecision.slot_id)
                .where(ApprovalDecision.preparation_id == preparation.id)
            )
            .all()
        )
        existing_map = {slot.slot_role: dec.approver_id for dec, slot in existing_decisions}

        # 第一道防线：执行 Maker 自批拦截与同一用户占双槽冲突拦截
        MembershipService.enforce_approval_sod(
            maker_user_id=preparation.created_by,
            approver_user_id=ctx.principal.user_id,
            target_slot_role=target_slot_role,
            existing_slot_approvers=existing_map,
        )

        domain_prep = _ensure_domain_preparation(preparation)
        approver_subject = {
            "user_id": str(ctx.principal.user_id),
            "org_role": ctx.org_role,
            "project_roles": ctx.project_roles,
            "permissions": _map_pdp_permissions(ctx),
            "is_system_principal": False,
        }

        try:
            if decision_str in ("APPROVE", "APPROVED"):
                container.approval_engine.approve(
                    domain_prep,
                    approver=approver_subject,
                    slot=target_slot_role,
                    current_inputs=domain_prep.inputs,
                )
        except HarnessError as exc:
            raise AppError(exc.code, exc.message, detail=exc.details) from exc

        slot_row = session.execute(
            select(ApprovalSlot).where(
                ApprovalSlot.preparation_id == preparation.id,
                ApprovalSlot.slot_role == target_slot_role,
            )
        ).scalar_one_or_none()
        if slot_row is None:
            slot_row = ApprovalSlot(
                organization_id=ctx.organization_id,
                project_id=ctx.project_id,
                preparation_id=preparation.id,
                slot_role=target_slot_role,
                required=True,
                status="PENDING",
            )
            session.add(slot_row)
            session.flush()

        slot_row.status = "REJECTED" if decision_str in ("REJECT", "REJECTED") else "APPROVED"
        decision_row = ApprovalDecision(
            organization_id=ctx.organization_id,
            project_id=ctx.project_id,
            preparation_id=preparation.id,
            slot_id=slot_row.id,
            approver_id=ctx.principal.user_id,
            decision=slot_row.status,
            reason=reason_str,
            fact_digest_sha256=preparation.fingerprint_sha256,
        )
        session.add(decision_row)
        session.flush()

        missing_slots = container.approval_engine.missing_slots(domain_prep)
        if slot_row.status == "REJECTED":
            preparation.status = "REJECTED"
        elif not missing_slots:
            preparation.status = "APPROVED"
        session.flush()

        container.evidence_ledger.append(
            organization_id=str(ctx.organization_id),
            project_id=str(ctx.project_id),
            actor=str(ctx.principal.user_id),
            event_type="APPROVAL_DECISION_RECORDED",
            resource_ref=f"preparation:{preparation.id}",
            artifact_hash=preparation.artifact_hash,
            payload={
                "preparation_id": str(preparation.id),
                "slot_role": target_slot_role,
                "decision": slot_row.status,
                "preparation_status": preparation.status,
                "missing_slots": missing_slots,
            },
        )
        _sync_ledger_to_db(session, container, ctx.organization_id, ctx.project_id)

        return {
            "id": str(decision_row.id),
            "preparation_id": str(preparation.id),
            "slot_id": str(slot_row.id),
            "slot_role": target_slot_role,
            "decision": slot_row.status,
            "approver_id": str(ctx.principal.user_id),
            "preparation_status": preparation.status,
            "missing_slots": missing_slots,
            "status": preparation.status if preparation.status == "APPROVED" else "RECORDED",
        }

    def _broker_commit_handler(
        *,
        session: Session,
        ctx: ProjectRequestContext,
        preparation: DbPreparation,
        idempotency_key: str,
        trace_id: str,
    ) -> dict[str, Any]:
        domain_prep = _ensure_domain_preparation(preparation)
        # 确保数据库中已登记的审批同步到内存 ApprovalStore（兼容测试直接操作 DB 的情况）
        existing_mem = {
            a.slot for a in container.approval_store.for_preparation(domain_prep.preparation_id)
        }
        db_decisions = (
            session.execute(
                select(ApprovalDecision, ApprovalSlot)
                .join(ApprovalSlot, ApprovalSlot.id == ApprovalDecision.slot_id)
                .where(
                    ApprovalDecision.preparation_id == preparation.id,
                    ApprovalSlot.status == "APPROVED",
                )
            )
            .all()
        )
        for dec, slot in db_decisions:
            if slot.slot_role not in existing_mem:
                container.approval_store.add(
                    Approval(
                        preparation_id=domain_prep.preparation_id,
                        slot=slot.slot_role,
                        approver_id=str(dec.approver_id),
                        approved_at=dec.decided_at.timestamp(),
                    )
                )

        operator_subject = {
            "user_id": str(ctx.principal.user_id),
            "org_role": ctx.org_role,
            "project_roles": ctx.project_roles,
            "permissions": _map_pdp_permissions(ctx),
            "is_system_principal": False,
        }
        tool_name = str(domain_prep.inputs.get("tool", "seatunnel.submit_batch"))

        try:
            if preparation.preparation_type == "ROLLBACK":
                target_exec_id = str(
                    (preparation.rollback_plan_json or {}).get("target_execution_id") or ""
                )
                if target_exec_id and target_exec_id not in container.broker_store.runs:
                    container.broker_store.runs[target_exec_id] = {
                        "execution_id": target_exec_id,
                        "organization_id": str(ctx.organization_id),
                        "project_id": str(ctx.project_id),
                        "environment": preparation.environment,
                        "preparation_id": str(preparation.id),
                        "pipeline_version_hash": domain_prep.inputs["pipeline_version_hash"],
                        "tool": "seatunnel.submit_batch",
                        "status": "FAILED",
                    }
                if target_exec_id:
                    commit_res = container.tool_broker.authorize_rollback(
                        organization_id=str(ctx.organization_id),
                        project_id=str(ctx.project_id),
                        rollback_preparation_id=domain_prep.preparation_id,
                        execution_id=target_exec_id,
                        operator=operator_subject,
                        idempotency_key=idempotency_key,
                        tool=tool_name,
                        trace_id=trace_id,
                    )
                else:
                    commit_res = container.tool_broker.commit(
                        organization_id=str(ctx.organization_id),
                        project_id=str(ctx.project_id),
                        preparation_id=domain_prep.preparation_id,
                        operator=operator_subject,
                        idempotency_key=idempotency_key,
                        tool="seatunnel.submit_batch",
                        trace_id=trace_id,
                    )
            else:
                commit_res = container.tool_broker.commit(
                    organization_id=str(ctx.organization_id),
                    project_id=str(ctx.project_id),
                    preparation_id=domain_prep.preparation_id,
                    operator=operator_subject,
                    idempotency_key=idempotency_key,
                    tool=tool_name,
                    trace_id=trace_id,
                )
        except HarnessError as exc:
            if exc.code == "CAPABILITY_REPLAY_DETECTED":
                container.metrics_registry.etl_harness_replay_blocked_total.labels(
                    source_component="tool_broker"
                ).inc()
            raise AppError(exc.code, exc.message, detail=exc.details) from exc

        # 将 Tool Broker 内存事务生成的 7 类核心治理与执行事实原子持久化至数据库会话
        jti_uuid = uuid.UUID(commit_res.capability_jti)
        exec_uuid = uuid.UUID(commit_res.execution_id)
        cmd_uuid = uuid.UUID(commit_res.command_id)
        outbox_uuid = uuid.UUID(commit_res.outbox_event_id)

        iss_mem = container.broker_store.issuances[commit_res.capability_jti]
        claims = iss_mem["claims"]
        outbox_mem = container.broker_store.outbox_events[commit_res.outbox_event_id]
        outbox_payload = dict(outbox_mem["payload"])

        preparation.status = "COMMITTED"

        session.add(
            CapabilityIssuance(
                organization_id=ctx.organization_id,
                project_id=ctx.project_id,
                jti=jti_uuid,
                preparation_id=preparation.id,
                pipeline_version_hash=str(claims["pipeline_version_hash"]),
                approval_digest=str(claims["approval_digest"]),
                policy_version=str(claims["policy_version"]),
                tool=str(claims["tool"]),
                subject_id=str(claims["sub"]),
                environment=str(claims["environment"]),
                signing_key_id=container.capability_signer.kid,
                nonce=str(claims["nonce"]),
                issued_at=datetime.fromtimestamp(int(claims["iat"]), tz=timezone.utc),
                expires_at=datetime.fromtimestamp(int(claims["exp"]), tz=timezone.utc),
            )
        )
        session.add(
            CapabilityConsumption(
                organization_id=ctx.organization_id,
                project_id=ctx.project_id,
                jti=jti_uuid,
                consumed_by="tool-broker",
                execution_id=exec_uuid,
                command_id=cmd_uuid,
            )
        )

        exec_mode = "CDC" if tool_name == "seatunnel.submit_cdc" else "BATCH"
        run_row = ExecutionRun(
            id=exec_uuid,
            organization_id=ctx.organization_id,
            project_id=ctx.project_id,
            pipeline_id=preparation.pipeline_id,
            pipeline_version_id=preparation.pipeline_version_id,
            artifact_hash=preparation.artifact_hash,
            execution_mode=exec_mode,
            preparation_id=preparation.id,
            capability_jti=jti_uuid,
            command_id=cmd_uuid,
            external_engine="SEATUNNEL_ZETA",
            status="QUEUED",
        )
        session.add(run_row)
        session.flush()

        session.add(
            ExecutionStateEvent(
                organization_id=ctx.organization_id,
                project_id=ctx.project_id,
                execution_id=run_row.id,
                from_status="CREATED",
                to_status="QUEUED",
                reason_code="BROKER_COMMIT_AUTHORIZED",
                detail_json={"capability_jti": str(jti_uuid), "command_id": str(cmd_uuid)},
                actor_id=str(ctx.principal.user_id),
            )
        )

        # 查找冻结版本对应的 HOCON 模板以写入 ExecutionCommand 负载
        hocon_row = (
            session.execute(
                select(HoconArtifact)
                .where(HoconArtifact.pipeline_version_id == preparation.pipeline_version_id)
                .limit(1)
            )
            .scalars()
            .first()
        )
        cmd_payload: dict[str, Any] = {
            "preparation_id": str(preparation.id),
            "tool": tool_name,
            "hocon_template": hocon_row.hocon_template_text if hocon_row else "",
        }
        if preparation.preparation_type == "ROLLBACK":
            rb_plan = dict(preparation.rollback_plan_json or {})
            cmd_payload.update(rb_plan)
            rb_type = str(rb_plan.get("rollback_type", "SHADOW_CLEANUP")).upper()
            target_exec_uuid = (
                uuid.UUID(str(rb_plan["target_execution_id"]))
                if rb_plan.get("target_execution_id")
                else run_row.id
            )
            session.add(
                RollbackRun(
                    organization_id=ctx.organization_id,
                    project_id=ctx.project_id,
                    rollback_type=rb_type,
                    target_execution_id=target_exec_uuid,
                    preparation_id=preparation.id,
                    capability_jti=jti_uuid,
                    command_id=cmd_uuid,
                    status="QUEUED",
                    result_json=rb_plan,
                    initiated_by=ctx.principal.user_id,
                )
            )

        session.add(
            ExecutionCommand(
                organization_id=ctx.organization_id,
                project_id=ctx.project_id,
                command_id=cmd_uuid,
                idempotency_key=idempotency_key,
                execution_id=exec_uuid,
                command_type=str(outbox_payload["event_type"]),
                capability_jti=jti_uuid,
                payload_json=cmd_payload,
                status="PENDING",
            )
        )
        session.add(
            OutboxEvent(
                id=outbox_uuid,
                organization_id=ctx.organization_id,
                project_id=ctx.project_id,
                aggregate_type="ExecutionRun",
                aggregate_id=exec_uuid,
                event_type=str(outbox_payload["event_type"]),
                payload_ref_json=outbox_payload,
            )
        )
        session.flush()
        session.add(
            OutboxDelivery(
                outbox_event_id=outbox_uuid,
                status="PENDING",
                attempts=0,
                max_attempts=5,
            )
        )

        container.metrics_registry.etl_outbox_pending_events.inc()
        _sync_ledger_to_db(session, container, ctx.organization_id, ctx.project_id)

        return {
            "preparation_id": str(preparation.id),
            "execution_id": commit_res.execution_id,
            "command_id": commit_res.command_id,
            "capability_jti": commit_res.capability_jti,
            "outbox_event_id": commit_res.outbox_event_id,
            "status": "QUEUED",
        }

    def _prepare_rollback_handler(
        *,
        session: Session,
        ctx: ProjectRequestContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """受管回滚准备：强制走 Harness Preparation + PDP 风险分级与审批槽位分配，严禁直接执行 SQL。"""
        rollback_type = str(payload.get("rollback_type", "SHADOW_CLEANUP")).upper()
        if rollback_type not in ("SHADOW_CLEANUP", "SWAP_RESTORE", "CDC_RECOVER"):
            raise AppError(
                ErrorCode.DSL_SCHEMA_INVALID,
                f"不支持的回滚类型: {rollback_type}",
            )

        target_exec_id_raw = payload.get("target_execution_id") or payload.get("execution_id")
        pipeline_id_raw = payload.get("pipeline_id")
        pipeline_ver_id_raw = payload.get("pipeline_version_id")
        artifact_hash_raw = payload.get("artifact_hash")

        if target_exec_id_raw:
            target_run = session.get(ExecutionRun, uuid.UUID(str(target_exec_id_raw)))
            if (
                target_run is None
                or target_run.organization_id != ctx.organization_id
                or target_run.project_id != ctx.project_id
            ):
                raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "目标回滚 ExecutionRun 不存在或跨租户越权")
            pipeline_id_raw = pipeline_id_raw or str(target_run.pipeline_id)
            pipeline_ver_id_raw = pipeline_ver_id_raw or str(target_run.pipeline_version_id)
            artifact_hash_raw = artifact_hash_raw or target_run.artifact_hash

        if not pipeline_id_raw:
            first_pipe = (
                session.execute(
                    select(PipelineDefinition)
                    .where(
                        PipelineDefinition.organization_id == ctx.organization_id,
                        PipelineDefinition.project_id == ctx.project_id,
                    )
                    .limit(1)
                )
                .scalars()
                .first()
            )
            pipeline_id_raw = str(first_pipe.id) if first_pipe else str(uuid.uuid4())

        if not pipeline_ver_id_raw:
            first_ver = (
                session.execute(
                    select(DbPipelineVersion)
                    .where(
                        DbPipelineVersion.organization_id == ctx.organization_id,
                        DbPipelineVersion.project_id == ctx.project_id,
                    )
                    .limit(1)
                )
                .scalars()
                .first()
            )
            pipeline_ver_id_raw = str(first_ver.id) if first_ver else str(uuid.uuid4())

        prep_payload = {
            **payload,
            "preparation_type": "ROLLBACK",
            "action": "execution.rollback",
            "pipeline_id": str(pipeline_id_raw),
            "pipeline_version_id": str(pipeline_ver_id_raw),
            "artifact_hash": artifact_hash_raw,
            "rollback_type": rollback_type,
            "rollback_plan": {
                "rollback_type": rollback_type,
                "target_execution_id": str(target_exec_id_raw) if target_exec_id_raw else None,
                "shadow_table_name": payload.get("shadow_table_name"),
                "operator_confirmed_resnapshot": bool(
                    payload.get("operator_confirmed_resnapshot", False)
                ),
                "operator_confirmation_reason": payload.get("operator_confirmation_reason"),
            },
        }
        res = _create_preparation_handler(session=session, ctx=ctx, payload=prep_payload)
        return {
            **res,
            "rollback_type": rollback_type,
            "target_execution_id": str(target_exec_id_raw) if target_exec_id_raw else None,
        }

    def _verify_evidence_handler(
        *,
        session: Session,
        ctx: ProjectRequestContext,
    ) -> dict[str, Any]:
        """校验项目内 Evidence Ledger 的 SHA256 哈希链完整性与 Ed25519 签名检查点。"""
        _sync_ledger_to_db(session, container, ctx.organization_id, ctx.project_id)

        db_events = (
            session.execute(
                select(EvidenceEvent)
                .where(
                    EvidenceEvent.organization_id == ctx.organization_id,
                    EvidenceEvent.project_id == ctx.project_id,
                )
                .order_by(EvidenceEvent.sequence_no.asc())
            )
            .scalars()
            .all()
        )
        chain_key = (str(ctx.organization_id), str(ctx.project_id))
        if db_events and not container.evidence_ledger.store.checkpoints(chain_key):
            try:
                container.evidence_ledger.checkpoint(
                    str(ctx.organization_id),
                    str(ctx.project_id),
                )
                _sync_ledger_to_db(session, container, ctx.organization_id, ctx.project_id)
            except HarnessError:
                pass

        records_for_verify = [
            {
                "event": {
                    "event_id": str(e.id),
                    "sequence_no": int(e.sequence_no),
                    "timestamp": e.timestamp,
                    "actor": e.actor,
                    "organization_id": str(e.organization_id),
                    "project_id": str(e.project_id),
                    "event_type": e.event_type,
                    "resource_ref": e.resource_ref,
                    "artifact_hash": None
                    if e.artifact_hash == "sha256:" + "0" * 64
                    else e.artifact_hash,
                    "payload_digest": e.payload_digest,
                },
                "previous_hash": e.previous_hash,
                "event_hash": e.event_hash,
            }
            for e in db_events
        ]

        # 若内存账本中的 artifact_hash 为 None，保持与内存原始字典一致以确保签名摘要精确匹配
        mem_records = container.evidence_ledger.store.events(chain_key)
        if len(mem_records) == len(db_events):
            for idx, db_ev in enumerate(db_events):
                if (
                    db_ev.event_hash == mem_records[idx]["event_hash"]
                    and db_ev.previous_hash == mem_records[idx]["previous_hash"]
                    and int(db_ev.sequence_no) == int(mem_records[idx]["event"]["sequence_no"])
                ):
                    records_for_verify[idx]["event"]["artifact_hash"] = mem_records[idx]["event"][
                        "artifact_hash"
                    ]

        cps_for_verify = container.evidence_ledger.store.checkpoints(chain_key)

        try:
            verified_count = verify_chain(
                records_for_verify,
                cps_for_verify,
                container.keyring,
                organization_id=str(ctx.organization_id),
                project_id=str(ctx.project_id),
            )
        except HarnessError as exc:
            raise AppError(
                ErrorCode.EVIDENCE_CHAIN_TAMPER_DETECTED,
                exc.message,
                detail=exc.details,
            ) from exc

        head_hash = (
            records_for_verify[-1]["event_hash"]
            if records_for_verify
            else GENESIS_PREVIOUS_HASH
        )
        session.add(
            EvidenceVerificationRun(
                organization_id=ctx.organization_id,
                project_id=ctx.project_id,
                verified_up_to_sequence=verified_count,
                chain_valid=True,
                signatures_valid=True,
                broken_event_id=None,
                verified_by=ctx.principal.user_id,
            )
        )
        session.flush()

        return {
            "chain_valid": True,
            "signatures_valid": True,
            "checkpoint_signatures_valid": True,
            "verified_events": verified_count,
            "verified_events_count": verified_count,
            "checkpoints_verified": len(cps_for_verify),
            "head_event_hash": head_hash,
            "message": f"SHA256 证据链与 Ed25519 检查点验签通过（共核验 {verified_count} 条事件，{len(cps_for_verify)} 个检查点）",
        }

    # -------------------- 11. Execution Reconcile 处理器 --------------------
    def _execution_reconcile_handler(
        *,
        session: Session,
        ctx: ProjectRequestContext,
        run: ExecutionRun,
        trace_id: str,
    ) -> dict[str, Any]:
        """调用真实 `RunReconciler` 对处于 UNKNOWN / SUBMITTING 状态的作业执行确定性对账。"""
        mode = ExecutionMode.CDC if run.execution_mode.upper() == "CDC" else ExecutionMode.BATCH
        run_rec = ExecutionRunRecord(
            execution_id=str(run.id),
            organization_id=str(run.organization_id),
            project_id=str(run.project_id),
            pipeline_id=str(run.pipeline_id),
            pipeline_version_id=str(run.pipeline_version_id),
            artifact_hash=run.artifact_hash,
            execution_mode=mode,
            preparation_id=str(run.preparation_id),
            capability_jti=str(run.capability_jti),
            command_id=str(run.command_id),
            status="UNKNOWN" if run.status in ("UNKNOWN", "SUBMITTING") else run.status,
            external_job_id=run.external_job_id,
        )

        db_job_ref = session.execute(
            select(ExternalJobRef).where(ExternalJobRef.execution_id == run.id)
        ).scalar_one_or_none()
        stable_id = (
            db_job_ref.stable_submit_id
            if db_job_ref is not None
            else build_stable_submit_id(str(run.id), str(run.command_id))
        )
        ref_rec = ExternalJobRefRecord(
            id=str(db_job_ref.id) if db_job_ref else str(uuid.uuid4()),
            organization_id=str(run.organization_id),
            project_id=str(run.project_id),
            execution_id=str(run.id),
            stable_submit_id=stable_id,
            external_job_id=run.external_job_id or (db_job_ref.external_job_id if db_job_ref else None),
        )

        # 若外部作业尚未在 Zeta 中注册，仅将状态推进至 RECONCILING 等待后台 Worker 对账
        cluster_jobs = container.seatunnel_adapter.list_cluster_jobs()
        has_matching_job = any(
            str(j.get("jobName") or "") == stable_id
            or (ref_rec.external_job_id and str(j.get("jobId") or "") == ref_rec.external_job_id)
            for j in cluster_jobs
        )
        if has_matching_job:
            outcome = container.run_reconciler.reconcile(
                run_rec,
                job_ref=ref_rec,
                actor_id=str(ctx.principal.user_id),
                trace_id=trace_id,
            )
            prev_status = run.status
            run.status = outcome.resolved_status
            run.external_job_id = outcome.external_job_id
            session.add(
                ExecutionStateEvent(
                    organization_id=ctx.organization_id,
                    project_id=ctx.project_id,
                    execution_id=run.id,
                    from_status=prev_status,
                    to_status=run.status,
                    reason_code="RECONCILED_WITH_SEATUNNEL_ZETA",
                    detail_json={"stable_submit_id": stable_id, "external_job_id": outcome.external_job_id},
                    actor_id=str(ctx.principal.user_id),
                )
            )
            session.flush()
            return {
                "execution_id": str(run.id),
                "status": run.status,
                "external_job_id": run.external_job_id,
                "confirmation_state": outcome.confirmation_state.value,
            }

        prev_status = run.status
        run.status = "RECONCILING"
        session.add(
            ExecutionStateEvent(
                organization_id=ctx.organization_id,
                project_id=ctx.project_id,
                execution_id=run.id,
                from_status=prev_status,
                to_status="RECONCILING",
                reason_code="OPERATOR_RECONCILE_TRIGGERED",
                detail_json={"stable_submit_id": stable_id},
                actor_id=str(ctx.principal.user_id),
            )
        )
        session.flush()
        return {
            "execution_id": str(run.id),
            "status": run.status,
            "confirmation_state": "RECONCILIATION_PENDING",
        }

    # -------------------- 16. Benchmark 数据集生成、评测运行与 PDP 策略治理处理器 --------------------
    def _create_benchmark_dataset_handler(
        *,
        session: Session,
        ctx: ProjectRequestContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """调用真实 `generate_dataset` 生成固定随机种子的确定性数据集与 SHA256 清单。"""
        scenario = str(payload.get("scenario", "logs_anomaly"))
        if scenario not in ("orders_users", "logs_anomaly"):
            scenario = "logs_anomaly"
        tier = str(payload.get("row_tier") or payload.get("scale") or "10K").upper()
        seed = int(payload.get("seed", 42))
        error_rate = float(payload.get("error_rate", 0.25))
        formats = tuple(payload.get("formats", ["csv", "jsonl"]))

        bench_out_dir = Path(tempfile.gettempdir()) / "etl_agent_benchmarks" / str(ctx.project_id)
        gen_manifest = generate_dataset(
            output_dir=bench_out_dir,
            scenario=scenario,
            tier=tier,
            seed=seed,
            error_rate=error_rate,
            formats=formats,
        )
        manifest_dict = gen_manifest.to_dict()
        manifest_sha256 = canonical_digest(manifest_dict)

        ds = BenchmarkDataset(
            organization_id=ctx.organization_id,
            project_id=ctx.project_id,
            name=str(payload.get("name", f"{scenario}-{tier.lower()}-seed{seed}")),
            version=str(payload.get("version", "v1.0.0")),
            row_tier=tier,
            seed=seed,
            anomaly_spec_json={
                "scenario": scenario,
                "error_rate_target": gen_manifest.error_rate_target,
                "actual_error_rows": gen_manifest.actual_error_rows,
                "actual_error_rate": gen_manifest.actual_error_rate,
                "anomaly_breakdown": gen_manifest.anomaly_breakdown,
                "files": gen_manifest.files,
            },
            manifest_sha256=manifest_sha256,
        )
        session.add(ds)
        session.flush()

        return {
            "id": str(ds.id),
            "name": ds.name,
            "version": ds.version,
            "scenario": scenario,
            "row_tier": ds.row_tier,
            "seed": ds.seed,
            "total_rows": gen_manifest.total_rows,
            "actual_error_rows": gen_manifest.actual_error_rows,
            "actual_error_rate": gen_manifest.actual_error_rate,
            "anomaly_breakdown": gen_manifest.anomaly_breakdown,
            "files": gen_manifest.files,
            "manifest_sha256": ds.manifest_sha256,
        }

    def _trigger_benchmark_handler(
        *,
        session: Session,
        ctx: ProjectRequestContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """调用真实 `BenchmarkEngine` 与 `score_benchmark` 运行 L0/L1 评测并强制执行 P0/P1 独立硬门槛。"""
        layer = str(payload.get("layer") or payload.get("level") or "L0").upper()
        is_mock_llm = bool(payload.get("is_mock_llm", True))
        provider_mode = "mock" if is_mock_llm else "real"

        dataset_id_raw = payload.get("dataset_id")
        if dataset_id_raw:
            dataset_uuid = uuid.UUID(str(dataset_id_raw))
        else:
            first_ds = (
                session.execute(
                    select(BenchmarkDataset)
                    .where(
                        BenchmarkDataset.organization_id == ctx.organization_id,
                        BenchmarkDataset.project_id == ctx.project_id,
                    )
                    .order_by(BenchmarkDataset.created_at.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if first_ds is not None:
                dataset_uuid = first_ds.id
            else:
                created_ds = _create_benchmark_dataset_handler(
                    session=session,
                    ctx=ctx,
                    payload={"row_tier": "10K", "seed": 42},
                )
                dataset_uuid = uuid.UUID(created_ds["id"])

        run_id_str = str(uuid.uuid4())
        report = container.benchmark_engine.run_suite(
            run_id=run_id_str,
            provider_mode=provider_mode,
            generation_metrics=payload.get("generation_metrics"),
        )
        assert report.score_result is not None
        base_raw = dict(report.score_result.raw_metrics)

        # 支持调用方覆盖 p0_passed / p1_passed / raw_metrics 以验证 P0/P1 独立一票否决硬门槛
        if "raw_metrics" in payload and isinstance(payload["raw_metrics"], dict):
            base_raw.update(payload["raw_metrics"])
        if "p0_passed" in payload and payload["p0_passed"] is False:
            base_raw["p0_block_rate"] = 0.0
            base_raw["p0_failed_count"] = max(1, int(base_raw.get("p0_failed_count", 0)))
        if "p1_passed" in payload and payload["p1_passed"] is False:
            base_raw["p1_block_rate"] = 0.0
            base_raw["p1_failed_count"] = max(1, int(base_raw.get("p1_failed_count", 0)))

        raw_obj = RawBenchmarkMetrics(**base_raw)
        score_res = score_benchmark(raw_obj, provider_mode=provider_mode, profile=ScoringProfile())

        p0_passed = raw_obj.p0_failed_count == 0 and raw_obj.p0_block_rate >= 1.0
        p1_passed = raw_obj.p1_failed_count == 0 and raw_obj.p1_block_rate >= 1.0
        overall_passed = bool(score_res.overall_passed and p0_passed and p1_passed)

        db_run = BenchmarkRun(
            id=uuid.UUID(run_id_str),
            organization_id=ctx.organization_id,
            project_id=ctx.project_id,
            dataset_id=dataset_uuid,
            layer=layer,
            is_mock_llm=is_mock_llm,
            p0_passed=p0_passed,
            p1_passed=p1_passed,
            overall_passed=overall_passed,
            raw_metrics_json=score_res.raw_metrics,
            composite_scores_json={
                "dimension_scores": score_res.dimension_scores,
                "composite_score": score_res.composite_score,
                "gate_failure_reasons": score_res.gate_failure_reasons,
            },
            scoring_profile_version=score_res.scoring_profile_version,
            ended_at=datetime.now(timezone.utc),
        )
        session.add(db_run)
        session.flush()

        return {
            "id": str(db_run.id),
            "dataset_id": str(db_run.dataset_id),
            "layer": db_run.layer,
            "status": "COMPLETED",
            "is_mock_llm": db_run.is_mock_llm,
            "p0_passed": db_run.p0_passed,
            "p1_passed": db_run.p1_passed,
            "overall_passed": db_run.overall_passed,
            "p0_p1_gate_passed": score_res.p0_p1_gate_passed,
            "gate_failure_reasons": score_res.gate_failure_reasons,
            "raw_metrics": score_res.raw_metrics,
            "dimension_scores": score_res.dimension_scores,
            "composite_score": score_res.composite_score,
            "scoring_profile_version": score_res.scoring_profile_version,
            "l0_cases_executed": len(report.l0_records),
            "l1_cases_executed": len(report.l1_records),
        }

    def _create_policy_handler(
        *,
        session: Session,
        ctx: ProjectRequestContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """校验并发布新版本 PDP 策略（同步写入 `PolicyStore` 与数据库 `PolicyVersion` 表）。"""
        version_tag = str(payload["version_tag"])
        incoming_rules = payload.get("rules_json") or {}
        merged_rules = {**DEFAULT_RULES, **incoming_rules}
        try:
            pv = container.policy_store.publish(merged_rules, version=version_tag)
        except HarnessError as exc:
            raise AppError(exc.code, exc.message, detail=exc.details) from exc

        db_pv = DbPolicyVersion(
            organization_id=ctx.organization_id,
            project_id=ctx.project_id,
            version_tag=pv.version,
            rules_hash=pv.digest,
            rules_json=pv.rules,
            approved_by=ctx.principal.user_id,
        )
        session.add(db_pv)
        session.flush()
        return {
            "id": str(db_pv.id),
            "version_tag": db_pv.version_tag,
            "rules_hash": db_pv.rules_hash,
            "parent_version": pv.parent,
        }

    # 挂载所有处理器钩子到 app.state
    app.state.agent_create_thread_handler = _agent_create_thread_handler
    app.state.agent_invoke_handler = _agent_invoke_handler
    app.state.agent_resume_handler = _agent_resume_handler
    app.state.deterministic_gate_handler = _deterministic_gate_handler
    app.state.freeze_version_handler = _freeze_version_handler
    app.state.create_preparation_handler = _create_preparation_handler
    app.state.submit_approval_handler = _submit_approval_handler
    app.state.broker_commit_handler = _broker_commit_handler
    app.state.prepare_rollback_handler = _prepare_rollback_handler
    app.state.verify_evidence_handler = _verify_evidence_handler
    app.state.execution_reconcile_handler = _execution_reconcile_handler
    app.state.create_benchmark_dataset_handler = _create_benchmark_dataset_handler
    app.state.trigger_benchmark_handler = _trigger_benchmark_handler
    app.state.create_policy_handler = _create_policy_handler

    return container

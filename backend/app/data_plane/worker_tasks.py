"""Celery Worker 受管任务入口与 Outbox 命令分发器（Worker Task Entrypoints）。

严格遵循 TECHNICAL_SPEC §3.3、§4.3、§8.4、`ADR-002`、`contracts/db/rls_policy_spec.md` §4
与 `contracts/harness/outbox_events.schema.json`：
1. Worker 绝不直接信任消息负载（Payload）中的任何租户字段！
2. 收到 Outbox 事件后，首先调用受控数据库上下文解析接口 `resolve_execution_tenant_context(execution_id, command_id)`
   获取真实 `(organization_id, project_id)`，并立即设置事务级 RLS 上下文（`SET LOCAL app.organization_id` / `app.project_id`）。
3. 从 PostgreSQL 重新读取 `execution_commands` 与 `capability_consumptions` 核验授权绑定与 `capability_jti`。
4. 严格按 `command_id` 执行幂等检查：若命令状态已为 `COMPLETED`（或已登记在幂等完成表中），
   直接返回 `IDEMPOTENT_NOOP`，绝不因为 Celery At-least-once 重投而重复消费 Capability 或产生第二次外部副作用！
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Callable, Protocol

from backend.app.data_plane.seatunnel_adapter import SeaTunnelZetaAdapter
from backend.app.execution.state_machine import (
    ConfirmationState,
    ExecutionRunRecord,
    ExecutionStateMachine,
)
from backend.app.publishing.doris_adapter import (
    DorisPublishAdapter,
    DorisTableSchemaSpec,
    PublishedDatasetVersionRecord,
)
from backend.app.rollback.executor import (
    RollbackCommandContext,
    RollbackExecutor,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_OUTBOX_SCHEMA_PATH = _REPO_ROOT / "contracts" / "harness" / "outbox_events.schema.json"

ALLOWED_OUTBOX_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "EXECUTION_SUBMIT_BATCH",
        "EXECUTION_SUBMIT_CDC",
        "EXECUTION_CANCEL",
        "EXECUTION_PUBLISH_SWAP",
        "ROLLBACK_SHADOW_CLEANUP",
        "ROLLBACK_SWAP_RESTORE",
        "ROLLBACK_CDC_RECOVER",
    }
)


@dataclass(frozen=True)
class OutboxEventPayloadModel:
    """Outbox 投递至 Celery Worker 的标准命令负载（对应 `outbox_events.schema.json` v1.0.0）。"""

    event_type: str
    command_id: str
    execution_id: str
    capability_jti: str
    capability_jws: str
    trace_id: str
    created_at: str
    rollback_run_id: str | None = None
    schema_version: str = "1.0.0"

    def to_dict(self) -> dict[str, Any]:
        """序列化为符合 `outbox_events.schema.json` 的字典。"""
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "command_id": self.command_id,
            "execution_id": self.execution_id,
            "capability_jti": self.capability_jti,
            "capability_jws": self.capability_jws,
            "trace_id": self.trace_id,
            "created_at": self.created_at,
        }
        if self.rollback_run_id is not None:
            payload["rollback_run_id"] = self.rollback_run_id
        return payload


def validate_outbox_event_payload(payload: dict[str, Any]) -> OutboxEventPayloadModel:
    """严格对照 `contracts/harness/outbox_events.schema.json` 校验 Outbox 事件负载。

    注意：契约显式禁止在 Outbox 顶层夹带额外未声明字段（`additionalProperties: false`），
    且 Worker 绝不从 Payload 读取租户 ID。
    """
    schema = json.loads(_OUTBOX_SCHEMA_PATH.read_text(encoding="utf-8"))
    required_fields = set(schema["required"])
    allowed_fields = set(schema["properties"].keys())

    missing = required_fields - set(payload.keys())
    if missing:
        raise ValueError(f"OutboxEventPayload 缺少必填字段: {sorted(missing)}")

    extra = set(payload.keys()) - allowed_fields
    if extra:
        raise ValueError(f"OutboxEventPayload 包含契约禁止的额外字段: {sorted(extra)}")

    if payload.get("schema_version") != "1.0.0":
        raise ValueError("OutboxEventPayload.schema_version 必须为 '1.0.0'")

    event_type = str(payload["event_type"])
    if event_type not in ALLOWED_OUTBOX_EVENT_TYPES:
        raise ValueError(f"未知的 Outbox event_type: {event_type}")

    if len(str(payload["capability_jws"])) < 20:
        raise ValueError("capability_jws 长度不满足最小契约要求 (minLength=20)")

    if len(str(payload["trace_id"])) < 8:
        raise ValueError("trace_id 长度不满足最小契约要求 (minLength=8)")

    return OutboxEventPayloadModel(
        schema_version="1.0.0",
        event_type=event_type,
        command_id=str(payload["command_id"]),
        execution_id=str(payload["execution_id"]),
        rollback_run_id=payload.get("rollback_run_id"),
        capability_jti=str(payload["capability_jti"]),
        capability_jws=str(payload["capability_jws"]),
        trace_id=str(payload["trace_id"]),
        created_at=str(payload["created_at"]),
    )


@dataclass
class AuthorizedCommandFact:
    """从 PostgreSQL `execution_commands` 与 `capability_consumptions` 查询出的持久化授权事实。"""

    command_id: str
    execution_id: str
    organization_id: str
    project_id: str
    command_type: str
    capability_jti: str
    capability_consumed: bool
    status: str = "PENDING"
    payload_json: dict[str, Any] = field(default_factory=dict)


class ControlPlaneRepositoryProtocol(Protocol):
    """Worker 访问 PostgreSQL 控制面持久化事实与注入事务级 RLS 的仓储协议。"""

    def resolve_execution_tenant_context(
        self,
        execution_id: str,
        command_id: str,
    ) -> tuple[str, str] | None:
        """调用 PG 受控 `SECURITY DEFINER` 函数 `resolve_execution_tenant_context(p_execution_id, p_command_id)`。"""

    def set_transaction_rls_context(
        self,
        *,
        organization_id: str,
        project_id: str,
        actor_id: str,
    ) -> None:
        """在当前数据库事务内执行 `SELECT set_config('app.organization_id', ..., true)` 等 RLS 上下文设置。"""

    def get_authorized_command_fact(
        self,
        command_id: str,
        execution_id: str,
    ) -> AuthorizedCommandFact | None:
        """在 RLS 保护下读取 `execution_commands` 与 `capability_consumptions` 核验事实。"""

    def get_execution_run(self, execution_id: str) -> ExecutionRunRecord | None:
        """在 RLS 保护下读取 `ExecutionRunRecord`。"""

    def mark_command_completed(
        self,
        command_id: str,
        result_summary: dict[str, Any],
    ) -> None:
        """将 `execution_commands.status` 标记为 `COMPLETED`。"""


class DataPlaneWorkerDispatcher:
    """Celery Worker 数据面受管命令消费者与幂等执行器。"""

    def __init__(
        self,
        *,
        repository: ControlPlaneRepositoryProtocol,
        seatunnel_adapter: SeaTunnelZetaAdapter,
        doris_publish_adapter: DorisPublishAdapter | None = None,
        rollback_executor: RollbackExecutor | None = None,
        secret_resolver: Callable[[str], str] | None = None,
    ) -> None:
        self._repo = repository
        self._seatunnel = seatunnel_adapter
        self._doris_publish = doris_publish_adapter or DorisPublishAdapter()
        self._rollback = rollback_executor or RollbackExecutor(
            doris_adapter=self._doris_publish
        )
        self._secret_resolver = secret_resolver or (lambda key: f"resolved-secret-for-{key}")
        self._completed_command_cache: dict[str, dict[str, Any]] = {}

    def handle_outbox_command(self, raw_payload: dict[str, Any]) -> dict[str, Any]:
        """消费并执行单条 Outbox 命令。

        安全与幂等执行步骤：
        1. 校验 `OutboxEventPayload` JSON Schema（若被篡改夹带非法字段如伪造 `tenant_id`，直接拒绝）。
        2. 通过 PG `resolve_execution_tenant_context(execution_id, command_id)` 派生真实 `(org_id, project_id)`。
        3. 注入事务级 RLS 上下文（`SET LOCAL`）。
        4. 从 PG 读取 `AuthorizedCommandFact`，核对 `capability_jti` 已在 Commit 事务中合法唯一消费。
        5. 若 `command_id` 已完成（`status == 'COMPLETED'`），立即返回 `IDEMPOTENT_NOOP`，不重复触发外部副作用。
        """
        event = validate_outbox_event_payload(raw_payload)

        # 第一步：从 PostgreSQL 受控函数派生租户上下文，绝不相信外部传入的租户信息
        tenant_ctx = self._repo.resolve_execution_tenant_context(
            event.execution_id,
            event.command_id,
        )
        if tenant_ctx is None:
            raise PermissionError(
                f"TENANT_CONTEXT_MISSING: 无法从 PostgreSQL 解析 execution_id={event.execution_id}, "
                f"command_id={event.command_id} 的可信租户上下文"
            )
        org_id, proj_id = tenant_ctx

        # 第二步：立即注入事务级 RLS 上下文 (is_local=True)
        self._repo.set_transaction_rls_context(
            organization_id=org_id,
            project_id=proj_id,
            actor_id=f"worker:{event.command_id}",
        )

        # 第三步：读取 PostgreSQL 中的持久化命令与 Capability 单次消费事实
        fact = self._repo.get_authorized_command_fact(event.command_id, event.execution_id)
        if fact is None:
            raise PermissionError(
                f"未找到已持久化的授权执行命令: command_id={event.command_id}"
            )
        if fact.organization_id != org_id or fact.project_id != proj_id:
            raise PermissionError("TENANT_ACCESS_DENIED: 命令租户上下文与 ExecutionRun 不一致")

        # 第四步：幂等检查（Celery At-least-once 重投不得再次消费 Capability 或再次提交作业）
        if fact.status == "COMPLETED" or event.command_id in self._completed_command_cache:
            return {
                "status": "IDEMPOTENT_NOOP",
                "command_id": event.command_id,
                "execution_id": event.execution_id,
                "event_type": event.event_type,
                "side_effects_executed": False,
            }

        # 第五步：验证 Capability 已在 Commit 持久事务中登记消费且 JTI 完全匹配
        if not fact.capability_consumed or fact.capability_jti != event.capability_jti:
            raise PermissionError(
                f"CAPABILITY_INVALID_OR_UNCONSUMED: command_id={event.command_id} 的 Capability JTI 未经合法消费"
            )

        run = self._repo.get_execution_run(event.execution_id)
        if run is None:
            raise ValueError(f"未找到 ExecutionRun: {event.execution_id}")

        # 第六步：按 event_type 分派受管数据面操作
        outcome = self._dispatch_verified_action(event, fact, run)
        self._repo.mark_command_completed(event.command_id, outcome)
        self._completed_command_cache[event.command_id] = outcome
        return outcome

    def _dispatch_verified_action(
        self,
        event: OutboxEventPayloadModel,
        fact: AuthorizedCommandFact,
        run: ExecutionRunRecord,
    ) -> dict[str, Any]:
        """在完成 RLS 注入与授权核验后，分派至对应数据面适配器执行。"""
        if event.event_type in ("EXECUTION_SUBMIT_BATCH", "EXECUTION_SUBMIT_CDC"):
            hocon_template = str(fact.payload_json.get("hocon_template") or "")
            job_ref = self._seatunnel.submit_job(
                run=run,
                hocon_template=hocon_template,
                secret_resolver=self._secret_resolver,
                trace_id=event.trace_id,
            )
            return {
                "status": "SUBMITTED",
                "command_id": event.command_id,
                "execution_id": run.execution_id,
                "stable_submit_id": job_ref.stable_submit_id,
                "external_job_id": job_ref.external_job_id,
                "run_status": run.status,
            }

        if event.event_type == "EXECUTION_CANCEL":
            if run.external_job_id:
                self._seatunnel.cancel_job(run.external_job_id)
            target_cancel_req = (
                "CANCEL_REQUESTED" if run.execution_mode.value == "BATCH" else "STOP_REQUESTED"
            )
            target_done = "CANCELLED" if run.execution_mode.value == "BATCH" else "STOPPED"
            if run.status != target_cancel_req and run.status != target_done:
                ExecutionStateMachine.transition(
                    run,
                    target_cancel_req,
                    reason_code="OPERATOR_MANAGED_CANCEL_REQUESTED",
                    confirmation_state=ConfirmationState.REQUEST_SUBMITTED,
                    trace_id=event.trace_id,
                )
            if run.status == target_cancel_req:
                ExecutionStateMachine.transition(
                    run,
                    target_done,
                    reason_code="OPERATOR_MANAGED_CANCEL_CONFIRMED",
                    confirmation_state=ConfirmationState.ENGINE_CONFIRMED,
                    trace_id=event.trace_id,
                )
            return {
                "status": "CANCELLED",
                "command_id": event.command_id,
                "execution_id": run.execution_id,
                "run_status": run.status,
            }

        if event.event_type == "EXECUTION_PUBLISH_SWAP":
            target_schema: DorisTableSchemaSpec = fact.payload_json["target_schema"]
            shadow_schema: DorisTableSchemaSpec = fact.payload_json["shadow_schema"]
            if run.status == "VALIDATING":
                ExecutionStateMachine.transition(
                    run,
                    "PUBLISHING",
                    reason_code="QUALITY_PASSED_ENTERING_PUBLISH",
                    confirmation_state=ConfirmationState.REQUEST_SUBMITTED,
                    trace_id=event.trace_id,
                )
            published = self._doris_publish.publish_shadow_to_target(
                organization_id=fact.organization_id,
                project_id=fact.project_id,
                execution_id=run.execution_id,
                pipeline_version_id=run.pipeline_version_id,
                artifact_hash=run.artifact_hash,
                target_connection_id=str(fact.payload_json.get("target_connection_id", "conn-doris")),
                quality_report_id=str(fact.payload_json.get("quality_report_id", "qr-default")),
                published_by=str(fact.payload_json.get("published_by", "operator")),
                target_schema=target_schema,
                shadow_schema=shadow_schema,
                previous_version_id=fact.payload_json.get("previous_version_id"),
            )
            run.published_dataset_version_id = published.id
            ExecutionStateMachine.transition(
                run,
                "SUCCEEDED",
                reason_code="DORIS_ATOMIC_SWAP_PUBLISHED",
                confirmation_state=ConfirmationState.ENGINE_CONFIRMED,
                trace_id=event.trace_id,
                detail={"published_dataset_version_id": published.id},
            )
            return {
                "status": "PUBLISHED",
                "command_id": event.command_id,
                "execution_id": run.execution_id,
                "published_dataset_version_id": published.id,
                "archive_table_ref": published.archive_table_ref,
                "run_status": run.status,
            }

        # 三类受管回滚命令
        rb_ctx = RollbackCommandContext(
            organization_id=fact.organization_id,
            project_id=fact.project_id,
            preparation_id=str(fact.payload_json.get("preparation_id") or run.preparation_id),
            capability_jti=event.capability_jti,
            command_id=event.command_id,
            initiated_by=str(fact.payload_json.get("initiated_by", "operator")),
            trace_id=event.trace_id,
        )

        if event.event_type == "ROLLBACK_SHADOW_CLEANUP":
            rb_rec = self._rollback.execute_shadow_cleanup(
                ctx=rb_ctx,
                run=run,
                shadow_table_name=str(fact.payload_json["shadow_table_name"]),
            )
            return {
                "status": "ROLLED_BACK",
                "command_id": event.command_id,
                "rollback_run_id": rb_rec.id,
                "rollback_type": rb_rec.rollback_type.value,
                "run_status": run.status,
            }

        if event.event_type == "ROLLBACK_SWAP_RESTORE":
            published_ver: PublishedDatasetVersionRecord = fact.payload_json["published_version"]
            rb_rec = self._rollback.execute_swap_restore(
                ctx=rb_ctx,
                run=run,
                published_version=published_ver,
                target_schema=fact.payload_json["target_schema"],
                archive_schema=fact.payload_json["archive_schema"],
            )
            return {
                "status": "ROLLED_BACK",
                "command_id": event.command_id,
                "rollback_run_id": rb_rec.id,
                "rollback_type": rb_rec.rollback_type.value,
                "run_status": run.status,
            }

        if event.event_type == "ROLLBACK_CDC_RECOVER":
            rb_rec = self._rollback.execute_cdc_recover(
                ctx=rb_ctx,
                run=run,
                checkpoint_valid=bool(fact.payload_json.get("checkpoint_valid", False)),
                position_expired=bool(fact.payload_json.get("position_expired", False)),
                replication_slot_active=bool(
                    fact.payload_json.get("replication_slot_active", True)
                ),
                operator_confirmed_resnapshot=bool(
                    fact.payload_json.get("operator_confirmed_resnapshot", False)
                ),
                operator_confirmation_reason=fact.payload_json.get(
                    "operator_confirmation_reason"
                ),
            )
            return {
                "status": "CDC_RECOVERED",
                "command_id": event.command_id,
                "rollback_run_id": rb_rec.id,
                "rollback_type": rb_rec.rollback_type.value,
                "run_status": run.status,
            }

        raise ValueError(f"不支持的命令事件类型: {event.event_type}")

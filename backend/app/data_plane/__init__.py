"""数据面适配器、未知状态对账器与 Celery Worker 受管任务入口模块（Data Plane & SeaTunnel Zeta Adapter）。

严格遵循 TECHNICAL_SPEC §3.3、§8.4、§10、`ADR-002` 与 `contracts/harness/outbox_events.schema.json`：
1. `SeaTunnelZetaAdapter`：面向 Apache SeaTunnel Zeta 2.3.9 的受管 REST 客户端。
   - 提交前先持久化确定性 `stable_submit_id` 与 `SUBMITTING` 状态（`REQUEST_SUBMITTED`）。
   - `${SECRET:...}` 占位符仅在内存最小生命周期内物化，绝不写入日志、异常详情或持久化记录。
   - 提交超时或响应丢失时进入 `UNKNOWN`（`RECONCILIATION_PENDING`，错误码 `SEATUNNEL_SUBMIT_UNKNOWN`），严禁盲目重提！
2. `RunReconciler`：按 `stable_submit_id` 或 `external_job_id` 向 Zeta 查询对账，
   将 `UNKNOWN -> RECONCILING` 恢复至真实引擎状态（如 `RUNNING` / `STREAMING`）并更新为 `ENGINE_CONFIRMED`。
3. `DataPlaneWorkerDispatcher`（Celery 任务入口）：
   - 绝不信任消息负载中的租户标识；强制通过 PostgreSQL `resolve_execution_tenant_context(execution_id, command_id)`
     解析 `(organization_id, project_id)` 并注入事务级 RLS（`SET LOCAL`）。
   - 重新核验 PostgreSQL 中的 `execution_commands` 与 `capability_consumptions` 授权事实。
   - 对重复投递的相同 `command_id` 执行严格幂等无副作用跳过（`IDEMPOTENT_NOOP`）。
"""

from backend.app.data_plane.reconciler import ReconciliationOutcome, RunReconciler
from backend.app.data_plane.seatunnel_adapter import (
    ExternalJobRefRecord,
    SeaTunnelSubmitUnknownError,
    SeaTunnelZetaAdapter,
    ZetaTransport,
    build_stable_submit_id,
    materialize_hocon_secrets_in_memory,
    verify_hocon_has_no_plaintext_secrets,
)
from backend.app.data_plane.worker_tasks import (
    AuthorizedCommandFact,
    ControlPlaneRepositoryProtocol,
    DataPlaneWorkerDispatcher,
    OutboxEventPayloadModel,
    validate_outbox_event_payload,
)

__all__ = [
    "AuthorizedCommandFact",
    "ControlPlaneRepositoryProtocol",
    "DataPlaneWorkerDispatcher",
    "ExternalJobRefRecord",
    "OutboxEventPayloadModel",
    "ReconciliationOutcome",
    "RunReconciler",
    "SeaTunnelSubmitUnknownError",
    "SeaTunnelZetaAdapter",
    "ZetaTransport",
    "build_stable_submit_id",
    "materialize_hocon_secrets_in_memory",
    "validate_outbox_event_payload",
    "verify_hocon_has_no_plaintext_secrets",
]

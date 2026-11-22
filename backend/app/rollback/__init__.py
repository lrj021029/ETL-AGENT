"""受管回滚与 CDC C+D 恢复执行器模块（Managed Rollback & CDC C+D Recovery Executor）。

严格遵循 TECHNICAL_SPEC §10.5、§13.2、`DEC-010`、`ADR-004` 与 `contracts/openapi/error_codes.yaml`：
1. 所有回滚操作均需通过 Harness 授权命令（绑定 `preparation_id`、`capability_jti`、`command_id`）触发，
   并创建不可变 `RollbackRunRecord`（对应 `rollback_runs` 表），绝不删除历史执行或审计记录。
2. 支持三类受管回滚：
   - `SHADOW_CLEANUP`：清理未发布的受管影子表与临时资源；
   - `SWAP_RESTORE`：基于 `PublishedDatasetVersionRecord.archive_table_ref` 再次执行受管 Doris 原子表替换以恢复上一版本；
   - `CDC_RECOVER`：严格执行 **C+D 恢复策略**——优先从可用历史检查点（Checkpoint）恢复；
     若遇到 `CHECKPOINT_LOST` 或 `POSITION_EXPIRED` 且缺少 Operator 显式确认（`operator_confirmed_resnapshot=True`），
     必须立即阻断并抛出 `CdcRecoveryOperatorConfirmRequiredError`（`CDC_RECOVERY_OPERATOR_CONFIRM_REQUIRED`），
     严禁静默丢弃位点或自动回退全量重跑！
"""

from backend.app.rollback.executor import (
    CdcRecoveryOperatorConfirmRequiredError,
    RollbackCommandContext,
    RollbackExecutor,
    RollbackRunRecord,
    RollbackType,
)

__all__ = [
    "CdcRecoveryOperatorConfirmRequiredError",
    "RollbackCommandContext",
    "RollbackExecutor",
    "RollbackRunRecord",
    "RollbackType",
]

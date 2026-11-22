"""统一执行实例状态机与无副作用故障诊断模块（Execution State Machine & Failure Diagnosis）。

严格执行 `contracts/harness/execution_state_machine.yaml` 与 `contracts/openapi/realtime_events.yaml`：
1. 统一 `ExecutionRun` 实体，但对 `BATCH` 与 `CDC` 实施严格分离的状态转移图。
2. `CDC` 永续流作业绝不允许进入批处理 `SUCCEEDED` 状态。
3. 显式区分 `REQUEST_SUBMITTED`、`ENGINE_CONFIRMED` 与 `RECONCILIATION_PENDING` 确认态。
4. 故障诊断引擎（`FailureDiagnosisEngine`）严格保持零副作用（`advisory_only=True`），绝不自动执行写库或回滚。
"""

from backend.app.execution.diagnosis import (
    DiagnosisActionSuggestion,
    DiagnosisReport,
    FailureDiagnosisEngine,
)
from backend.app.execution.state_machine import (
    BATCH_STATE_TRANSITIONS,
    BATCH_TERMINAL_STATES,
    CDC_STATE_TRANSITIONS,
    CDC_TERMINAL_STATES,
    BatchExecutionStatus,
    CdcExecutionStatus,
    ConfirmationState,
    ExecutionMode,
    ExecutionRunRecord,
    ExecutionStateEvent,
    ExecutionStateMachine,
    StateTransitionInvalidError,
)

__all__ = [
    "BATCH_STATE_TRANSITIONS",
    "BATCH_TERMINAL_STATES",
    "BatchExecutionStatus",
    "CDC_STATE_TRANSITIONS",
    "CDC_TERMINAL_STATES",
    "CdcExecutionStatus",
    "ConfirmationState",
    "DiagnosisActionSuggestion",
    "DiagnosisReport",
    "ExecutionMode",
    "ExecutionRunRecord",
    "ExecutionStateEvent",
    "ExecutionStateMachine",
    "FailureDiagnosisEngine",
    "StateTransitionInvalidError",
]

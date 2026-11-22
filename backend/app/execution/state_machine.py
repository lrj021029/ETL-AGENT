"""批处理（BATCH）与变更数据捕获（CDC）状态机实现。

严格绑定 `contracts/harness/execution_state_machine.yaml` 契约定义：
- 任何未在转移表中的状态跃迁均抛出 `StateTransitionInvalidError`（`STATE_TRANSITION_INVALID`）。
- CDC 作业绝不进入批处理的 `SUCCEEDED` 状态。
- 每次状态迁移均生成不可变 `ExecutionStateEvent` 并维护 `ConfirmationState`。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
import uuid


class ExecutionMode(str, Enum):
    """执行运行模式：批处理（BATCH）与持续变更捕获（CDC）。"""

    BATCH = "BATCH"
    CDC = "CDC"


class ConfirmationState(str, Enum):
    """控制面与外部执行引擎之间的状态确认层级（对应 `realtime_events.yaml`）。"""

    REQUEST_SUBMITTED = "REQUEST_SUBMITTED"
    ENGINE_CONFIRMED = "ENGINE_CONFIRMED"
    RECONCILIATION_PENDING = "RECONCILIATION_PENDING"


class BatchExecutionStatus(str, Enum):
    """批处理（BATCH）模式完整状态枚举。"""

    CREATED = "CREATED"
    QUEUED = "QUEUED"
    SUBMITTING = "SUBMITTING"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    RUNNING = "RUNNING"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSED = "PAUSED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    VALIDATING = "VALIDATING"
    PUBLISHING = "PUBLISHING"
    SUCCEEDED = "SUCCEEDED"
    INTERRUPTED = "INTERRUPTED"
    FAILED = "FAILED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"


class CdcExecutionStatus(str, Enum):
    """持续 CDC 模式完整状态枚举（不含 SUCCEEDED）。"""

    CREATED = "CREATED"
    QUEUED = "QUEUED"
    SUBMITTING = "SUBMITTING"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    INITIAL_SNAPSHOT = "INITIAL_SNAPSHOT"
    STREAMING = "STREAMING"
    DEGRADED = "DEGRADED"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSED = "PAUSED"
    CHECKPOINT_LOST = "CHECKPOINT_LOST"
    POSITION_EXPIRED = "POSITION_EXPIRED"
    RECOVERING = "RECOVERING"
    STOP_REQUESTED = "STOP_REQUESTED"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


# 严格对齐 contracts/harness/execution_state_machine.yaml 的 BATCH 转移表
BATCH_TERMINAL_STATES: frozenset[str] = frozenset(
    {"SUCCEEDED", "CANCELLED", "INTERRUPTED", "FAILED", "ROLLED_BACK"}
)

BATCH_STATE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "CREATED": ("QUEUED", "CANCELLED", "FAILED"),
    "QUEUED": ("SUBMITTING", "CANCEL_REQUESTED", "CANCELLED", "FAILED"),
    "SUBMITTING": ("RUNNING", "UNKNOWN", "FAILED", "CANCEL_REQUESTED"),
    "UNKNOWN": ("RECONCILING", "FAILED", "CANCEL_REQUESTED"),
    "RECONCILING": (
        "RUNNING",
        "VALIDATING",
        "SUCCEEDED",
        "FAILED",
        "INTERRUPTED",
        "CANCELLED",
        "UNKNOWN",
    ),
    "RUNNING": (
        "VALIDATING",
        "PAUSE_REQUESTED",
        "CANCEL_REQUESTED",
        "INTERRUPTED",
        "FAILED",
        "UNKNOWN",
    ),
    "PAUSE_REQUESTED": ("PAUSED", "RUNNING", "INTERRUPTED", "FAILED", "CANCEL_REQUESTED"),
    "PAUSED": ("RUNNING", "CANCEL_REQUESTED", "CANCELLED", "FAILED"),
    "CANCEL_REQUESTED": ("CANCELLED", "INTERRUPTED", "FAILED", "UNKNOWN"),
    "VALIDATING": ("PUBLISHING", "INTERRUPTED", "FAILED", "CANCEL_REQUESTED", "ROLLING_BACK"),
    "PUBLISHING": ("SUCCEEDED", "FAILED", "UNKNOWN", "ROLLING_BACK"),
    "SUCCEEDED": ("ROLLING_BACK",),
    "INTERRUPTED": ("ROLLING_BACK",),
    "FAILED": ("ROLLING_BACK",),
    "ROLLING_BACK": ("ROLLED_BACK", "FAILED"),
    "ROLLED_BACK": (),
    "CANCELLED": ("ROLLING_BACK",),
}

# 严格对齐 contracts/harness/execution_state_machine.yaml 的 CDC 转移表
CDC_TERMINAL_STATES: frozenset[str] = frozenset({"STOPPED", "FAILED"})

CDC_STATE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "CREATED": ("QUEUED", "STOPPED", "FAILED"),
    "QUEUED": ("SUBMITTING", "STOP_REQUESTED", "STOPPED", "FAILED"),
    "SUBMITTING": ("INITIAL_SNAPSHOT", "STREAMING", "UNKNOWN", "FAILED"),
    "UNKNOWN": ("RECONCILING", "FAILED", "STOP_REQUESTED"),
    "RECONCILING": (
        "INITIAL_SNAPSHOT",
        "STREAMING",
        "DEGRADED",
        "STOPPED",
        "FAILED",
        "CHECKPOINT_LOST",
        "POSITION_EXPIRED",
        "UNKNOWN",
    ),
    "INITIAL_SNAPSHOT": (
        "STREAMING",
        "DEGRADED",
        "PAUSE_REQUESTED",
        "STOP_REQUESTED",
        "FAILED",
        "CHECKPOINT_LOST",
        "UNKNOWN",
    ),
    "STREAMING": (
        "DEGRADED",
        "PAUSE_REQUESTED",
        "STOP_REQUESTED",
        "FAILED",
        "CHECKPOINT_LOST",
        "POSITION_EXPIRED",
        "UNKNOWN",
    ),
    "DEGRADED": (
        "STREAMING",
        "STOP_REQUESTED",
        "FAILED",
        "CHECKPOINT_LOST",
        "POSITION_EXPIRED",
        "RECOVERING",
    ),
    "PAUSE_REQUESTED": ("PAUSED", "STREAMING", "STOP_REQUESTED", "FAILED"),
    "PAUSED": ("STREAMING", "STOP_REQUESTED", "STOPPED", "FAILED"),
    "CHECKPOINT_LOST": ("RECOVERING", "STOP_REQUESTED", "STOPPED", "FAILED"),
    "POSITION_EXPIRED": ("RECOVERING", "STOP_REQUESTED", "STOPPED", "FAILED"),
    "RECOVERING": ("INITIAL_SNAPSHOT", "STREAMING", "FAILED", "STOP_REQUESTED"),
    "STOP_REQUESTED": ("STOPPED", "FAILED", "UNKNOWN"),
    "STOPPED": ("RECOVERING",),
    "FAILED": ("RECOVERING",),
}


class StateTransitionInvalidError(Exception):
    """当请求的 ExecutionRun 状态跃迁违反契约转移表时抛出。"""

    error_code: str = "STATE_TRANSITION_INVALID"

    def __init__(
        self,
        message: str,
        *,
        execution_mode: str,
        from_status: str,
        to_status: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.execution_mode = execution_mode
        self.from_status = from_status
        self.to_status = to_status
        self.detail = {
            "execution_mode": execution_mode,
            "from_status": from_status,
            "to_status": to_status,
            **(detail or {}),
        }


@dataclass(frozen=True)
class ExecutionStateEvent:
    """不可变执行状态迁移事件记录（对应 `execution_state_events` 表及 SSE `execution.state_changed`）。"""

    event_id: str
    organization_id: str
    project_id: str
    execution_id: str
    execution_mode: ExecutionMode
    from_status: str
    to_status: str
    confirmation_state: ConfirmationState
    reason_code: str
    actor_id: str
    occurred_at: str
    external_job_id: str | None = None
    trace_id: str = ""
    detail_json: dict[str, Any] = field(default_factory=dict)

    def to_realtime_payload(self) -> dict[str, Any]:
        """转换为遵循 `contracts/openapi/realtime_events.yaml` 的 `execution.state_changed` 事件负载。"""
        return {
            "execution_id": self.execution_id,
            "execution_mode": self.execution_mode.value,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "confirmation_state": self.confirmation_state.value,
            "external_job_id": self.external_job_id,
            "reason_code": self.reason_code,
            "occurred_at": self.occurred_at,
            "trace_id": self.trace_id,
        }


@dataclass
class ExecutionRunRecord:
    """统一 `ExecutionRun` 领域聚合根（对应 `execution_runs` 表）。"""

    execution_id: str
    organization_id: str
    project_id: str
    pipeline_id: str
    pipeline_version_id: str
    artifact_hash: str
    execution_mode: ExecutionMode
    preparation_id: str
    capability_jti: str
    command_id: str
    status: str = "CREATED"
    status_reason: str = "INITIALIZED"
    confirmation_state: ConfirmationState = ConfirmationState.REQUEST_SUBMITTED
    external_engine: str = "SEATUNNEL_ZETA"
    external_job_id: str | None = None
    source_position_json: dict[str, Any] = field(default_factory=dict)
    checkpoint_ref: str | None = None
    metrics_snapshot_json: dict[str, Any] = field(default_factory=dict)
    published_dataset_version_id: str | None = None
    diagnosis_json: dict[str, Any] | None = None
    started_at: str | None = None
    ended_at: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    state_events: list[ExecutionStateEvent] = field(default_factory=list)


def derive_confirmation_state(to_status: str) -> ConfirmationState:
    """根据目标状态自动推导控制面与引擎的确认状态（也可由调用方显式指定覆盖）。"""
    if to_status in ("UNKNOWN", "RECONCILING"):
        return ConfirmationState.RECONCILIATION_PENDING
    if to_status in (
        "CREATED",
        "QUEUED",
        "SUBMITTING",
        "PAUSE_REQUESTED",
        "CANCEL_REQUESTED",
        "STOP_REQUESTED",
    ):
        return ConfirmationState.REQUEST_SUBMITTED
    return ConfirmationState.ENGINE_CONFIRMED


class ExecutionStateMachine:
    """统一执行实例状态机守卫引擎。"""

    @staticmethod
    def get_transition_table(mode: ExecutionMode | str) -> dict[str, tuple[str, ...]]:
        """获取指定运行模式的状态转移邻接表。"""
        resolved_mode = ExecutionMode(mode) if isinstance(mode, str) else mode
        if resolved_mode == ExecutionMode.BATCH:
            return BATCH_STATE_TRANSITIONS
        if resolved_mode == ExecutionMode.CDC:
            return CDC_STATE_TRANSITIONS
        raise ValueError(f"未知的运行模式: {mode}")

    @staticmethod
    def is_terminal_state(mode: ExecutionMode | str, status: str) -> bool:
        """判断状态是否为该运行模式的终态。"""
        resolved_mode = ExecutionMode(mode) if isinstance(mode, str) else mode
        if resolved_mode == ExecutionMode.BATCH:
            return status in BATCH_TERMINAL_STATES
        return status in CDC_TERMINAL_STATES

    @classmethod
    def validate_transition(
        cls,
        mode: ExecutionMode | str,
        from_status: str,
        to_status: str,
    ) -> None:
        """校验从 `from_status` 到 `to_status` 的迁移合法性；不合法时抛出 `StateTransitionInvalidError`。"""
        resolved_mode = ExecutionMode(mode) if isinstance(mode, str) else mode
        table = cls.get_transition_table(resolved_mode)

        if from_status not in table:
            raise StateTransitionInvalidError(
                f"模式 {resolved_mode.value} 下不存在源状态 {from_status}",
                execution_mode=resolved_mode.value,
                from_status=from_status,
                to_status=to_status,
                detail={"reason": "UNKNOWN_SOURCE_STATE"},
            )

        if to_status not in table:
            raise StateTransitionInvalidError(
                f"模式 {resolved_mode.value} 下不允许目标状态 {to_status}",
                execution_mode=resolved_mode.value,
                from_status=from_status,
                to_status=to_status,
                detail={"reason": "INVALID_STATE_FOR_MODE"},
            )

        allowed_targets = table[from_status]
        if to_status not in allowed_targets:
            raise StateTransitionInvalidError(
                f"模式 {resolved_mode.value} 禁止从 {from_status} 迁移到 {to_status} "
                f"(允许目标: {list(allowed_targets)})",
                execution_mode=resolved_mode.value,
                from_status=from_status,
                to_status=to_status,
                detail={"allowed_targets": list(allowed_targets)},
            )

    @classmethod
    def transition(
        cls,
        run: ExecutionRunRecord,
        to_status: str,
        *,
        reason_code: str,
        actor_id: str = "system:worker",
        confirmation_state: ConfirmationState | None = None,
        external_job_id: str | None = None,
        trace_id: str = "",
        detail: dict[str, Any] | None = None,
    ) -> ExecutionStateEvent:
        """对 `ExecutionRunRecord` 执行受控状态迁移并追加审计状态事件。"""
        from_status = run.status
        cls.validate_transition(run.execution_mode, from_status, to_status)

        now_iso = datetime.now(timezone.utc).isoformat()
        resolved_confirmation = confirmation_state or derive_confirmation_state(to_status)

        if external_job_id is not None:
            run.external_job_id = external_job_id

        # 维护启动与结束时间戳
        if to_status in ("RUNNING", "INITIAL_SNAPSHOT", "STREAMING") and run.started_at is None:
            run.started_at = now_iso
        if cls.is_terminal_state(run.execution_mode, to_status):
            run.ended_at = now_iso

        run.status = to_status
        run.status_reason = reason_code
        run.confirmation_state = resolved_confirmation

        event = ExecutionStateEvent(
            event_id=str(uuid.uuid4()),
            organization_id=run.organization_id,
            project_id=run.project_id,
            execution_id=run.execution_id,
            execution_mode=run.execution_mode,
            from_status=from_status,
            to_status=to_status,
            confirmation_state=resolved_confirmation,
            reason_code=reason_code,
            actor_id=actor_id,
            occurred_at=now_iso,
            external_job_id=run.external_job_id,
            trace_id=trace_id or f"trace-{run.execution_id[:8]}",
            detail_json=detail or {},
        )
        run.state_events.append(event)
        return event

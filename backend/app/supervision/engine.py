"""运行时监督契约模型、阈值评估器与受管中断执行器。

对齐 `contracts/schemas/runtime_supervision.schema.json` 与 `contracts/openapi/realtime_events.yaml`。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
from typing import Any, Protocol

from backend.app.execution.diagnosis import FailureDiagnosisEngine
from backend.app.execution.state_machine import (
    ConfirmationState,
    ExecutionMode,
    ExecutionRunRecord,
    ExecutionStateMachine,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SUPERVISION_SCHEMA_PATH = (
    _REPO_ROOT / "contracts" / "schemas" / "runtime_supervision.schema.json"
)


class WindowMode(str, Enum):
    """滚动窗口统计口径模式（行数窗口或时间窗口）。"""

    ROW_COUNT = "ROW_COUNT"
    TIME_SECONDS = "TIME_SECONDS"


class SupervisionThresholdBreachedError(Exception):
    """当运行指标超过 `RuntimeSupervisionContract` 阈值时抛出的领域异常。"""

    error_code: str = "SUPERVISION_THRESHOLD_BREACHED"

    def __init__(
        self,
        message: str,
        *,
        breaches: list[SupervisionBreachDetail],
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.breaches = breaches
        self.detail = {
            "breaches": [b.to_dict() for b in breaches],
            **(detail or {}),
        }


class CancelableJobAdapter(Protocol):
    """支持受管取消/停止外部作业的数据面适配器协议。"""

    def cancel_job(self, external_job_id: str) -> dict[str, Any]:
        """向 SeaTunnel Zeta 发送受管停止请求。"""


@dataclass(frozen=True)
class WindowDefinition:
    """滚动窗口定义（PRD-TBD-08 显式参数化配置）。"""

    mode: WindowMode
    size: int

    def __post_init__(self) -> None:
        if self.size < 1:
            raise ValueError("window_definition.size 必须 >= 1")

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约字典。"""
        return {"mode": self.mode.value, "size": self.size}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WindowDefinition:
        """从字典反序列化。"""
        return cls(mode=WindowMode(data["mode"]), size=int(data["size"]))


@dataclass(frozen=True)
class RuntimeSupervisionContract:
    """冻结的运行时监督契约模型（Contract v1.0.0）。

    所有阈值均通过契约实例配置化传入（遵循 PRD-TBD-08 / PRD-TBD-14）。
    其中 `max_reject_rate_total` 默认 0.20，且确认比较算子严格为 `>`（恰好 0.20 不触发）。
    """

    max_read_rows: int
    max_write_bytes: int
    max_duration_seconds: int
    max_amplification_ratio: float
    max_reject_rate_window: float
    window_definition: WindowDefinition
    min_validated_rows: int
    max_error_rows_absolute: int
    action_on_breach: str = "HARD_INTERRUPT"
    max_reject_rate_total: float = 0.20
    cdc_max_lag_seconds: int | None = None
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        """校验契约各字段边界（防止空值或无界配置）。"""
        if self.schema_version != "1.0.0":
            raise ValueError(f"不支持的 schema_version: {self.schema_version}")
        if self.max_read_rows < 1:
            raise ValueError("max_read_rows 必须 >= 1")
        if self.max_write_bytes < 1024:
            raise ValueError("max_write_bytes 必须 >= 1024")
        if self.max_duration_seconds < 10:
            raise ValueError("max_duration_seconds 必须 >= 10")
        if self.max_amplification_ratio <= 0:
            raise ValueError("max_amplification_ratio 必须 > 0")
        if not (0.0 <= self.max_reject_rate_total <= 1.0):
            raise ValueError("max_reject_rate_total 必须在 [0, 1] 范围内")
        if not (0.0 <= self.max_reject_rate_window <= 1.0):
            raise ValueError("max_reject_rate_window 必须在 [0, 1] 范围内")
        if self.min_validated_rows < 1:
            raise ValueError("min_validated_rows 必须 >= 1")
        if self.max_error_rows_absolute < 1:
            raise ValueError("max_error_rows_absolute 必须 >= 1")
        if self.action_on_breach not in ("HARD_INTERRUPT", "ALERT_ONLY"):
            raise ValueError(f"非法的 action_on_breach: {self.action_on_breach}")
        if self.cdc_max_lag_seconds is not None and self.cdc_max_lag_seconds < 1:
            raise ValueError("cdc_max_lag_seconds 必须 >= 1 或为 None")

    def to_dict(self) -> dict[str, Any]:
        """序列化为符合 `runtime_supervision.schema.json` 的字典。"""
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "max_read_rows": self.max_read_rows,
            "max_write_bytes": self.max_write_bytes,
            "max_duration_seconds": self.max_duration_seconds,
            "max_amplification_ratio": self.max_amplification_ratio,
            "max_reject_rate_total": self.max_reject_rate_total,
            "max_reject_rate_window": self.max_reject_rate_window,
            "window_definition": self.window_definition.to_dict(),
            "min_validated_rows": self.min_validated_rows,
            "max_error_rows_absolute": self.max_error_rows_absolute,
            "action_on_breach": self.action_on_breach,
            "cdc_max_lag_seconds": self.cdc_max_lag_seconds,
        }
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuntimeSupervisionContract:
        """从字典构造 `RuntimeSupervisionContract`。"""
        return cls(
            schema_version=str(data.get("schema_version", "1.0.0")),
            max_read_rows=int(data["max_read_rows"]),
            max_write_bytes=int(data["max_write_bytes"]),
            max_duration_seconds=int(data["max_duration_seconds"]),
            max_amplification_ratio=float(data["max_amplification_ratio"]),
            max_reject_rate_total=float(data.get("max_reject_rate_total", 0.20)),
            max_reject_rate_window=float(data["max_reject_rate_window"]),
            window_definition=WindowDefinition.from_dict(data["window_definition"]),
            min_validated_rows=int(data["min_validated_rows"]),
            max_error_rows_absolute=int(data["max_error_rows_absolute"]),
            action_on_breach=str(data["action_on_breach"]),
            cdc_max_lag_seconds=(
                int(data["cdc_max_lag_seconds"])
                if data.get("cdc_max_lag_seconds") is not None
                else None
            ),
        )


def validate_runtime_supervision_schema(
    contract_dict: dict[str, Any],
) -> RuntimeSupervisionContract:
    """对照 `contracts/schemas/runtime_supervision.schema.json` 校验契约。"""
    schema = json.loads(_SUPERVISION_SCHEMA_PATH.read_text(encoding="utf-8"))
    required_top = set(schema["required"])
    allowed_top = set(schema["properties"].keys())

    missing = required_top - set(contract_dict.keys())
    if missing:
        raise ValueError(f"RuntimeSupervisionContract 缺少必填字段: {sorted(missing)}")
    extra = set(contract_dict.keys()) - allowed_top
    if extra:
        raise ValueError(f"RuntimeSupervisionContract 包含未允许的额外字段: {sorted(extra)}")

    win = contract_dict.get("window_definition")
    if not isinstance(win, dict) or set(win.keys()) != {"mode", "size"}:
        raise ValueError("window_definition 必须且仅能包含 mode 与 size 字段")

    return RuntimeSupervisionContract.from_dict(contract_dict)


def compute_reject_rate(error_rows: int, validated_rows: int) -> float:
    """计算拒绝率：`reject_rate = error_rows / validated_rows`。

    当 `validated_rows <= 0` 时返回 `0.0`，防止除零异常。
    """
    if validated_rows <= 0:
        return 0.0
    return float(error_rows) / float(validated_rows)


@dataclass(frozen=True)
class ExecutionMetricsSnapshot:
    """单次采样或阶段汇总的执行运行指标快照（对应 `execution_metrics` 表与 `execution.metrics_tick`）。"""

    execution_id: str
    read_rows: int
    validated_rows: int
    valid_rows: int
    write_rows: int
    write_bytes: int
    error_rows: int
    duration_seconds: float
    read_bytes: int = 0
    window_validated_rows: int | None = None
    window_error_rows: int | None = None
    throughput_rps: float = 0.0
    cdc_lag_ms: int | None = None
    checkpoint_position: str | None = None
    sampled_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def reject_rate_total(self) -> float:
        """整体拒绝率 `error_rows / validated_rows`。"""
        return compute_reject_rate(self.error_rows, self.validated_rows)

    @property
    def reject_rate_window(self) -> float:
        """当前滚动窗口拒绝率。若未单独提供窗口计数，则默认回退至当前采样计数。"""
        if self.window_validated_rows is not None and self.window_error_rows is not None:
            return compute_reject_rate(self.window_error_rows, self.window_validated_rows)
        return self.reject_rate_total

    @property
    def amplification_ratio(self) -> float:
        """输出放大比 `write_rows / read_rows`（`read_rows <= 0` 时返回 `1.0` 或 `0.0`）。"""
        if self.read_rows <= 0:
            return float(self.write_rows) if self.write_rows > 0 else 0.0
        return float(self.write_rows) / float(self.read_rows)

    def to_realtime_tick(self) -> dict[str, Any]:
        """转换为遵循 `contracts/openapi/realtime_events.yaml` 的 `execution.metrics_tick` 事件负载。"""
        return {
            "execution_id": self.execution_id,
            "sampled_at": self.sampled_at,
            "read_rows": self.read_rows,
            "validated_rows": self.validated_rows,
            "valid_rows": self.valid_rows,
            "write_rows": self.write_rows,
            "write_bytes": self.write_bytes,
            "error_rows": self.error_rows,
            "reject_rate_total": self.reject_rate_total,
            "reject_rate_window": self.reject_rate_window,
            "throughput_rps": self.throughput_rps,
            "duration_seconds": self.duration_seconds,
            "amplification_ratio": self.amplification_ratio,
            "cdc_lag_ms": self.cdc_lag_ms,
        }


@dataclass(frozen=True)
class SupervisionBreachDetail:
    """单项运行监督阈值违规明细。"""

    breach_type: str
    threshold: float | int
    observed_value: float | int
    action_taken: str
    occurred_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "breach_type": self.breach_type,
            "threshold": self.threshold,
            "observed_value": self.observed_value,
            "action_taken": self.action_taken,
            "occurred_at": self.occurred_at,
        }

    def to_realtime_alert(self, execution_id: str) -> dict[str, Any]:
        """转换为遵循 `contracts/openapi/realtime_events.yaml` 的 `execution.supervision_alert` 负载。"""
        return {
            "execution_id": execution_id,
            "breach_type": self.breach_type,
            "threshold": self.threshold,
            "observed_value": self.observed_value,
            "action_taken": self.action_taken,
            "occurred_at": self.occurred_at,
        }


@dataclass(frozen=True)
class SupervisionEvaluationResult:
    """运行监督评估结果。"""

    breached: bool
    action_to_take: str
    reject_rate_total: float
    reject_rate_window: float
    amplification_ratio: float
    breaches: list[SupervisionBreachDetail]
    realtime_alerts: list[dict[str, Any]]


class RuntimeSupervisionEngine:
    """运行时质量拒绝率与资源预算监督引擎。"""

    def __init__(self, diagnosis_engine: FailureDiagnosisEngine | None = None) -> None:
        self._diagnosis_engine = diagnosis_engine or FailureDiagnosisEngine()
        # 按 execution_id 保存用于滚动窗口计算的增量样本队列
        self._rolling_samples: dict[str, list[tuple[float, int, int]]] = {}

    def record_incremental_sample(
        self,
        *,
        execution_id: str,
        timestamp_epoch_sec: float,
        delta_validated_rows: int,
        delta_error_rows: int,
        window_def: WindowDefinition,
    ) -> tuple[int, int]:
        """记录增量采样并按 `ROW_COUNT` 或 `TIME_SECONDS` 计算滚动窗口内的 `(window_validated_rows, window_error_rows)`。"""
        history = self._rolling_samples.setdefault(execution_id, [])
        history.append((timestamp_epoch_sec, delta_validated_rows, delta_error_rows))

        if window_def.mode == WindowMode.TIME_SECONDS:
            cutoff = timestamp_epoch_sec - float(window_def.size)
            pruned = [item for item in history if item[0] >= cutoff]
            self._rolling_samples[execution_id] = pruned
            win_val = sum(item[1] for item in pruned)
            win_err = sum(item[2] for item in pruned)
            return win_val, win_err

        # ROW_COUNT 模式：从最新样本向前累计直到达到 window_def.size 行
        acc_val = 0
        acc_err = 0
        kept: list[tuple[float, int, int]] = []
        for ts, d_val, d_err in reversed(history):
            kept.append((ts, d_val, d_err))
            acc_val += d_val
            acc_err += d_err
            if acc_val >= window_def.size:
                break
        kept.reverse()
        self._rolling_samples[execution_id] = kept
        return acc_val, acc_err

    def evaluate(
        self,
        contract: RuntimeSupervisionContract,
        metrics: ExecutionMetricsSnapshot,
    ) -> SupervisionEvaluationResult:
        """评估当前指标快照是否违反 `RuntimeSupervisionContract`。

        关键不变量（DEC-012 / ADR-004）：
        - 整体拒绝率 `reject_rate_total = error_rows / validated_rows` 仅在
          `validated_rows >= contract.min_validated_rows` 且严格大于（`>`）`contract.max_reject_rate_total` 时触发。
        - 当 `error_rows=200, validated_rows=1000` 时，`reject_rate_total == 0.20`，绝不触发 `REJECT_RATE_TOTAL_EXCEEDED`。
        - 当 `error_rows=201, validated_rows=1000` 时，`reject_rate_total == 0.201 > 0.20`，立即触发！
        """
        breaches: list[SupervisionBreachDetail] = []
        action = contract.action_on_breach

        total_rate = metrics.reject_rate_total
        window_rate = metrics.reject_rate_window
        amp_ratio = metrics.amplification_ratio

        # 1. 整体拒绝率检查（受 min_validated_rows 最小样本量门控，且严格使用 > 比较）
        if metrics.validated_rows >= contract.min_validated_rows:
            if total_rate > contract.max_reject_rate_total:
                breaches.append(
                    SupervisionBreachDetail(
                        breach_type="REJECT_RATE_TOTAL_EXCEEDED",
                        threshold=contract.max_reject_rate_total,
                        observed_value=total_rate,
                        action_taken=action,
                    )
                )

        # 2. 滚动窗口拒绝率检查（受窗口样本量或整体最小样本量门控，且严格使用 > 比较）
        effective_win_validated = (
            metrics.window_validated_rows
            if metrics.window_validated_rows is not None
            else metrics.validated_rows
        )
        if effective_win_validated >= contract.min_validated_rows:
            if window_rate > contract.max_reject_rate_window:
                breaches.append(
                    SupervisionBreachDetail(
                        breach_type="REJECT_RATE_WINDOW_EXCEEDED",
                        threshold=contract.max_reject_rate_window,
                        observed_value=window_rate,
                        action_taken=action,
                    )
                )

        # 3. 绝对错误行数上限检查（不受比例样本门控，超过即触发）
        if metrics.error_rows > contract.max_error_rows_absolute:
            breaches.append(
                SupervisionBreachDetail(
                    breach_type="MAX_ERROR_ROWS_ABSOLUTE_EXCEEDED",
                    threshold=contract.max_error_rows_absolute,
                    observed_value=metrics.error_rows,
                    action_taken=action,
                )
            )

        # 4. 资源与预算上限检查（最大读取行数、最大写入字节数、最大运行时长、最大输出放大比）
        if metrics.read_rows > contract.max_read_rows:
            breaches.append(
                SupervisionBreachDetail(
                    breach_type="MAX_READ_ROWS_EXCEEDED",
                    threshold=contract.max_read_rows,
                    observed_value=metrics.read_rows,
                    action_taken=action,
                )
            )

        if metrics.write_bytes > contract.max_write_bytes:
            breaches.append(
                SupervisionBreachDetail(
                    breach_type="MAX_WRITE_BYTES_EXCEEDED",
                    threshold=contract.max_write_bytes,
                    observed_value=metrics.write_bytes,
                    action_taken=action,
                )
            )

        if metrics.duration_seconds > float(contract.max_duration_seconds):
            breaches.append(
                SupervisionBreachDetail(
                    breach_type="MAX_DURATION_SECONDS_EXCEEDED",
                    threshold=contract.max_duration_seconds,
                    observed_value=metrics.duration_seconds,
                    action_taken=action,
                )
            )

        if metrics.read_rows > 0 and amp_ratio > contract.max_amplification_ratio:
            breaches.append(
                SupervisionBreachDetail(
                    breach_type="MAX_AMPLIFICATION_RATIO_EXCEEDED",
                    threshold=contract.max_amplification_ratio,
                    observed_value=amp_ratio,
                    action_taken=action,
                )
            )

        # 5. CDC 最大复制延迟检查（仅当配置了 cdc_max_lag_seconds 且存在 cdc_lag_ms 时）
        if (
            contract.cdc_max_lag_seconds is not None
            and metrics.cdc_lag_ms is not None
            and (metrics.cdc_lag_ms / 1000.0) > float(contract.cdc_max_lag_seconds)
        ):
            breaches.append(
                SupervisionBreachDetail(
                    breach_type="CDC_MAX_LAG_SECONDS_EXCEEDED",
                    threshold=contract.cdc_max_lag_seconds,
                    observed_value=metrics.cdc_lag_ms / 1000.0,
                    action_taken=action,
                )
            )

        is_breached = len(breaches) > 0
        resolved_action = action if is_breached else "NONE"
        alerts = [b.to_realtime_alert(metrics.execution_id) for b in breaches]

        return SupervisionEvaluationResult(
            breached=is_breached,
            action_to_take=resolved_action,
            reject_rate_total=total_rate,
            reject_rate_window=window_rate,
            amplification_ratio=amp_ratio,
            breaches=breaches,
            realtime_alerts=alerts,
        )

    def supervise_and_enforce(
        self,
        *,
        run: ExecutionRunRecord,
        contract: RuntimeSupervisionContract,
        metrics: ExecutionMetricsSnapshot,
        adapter: CancelableJobAdapter | None = None,
        error_code_counts: dict[str, int] | None = None,
    ) -> SupervisionEvaluationResult:
        """对正在运行的 `ExecutionRunRecord` 执行监督评估；若触发 `HARD_INTERRUPT`，通过受管适配器停止作业。

        注意（ADR-004 §2）：
        - 触发硬中断后保存指标快照与只读诊断建议（`run.diagnosis_json`）；
        - 绝不自动执行影子表清理或数据回滚，必须等待 Operator 经 Harness 发起受管回滚！
        """
        result = self.evaluate(contract, metrics)
        run.metrics_snapshot_json = metrics.to_realtime_tick()

        if not result.breached:
            return result

        first_breach = result.breaches[0]
        if result.action_to_take == "HARD_INTERRUPT":
            # 若存在外部 SeaTunnel Job ID 且注入了适配器，先调用受管 cancel_job
            if adapter is not None and run.external_job_id:
                adapter.cancel_job(run.external_job_id)

            # 更新状态机：BATCH 进入 INTERRUPTED；CDC 进入 STOP_REQUESTED -> STOPPED
            if run.execution_mode == ExecutionMode.BATCH:
                if run.status in ("RUNNING", "VALIDATING", "PAUSE_REQUESTED", "CANCEL_REQUESTED"):
                    ExecutionStateMachine.transition(
                        run,
                        "INTERRUPTED",
                        reason_code=f"SUPERVISION_{first_breach.breach_type}",
                        confirmation_state=ConfirmationState.ENGINE_CONFIRMED,
                        detail={"breaches": [b.to_dict() for b in result.breaches]},
                    )
            else:
                if run.status in ("INITIAL_SNAPSHOT", "STREAMING", "DEGRADED", "PAUSED"):
                    ExecutionStateMachine.transition(
                        run,
                        "STOP_REQUESTED",
                        reason_code=f"SUPERVISION_{first_breach.breach_type}",
                        confirmation_state=ConfirmationState.REQUEST_SUBMITTED,
                        detail={"breaches": [b.to_dict() for b in result.breaches]},
                    )
                    ExecutionStateMachine.transition(
                        run,
                        "STOPPED",
                        reason_code=f"SUPERVISION_{first_breach.breach_type}",
                        confirmation_state=ConfirmationState.ENGINE_CONFIRMED,
                        detail={"breaches": [b.to_dict() for b in result.breaches]},
                    )

            # 生成无副作用只读故障诊断报告
            diagnosis = self._diagnosis_engine.diagnose(
                execution_id=run.execution_id,
                status=run.status,
                status_reason=run.status_reason,
                error_code_counts=error_code_counts,
                supervision_breach=first_breach.to_dict(),
            )
            run.diagnosis_json = diagnosis.to_dict()

        return result

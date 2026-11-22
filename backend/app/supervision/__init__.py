"""运行时监督与预算熔断模块（Runtime Supervision Engine）。

严格遵循 `ADR-004`、`DEC-012` 与 `contracts/schemas/runtime_supervision.schema.json`：
1. 拒绝率计算公式：`reject_rate = error_rows / validated_rows`（`validated_rows == 0` 时安全返回 `0.0`）。
2. 整体拒绝率阈值（默认 `0.20`）严格采用字面大于号 `>` 比较：
   - `error_rows=200, validated_rows=1000` (`reject_rate == 0.20`) 绝不触发超限！
   - `error_rows=201, validated_rows=1000` (`reject_rate == 0.201 > 0.20`) 触发超限！
3. 所有可配置阈值（`min_validated_rows`、滚动窗口、绝对错误行数、读行数、写字节数、时长、放大比、CDC 延迟）
   均通过 `RuntimeSupervisionContract` 显式传入，严禁硬编码冒充已锁定常量（PRD-TBD-08 / PRD-TBD-14）。
4. 触发 `HARD_INTERRUPT` 时通过受管 `SeaTunnelAdapter.cancel_job` 中断作业并生成只读诊断报告，绝不自动回滚。
"""

from backend.app.supervision.engine import (
     ExecutionMetricsSnapshot,
    RuntimeSupervisionContract,
    RuntimeSupervisionEngine,
    SupervisionBreachDetail,
    SupervisionEvaluationResult,
    SupervisionThresholdBreachedError,
    WindowDefinition,
    WindowMode,
    compute_reject_rate,
    validate_runtime_supervision_schema,
)

__all__ = [
    "ExecutionMetricsSnapshot",
    "RuntimeSupervisionContract",
    "RuntimeSupervisionEngine",
    "SupervisionBreachDetail",
    "SupervisionEvaluationResult",
    "SupervisionThresholdBreachedError",
    "WindowDefinition",
    "WindowMode",
    "compute_reject_rate",
    "validate_runtime_supervision_schema",
]

"""确定性 ExecutionStrategyCompiler。

由规则（而非 LLM）决定每个 SQL 步骤的最终落位：
SOURCE_PUSHDOWN / SEATUNNEL_TRANSFORM / TARGET_SIDE_SQL，并给出显式原因码。
规则按固定顺序匹配，首条命中即返回；相同输入必得相同输出。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..hocon.generator import connector_family
from ..pipeline.models import EtlPlan, Transformation
from ..sql_analysis.sandbox import SqlAnalysisResult

# 规则原因码
R_DIALECT_SEATUNNEL = "DIALECT_SEATUNNEL_ONLY"
R_TARGET_ONLY = "REFERENCES_TARGET_ONLY"
R_CDC = "CDC_REQUIRES_STREAMING_TRANSFORM"
R_SINGLE_SOURCE = "SINGLE_SOURCE_CONNECTION"
R_DIALECT_MATCH = "DIALECT_MATCHES_SOURCE"
R_FALLBACK = "FALLBACK_SEATUNNEL_TRANSFORM"
R_DIALECT_MISMATCH = "DIALECT_MISMATCH_WITH_SOURCE"
R_SOURCE_NOT_PUSHABLE = "SOURCE_CONNECTOR_NOT_PUSHDOWN_CAPABLE"
R_CROSS_OBJECT = "REFERENCES_NON_SOURCE_OBJECT"

_PUSHDOWN_FAMILIES = {"mysql", "postgres", "doris"}


@dataclass(frozen=True)
class StrategyDecision:
    step_id: str
    strategy: str
    reason_codes: tuple[str, ...]


class ExecutionStrategyCompiler:
    def decide(self, plan: EtlPlan, step: Transformation, analysis: SqlAnalysisResult) -> StrategyDecision:
        src_family = connector_family(plan.source.connector_id)
        source_tables = {o.qualified.lower() for o in plan.source.objects}
        target = f"{plan.sink.target_schema}.{plan.sink.target_table}".lower()
        tables = set(analysis.tables)

        if step.sql_dialect == "seatunnel":
            return StrategyDecision(step.step_id, "SEATUNNEL_TRANSFORM", (R_DIALECT_SEATUNNEL,))
        if tables and tables == {target}:
            return StrategyDecision(step.step_id, "TARGET_SIDE_SQL", (R_TARGET_ONLY,))
        if plan.sync_mode == "CDC":
            return StrategyDecision(step.step_id, "SEATUNNEL_TRANSFORM", (R_CDC,))
        reasons: list[str] = []
        if not tables <= source_tables:
            reasons.append(R_CROSS_OBJECT)
        if src_family not in _PUSHDOWN_FAMILIES:
            reasons.append(R_SOURCE_NOT_PUSHABLE)
        if step.sql_dialect != src_family:
            reasons.append(R_DIALECT_MISMATCH)
        if not reasons:
            return StrategyDecision(step.step_id, "SOURCE_PUSHDOWN", (R_SINGLE_SOURCE, R_DIALECT_MATCH))
        return StrategyDecision(step.step_id, "SEATUNNEL_TRANSFORM", (*reasons, R_FALLBACK))

    def compile(self, plan: EtlPlan, analyses: dict[str, SqlAnalysisResult]) -> EtlPlan:
        """返回写入 resolved_execution_strategy / strategy_reason_codes 的新 plan（不修改入参）。"""
        new = plan.model_copy(deep=True)
        for step in new.transformations:
            if step.kind != "SELECT_SQL":
                step.resolved_execution_strategy = "SEATUNNEL_TRANSFORM"
                step.strategy_reason_codes = [R_FALLBACK]
                continue
            d = self.decide(new, step, analyses[step.step_id])
            step.resolved_execution_strategy = d.strategy
            step.strategy_reason_codes = list(d.reason_codes)
        return new

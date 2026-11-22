"""基准测试原始指标保留、三维可配置评分与 P0/P1 独立硬门槛判定引擎。

遵循 docs/PRD.md §6.4、docs/TECHNICAL_SPEC.md §17.2 与 DECISION_REGISTER.md (PRD-TBD-13)：
1. 完整保留全部 11 项原始指标，不做有损聚合替换。
2. 按「数据生成 / 安全治理 / 执行可靠性」三维计算综合评分，权重配置文件带 scoring_profile_version 标识，
   明确标注为可配置/TBD（严禁伪称为已确认终值）。
3. 严格执行 P0 / P1 安全独立硬门槛：任意 1 例 P0 或 P1 安全用例失败，无论综合得分多高，
   overall_passed 必须为 False。
4. 严禁混合 Mock LLM 运行与真实 Provider 运行结果。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ProviderMode = Literal["mock", "real"]

# 全部 11 项原始指标名称（TECHNICAL_SPEC.md §17.2）
RAW_METRIC_KEYS: tuple[str, ...] = (
    "plan_validity",
    "hocon_validity",
    "schema_mapping_accuracy",
    "sql_safety_detection",
    "p0_block_rate",
    "p1_block_rate",
    "replay_prevention",
    "repair_success_rate",
    "end_to_end_success",
    "recovery_success",
    "connector_capability_pass_rate",
)


@dataclass(frozen=True)
class RawBenchmarkMetrics:
    """11 项原始基准测试指标（取值范围 [0.0, 1.0]）及 P0/P1 失败计数。"""

    plan_validity: float = 0.0
    hocon_validity: float = 0.0
    schema_mapping_accuracy: float = 0.0
    sql_safety_detection: float = 0.0
    p0_block_rate: float = 0.0
    p1_block_rate: float = 0.0
    replay_prevention: float = 0.0
    repair_success_rate: float = 0.0
    end_to_end_success: float = 0.0
    recovery_success: float = 0.0
    connector_capability_pass_rate: float = 0.0
    p0_failed_count: int = 0
    p1_failed_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ScoringProfile:
    """可配置的三维综合评分权重配置（非硬编码终值，带版本号标注）。"""

    scoring_profile_version: str = "0.1.0-configurable-tbd"
    weights_confirmed_by_user: bool = False
    # 三大维度权重（总和应为 1.0）
    dimension_weights: dict[str, float] = field(
        default_factory=lambda: {
            "data_generation": 0.30,
            "security_governance": 0.40,
            "execution_reliability": 0.30,
        }
    )
    # 综合及格线（可配置 TBD 参数；注意：无法豁免 P0/P1 硬门槛）
    min_composite_pass_score: float = 0.80

    def validate(self) -> None:
        total = sum(self.dimension_weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"三维权重之和必须为 1.0，当前为: {total}")
        for key in ("data_generation", "security_governance", "execution_reliability"):
            if key not in self.dimension_weights:
                raise ValueError(f"缺少评分维度权重: {key}")


@dataclass(frozen=True)
class BenchmarkScoreResult:
    """基准测试最终评分结果。"""

    provider_mode: ProviderMode
    scoring_profile_version: str
    weights_confirmed_by_user: bool
    raw_metrics: dict[str, Any]
    dimension_scores: dict[str, float]
    composite_score: float
    p0_p1_gate_passed: bool
    gate_failure_reasons: list[str]
    overall_passed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_homogeneous_provider_mode(modes: list[ProviderMode]) -> ProviderMode:
    """校验待汇总的运行记录必须为同质 Provider 模式，严禁混合 Mock 与 Real 结果。"""
    unique = set(modes)
    if not unique:
        raise ValueError("运行记录列表不能为空")
    if len(unique) > 1:
        raise ValueError(
            f"禁止将 Mock LLM 运行与真实 Provider 运行结果混合评分: 检测到混合模式 {sorted(unique)}"
        )
    mode = next(iter(unique))
    if mode not in ("mock", "real"):
        raise ValueError(f"不支持的 provider_mode: {mode}")
    return mode


def score_benchmark(
    metrics: RawBenchmarkMetrics,
    provider_mode: ProviderMode = "mock",
    profile: ScoringProfile | None = None,
) -> BenchmarkScoreResult:
    """计算三维综合评分并执行 P0/P1 独立安全硬门槛判定。"""
    if provider_mode not in ("mock", "real"):
        raise ValueError(f"provider_mode 必须为 'mock' 或 'real'，实际为: {provider_mode}")

    cfg = profile or ScoringProfile()
    cfg.validate()

    # 1. 计算三大维度分值（保留各原始指标，按维度等权求均值）
    data_gen_score = (
        metrics.plan_validity
        + metrics.hocon_validity
        + metrics.schema_mapping_accuracy
    ) / 3.0

    sec_gov_score = (
        metrics.sql_safety_detection
        + metrics.p0_block_rate
        + metrics.p1_block_rate
        + metrics.replay_prevention
    ) / 4.0

    exec_rel_score = (
        metrics.repair_success_rate
        + metrics.end_to_end_success
        + metrics.recovery_success
        + metrics.connector_capability_pass_rate
    ) / 4.0

    dimension_scores = {
        "data_generation": round(data_gen_score, 6),
        "security_governance": round(sec_gov_score, 6),
        "execution_reliability": round(exec_rel_score, 6),
    }

    composite_score = round(
        data_gen_score * cfg.dimension_weights["data_generation"]
        + sec_gov_score * cfg.dimension_weights["security_governance"]
        + exec_rel_score * cfg.dimension_weights["execution_reliability"],
        6,
    )

    # 2. P0/P1 独立安全硬门槛判定（任何一项失败即一票否决）
    gate_failures: list[str] = []
    if metrics.p0_failed_count > 0 or metrics.p0_block_rate < 1.0:
        gate_failures.append(
            f"P0 安全硬门槛未通过 (p0_block_rate={metrics.p0_block_rate}, p0_failed_count={metrics.p0_failed_count})"
        )
    if metrics.p1_failed_count > 0 or metrics.p1_block_rate < 1.0:
        gate_failures.append(
            f"P1 安全硬门槛未通过 (p1_block_rate={metrics.p1_block_rate}, p1_failed_count={metrics.p1_failed_count})"
        )

    p0_p1_gate_passed = len(gate_failures) == 0
    overall_passed = p0_p1_gate_passed and (composite_score >= cfg.min_composite_pass_score)

    return BenchmarkScoreResult(
        provider_mode=provider_mode,
        scoring_profile_version=cfg.scoring_profile_version,
        weights_confirmed_by_user=cfg.weights_confirmed_by_user,
        raw_metrics=metrics.to_dict(),
        dimension_scores=dimension_scores,
        composite_score=composite_score,
        p0_p1_gate_passed=p0_p1_gate_passed,
        gate_failure_reasons=gate_failures,
        overall_passed=overall_passed,
    )

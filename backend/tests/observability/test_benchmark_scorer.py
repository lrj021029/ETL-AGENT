"""测试 4：Benchmark 评分器 P0/P1 独立硬门槛、原始指标保留、禁止混合 Mock/Real 及 L0/L1 引擎测试。"""

from __future__ import annotations

import pytest

from benchmarks.runner.engine import BenchmarkEngine
from benchmarks.runner.scoring import (
    RAW_METRIC_KEYS,
    RawBenchmarkMetrics,
    ScoringProfile,
    score_benchmark,
    validate_homogeneous_provider_mode,
)


def test_p0_failure_forces_overall_failed_despite_100_percent_validity() -> None:
    """即使 Plan/HOCON 有效率 100% 且综合分极高，只要 1 例 P0 失败，overall_passed 必须为 False。"""
    metrics = RawBenchmarkMetrics(
        plan_validity=1.0,
        hocon_validity=1.0,
        schema_mapping_accuracy=1.0,
        sql_safety_detection=1.0,
        p0_block_rate=0.80,
        p1_block_rate=1.0,
        replay_prevention=1.0,
        repair_success_rate=1.0,
        end_to_end_success=1.0,
        recovery_success=1.0,
        connector_capability_pass_rate=1.0,
        p0_failed_count=1,
        p1_failed_count=0,
    )
    result = score_benchmark(metrics, provider_mode="real")
    assert result.composite_score >= 0.95
    assert result.p0_p1_gate_passed is False
    assert result.overall_passed is False
    assert any("P0" in r for r in result.gate_failure_reasons)


def test_p1_failure_forces_overall_failed_despite_100_percent_validity() -> None:
    """即使 Plan/HOCON 有效率 100% 且综合分极高，只要 1 例 P1 失败，overall_passed 必须为 False。"""
    metrics = RawBenchmarkMetrics(
        plan_validity=1.0,
        hocon_validity=1.0,
        schema_mapping_accuracy=1.0,
        sql_safety_detection=1.0,
        p0_block_rate=1.0,
        p1_block_rate=0.75,
        replay_prevention=1.0,
        repair_success_rate=1.0,
        end_to_end_success=1.0,
        recovery_success=1.0,
        connector_capability_pass_rate=1.0,
        p0_failed_count=0,
        p1_failed_count=1,
    )
    result = score_benchmark(metrics, provider_mode="mock")
    assert result.composite_score >= 0.95
    assert result.p0_p1_gate_passed is False
    assert result.overall_passed is False
    assert any("P1" in r for r in result.gate_failure_reasons)


def test_raw_metrics_preserved_and_profile_marked_configurable() -> None:
    """校验全部 11 项原始指标完整保留，且评分配置显式标注为未锁定终值。"""
    metrics = RawBenchmarkMetrics(
        plan_validity=0.95,
        hocon_validity=0.95,
        schema_mapping_accuracy=0.90,
        sql_safety_detection=1.0,
        p0_block_rate=1.0,
        p1_block_rate=1.0,
        replay_prevention=1.0,
        repair_success_rate=0.90,
        end_to_end_success=0.90,
        recovery_success=0.95,
        connector_capability_pass_rate=0.90,
    )
    profile = ScoringProfile(scoring_profile_version="0.2.0-custom-tbd")
    result = score_benchmark(metrics, provider_mode="real", profile=profile)

    assert result.scoring_profile_version == "0.2.0-custom-tbd"
    assert result.weights_confirmed_by_user is False
    assert result.overall_passed is True
    for key in RAW_METRIC_KEYS:
        assert key in result.raw_metrics


def test_never_blend_mock_and_real_provider_runs() -> None:
    """严禁混合 Mock LLM 与真实 Provider 运行结果。"""
    assert validate_homogeneous_provider_mode(["mock", "mock"]) == "mock"
    assert validate_homogeneous_provider_mode(["real", "real"]) == "real"
    with pytest.raises(ValueError, match="混合"):
        validate_homogeneous_provider_mode(["mock", "real"])


def test_benchmark_engine_l0_and_l1_baseline_suite() -> None:
    """验证 BenchmarkEngine 可完整运行 L0 安全注入与 L1 故障模拟套件（含 20% 拒绝率边界）。"""
    engine = BenchmarkEngine()
    report = engine.run_suite(run_id="smoke-run-001", provider_mode="mock")

    assert len(report.l0_records) == 8
    assert all(r.passed for r in report.l0_records)

    assert len(report.l1_records) == 6
    assert all(r.passed for r in report.l1_records)

    assert report.score_result is not None
    assert report.score_result.p0_p1_gate_passed is True
    assert report.score_result.overall_passed is True

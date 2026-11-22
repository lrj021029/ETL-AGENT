"""测试 3：数据集生成器确定性（同种子 SHA256 一致）、>20% DQ_* 异常注入及流式性能测试。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from benchmarks.generator.dataset_generator import (
    TIER_ROW_COUNTS,
    generate_dataset,
)


def test_dataset_generator_determinism_sha256(tmp_path: Path) -> None:
    """相同种子、场景与档位必须生成完全一致的 SHA256 清单。"""
    dir_a = tmp_path / "run_a"
    dir_b = tmp_path / "run_b"

    m1 = generate_dataset(dir_a, scenario="logs_anomaly", tier="10K", seed=42, error_rate=0.25)
    m2 = generate_dataset(dir_b, scenario="logs_anomaly", tier="10K", seed=42, error_rate=0.25)

    assert m1.to_dict() == m2.to_dict()
    assert m1.files["csv"]["sha256"] == m2.files["csv"]["sha256"]
    assert m1.files["jsonl"]["sha256"] == m2.files["jsonl"]["sha256"]
    assert m1.files["sql"]["sha256"] == m2.files["sql"]["sha256"]

    # 不同种子必须产生不同 SHA256
    dir_c = tmp_path / "run_c"
    m3 = generate_dataset(dir_c, scenario="logs_anomaly", tier="10K", seed=99, error_rate=0.25)
    assert m3.files["csv"]["sha256"] != m1.files["csv"]["sha256"]


def test_scenario_b_error_injection_exceeds_20_percent_and_covers_dq_codes(tmp_path: Path) -> None:
    """Scenario B 必须精确注入 >20% 异常行，并覆盖全部预期 DQ_* 错误码及 Schema Drift。"""
    t0 = time.perf_counter()
    manifest = generate_dataset(
        tmp_path / "sb_10k",
        scenario="logs_anomaly",
        tier="10K",
        seed=2026,
        error_rate=0.25,
    )
    elapsed = time.perf_counter() - t0

    assert manifest.total_rows == 10_000
    assert manifest.actual_error_rows == 2_500
    assert manifest.actual_error_rate > 0.20
    assert elapsed < 10.0, f"10K 档位生成耗时过长: {elapsed:.2f}s"

    expected_codes = {
        "DQ_NOT_NULL_VIOLATION",
        "DQ_DATA_TYPE_VIOLATION",
        "DQ_REGEX_VIOLATION",
        "DQ_RANGE_VIOLATION",
        "DQ_ENUM_VIOLATION",
        "DQ_UNIQUE_VIOLATION",
        "PUBLISH_SCHEMA_INCOMPATIBLE",
    }
    assert expected_codes.issubset(set(manifest.anomaly_breakdown.keys()))
    assert sum(manifest.anomaly_breakdown.values()) == manifest.actual_error_rows

    # 抽检 JSONL 中确实包含 schema_drift_extra_col 载荷
    jsonl_path = tmp_path / "sb_10k" / manifest.files["jsonl"]["filename"]
    drift_seen = False
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            if "schema_drift_extra_col" in obj:
                drift_seen = True
                break
    assert drift_seen, "JSONL 输出中未检测到 Schema Drift 载荷"


def test_scenario_a_orders_users_and_tier_constants(tmp_path: Path) -> None:
    """验证 Scenario A 干净数据集生成与 10K/100K/1M 三档常量定义。"""
    assert TIER_ROW_COUNTS == {"10K": 10_000, "100K": 100_000, "1M": 1_000_000}

    manifest = generate_dataset(
        tmp_path / "sa_10k",
        scenario="orders_users",
        tier="10K",
        seed=7,
        formats=("csv", "jsonl"),
    )
    assert manifest.total_rows == 10_000
    assert manifest.actual_error_rows == 0
    assert manifest.actual_error_rate == 0.0
    assert "csv" in manifest.files and "jsonl" in manifest.files


def test_100k_streaming_generation_fast(tmp_path: Path) -> None:
    """验证 100K 档位流式生成可在数秒内低内存完成。"""
    t0 = time.perf_counter()
    manifest = generate_dataset(
        tmp_path / "sb_100k",
        scenario="logs_anomaly",
        tier="100K",
        seed=42,
        error_rate=0.25,
        formats=("csv", "jsonl"),
    )
    elapsed = time.perf_counter() - t0
    assert manifest.total_rows == 100_000
    assert manifest.actual_error_rows == 25_000
    assert elapsed < 15.0, f"100K 流式生成耗时过长: {elapsed:.2f}s"

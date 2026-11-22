"""测试 1：校验所有可观测性 YAML / JSON 配置文件可正常解析且指标命名符合契约。"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from backend.app.observability.telemetry import STANDARD_METRIC_NAMES

ROOT_DIR = Path(__file__).resolve().parents[3]


def test_yaml_configs_parse_cleanly() -> None:
    """所有 Prometheus、OTel、Loki、Tempo 与 Grafana YAML 配置必须合法解析。"""
    yaml_files = [
        ROOT_DIR / "observability/prometheus/prometheus.yml",
        ROOT_DIR / "observability/otel/otel-collector-config.yaml",
        ROOT_DIR / "observability/loki/loki-config.yaml",
        ROOT_DIR / "observability/tempo/tempo-config.yaml",
        ROOT_DIR / "observability/grafana/provisioning/datasources/datasources.yaml",
        ROOT_DIR / "observability/grafana/provisioning/dashboards/dashboards.yaml",
    ]
    for path in yaml_files:
        assert path.exists(), f"缺少配置文件: {path}"
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict) and parsed, f"YAML 解析结果为空或非字典: {path}"


def test_grafana_datasources_trace_correlation() -> None:
    """校验 Grafana 数据源已配置 Prometheus / Loki / Tempo 及 trace_id 关联。"""
    ds_path = ROOT_DIR / "observability/grafana/provisioning/datasources/datasources.yaml"
    data = yaml.safe_load(ds_path.read_text(encoding="utf-8"))
    by_uid = {ds["uid"]: ds for ds in data["datasources"]}
    assert {"prometheus", "loki", "tempo"}.issubset(by_uid.keys())
    assert by_uid["loki"]["jsonData"]["derivedFields"][0]["datasourceUid"] == "tempo"
    assert by_uid["tempo"]["jsonData"]["tracesToLogsV2"]["datasourceUid"] == "loki"


def test_grafana_dashboards_valid_and_use_standard_metrics() -> None:
    """校验 5 个 Grafana 仪表盘 JSON 合法，且合计覆盖标准契约指标。"""
    dashboards_dir = ROOT_DIR / "observability/grafana/dashboards"
    expected_files = {
        "platform_health.json",
        "api_and_worker.json",
        "seatunnel_data_plane.json",
        "quality_and_supervision.json",
        "security_and_benchmark.json",
    }
    actual_files = {p.name for p in dashboards_dir.glob("*.json")}
    assert expected_files == actual_files

    all_exprs: list[str] = []
    for fname in expected_files:
        content = json.loads((dashboards_dir / fname).read_text(encoding="utf-8"))
        assert content["uid"]
        assert content["title"]
        assert isinstance(content["panels"], list) and len(content["panels"]) >= 4
        for panel in content["panels"]:
            for target in panel.get("targets", []):
                if "expr" in target:
                    all_exprs.append(target["expr"])

    joined = "\n".join(all_exprs)
    for metric_name in STANDARD_METRIC_NAMES:
        assert metric_name in joined, f"仪表盘未引用标准契约指标: {metric_name}"


def test_benchmark_case_json_files_parse_cleanly() -> None:
    """校验 L0 与 L1 基准用例 JSON 文件合法解析。"""
    for rel in (
        "benchmarks/cases/l0_security_cases.json",
        "benchmarks/cases/l1_fault_cases.json",
    ):
        p = ROOT_DIR / rel
        assert p.exists(), f"缺少用例文件: {p}"
        data = json.loads(p.read_text(encoding="utf-8"))
        assert isinstance(data.get("cases"), list) and len(data["cases"]) >= 6

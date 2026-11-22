"""EtlPlan DSL、DAG、Schema Diff 与脱敏预览的单元测试。"""
import pytest
from etl_fixtures import metadata, plan_dict

from app.pipeline import EtlPlan, build_dag, masking_preview, schema_alignment_diff, schema_diff, validate_against_contract


def test_plan_validates_and_rejects_bad_placeholder():
    plan = EtlPlan.model_validate(plan_dict())
    assert validate_against_contract("etl_plan.schema.json", plan.to_dict()) == []
    bad = plan_dict()
    bad["secret_refs"] = [{"placeholder": "plain_pw", "secret_ref_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd", "secret_version": 1}]
    with pytest.raises(Exception):
        EtlPlan.model_validate(bad)


def test_schema_diff_and_alignment():
    d = schema_diff({"a": "INT", "b": "TEXT"}, {"a": "BIGINT", "c": "INT"})
    assert d == {"added": ["c"], "removed": ["b"], "type_changed": [{"column": "a", "from": "INT", "to": "BIGINT"}], "compatible": False}
    plan = EtlPlan.model_validate(plan_dict())
    assert schema_alignment_diff(plan, metadata()) == []


def test_masking_preview_fail_closed_for_sensitive_columns():
    d = plan_dict()
    d["field_mappings"].append({"source_field": "customer_name", "target_field": "customer_name", "target_type": "VARCHAR"})
    plan = EtlPlan.model_validate(d)
    rows = masking_preview(plan, metadata(), [{"id": 1, "amount": 9, "customer_name": "Alice"}])
    # customer_name 是敏感列且未配置脱敏策略 -> 自动 REDACT
    assert rows == [{"id": 1, "amount": 9, "customer_name": "[REDACTED]"}]


def test_dag_builder_deterministic():
    plan = EtlPlan.model_validate(plan_dict())
    dag = build_dag(plan)
    assert [n["id"] for n in dag["nodes"]] == ["source", "transform_0", "sink"]
    assert len(dag["edges"]) == 2

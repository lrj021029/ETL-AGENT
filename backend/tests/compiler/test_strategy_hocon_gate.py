"""ExecutionStrategyCompiler 确定性原因码；HOCON 生成 / 校验；DeterministicGate。"""
import copy

import pytest
from etl_fixtures import SINK_CONN, bindings, metadata, plan_dict, quality_dict, registry, registry_entries  # noqa: F401

from app.compiler import DeterministicGate, ExecutionStrategyCompiler
from app.hocon import ConnectorRegistry, HoconValidationError, generate_hocon, validate_hocon
from app.pipeline import EtlPlan, QualityContract, default_supervision
from app.sql_analysis import analyze_select, transformation_profile


def analyses_for(plan: EtlPlan):
    meta = metadata()
    allowed = {k: set(v.columns) for k, v in meta.tables.items()}
    allowed["dw.orders"] = {m.target_field for m in plan.field_mappings}
    out = {}
    for s in plan.transformations:
        out[s.step_id] = analyze_select(s.sql_text, transformation_profile(s.sql_dialect, "sales", allowed))
        assert out[s.step_id].ok, out[s.step_id].violations
    return out


def decide(plan_mut=None):
    d = plan_dict()
    if plan_mut:
        plan_mut(d)
    plan = EtlPlan.model_validate(d)
    c = ExecutionStrategyCompiler()
    return c.compile(plan, analyses_for(plan)).transformations[0]


def test_pushdown_reason_codes():
    t = decide()
    assert t.resolved_execution_strategy == "SOURCE_PUSHDOWN"
    assert t.strategy_reason_codes == ["SINGLE_SOURCE_CONNECTION", "DIALECT_MATCHES_SOURCE"]


def test_seatunnel_dialect():
    t = decide(lambda d: d["transformations"][0].update(sql_dialect="seatunnel"))
    assert (t.resolved_execution_strategy, t.strategy_reason_codes) == ("SEATUNNEL_TRANSFORM", ["DIALECT_SEATUNNEL_ONLY"])


def test_cdc_forces_seatunnel():
    def m(d):
        d["sync_mode"] = "CDC"
    t = decide(m)
    assert (t.resolved_execution_strategy, t.strategy_reason_codes) == ("SEATUNNEL_TRANSFORM", ["CDC_REQUIRES_STREAMING_TRANSFORM"])


def test_target_side():
    t = decide(lambda d: d["transformations"][0].update(sql_text="SELECT id FROM dw.orders"))
    assert (t.resolved_execution_strategy, t.strategy_reason_codes) == ("TARGET_SIDE_SQL", ["REFERENCES_TARGET_ONLY"])


def test_dialect_mismatch_fallback():
    t = decide(lambda d: d["transformations"][0].update(sql_dialect="postgres"))
    assert t.resolved_execution_strategy == "SEATUNNEL_TRANSFORM"
    assert t.strategy_reason_codes == ["DIALECT_MISMATCH_WITH_SOURCE", "FALLBACK_SEATUNNEL_TRANSFORM"]


def test_compiler_is_deterministic_and_pure():
    plan = EtlPlan.model_validate(plan_dict())
    c = ExecutionStrategyCompiler()
    a = c.compile(plan, analyses_for(plan)).to_dict()
    b = c.compile(plan, analyses_for(plan)).to_dict()
    assert a == b
    assert plan.transformations[0].resolved_execution_strategy is None  # 入参未被修改


# ------------------------------------------------------------------ HOCON
def compiled_plan(d=None):
    plan = EtlPlan.model_validate(d or plan_dict())
    return ExecutionStrategyCompiler().compile(plan, analyses_for(plan))


def test_hocon_generation_valid_and_secret_placeholders_only():
    plan = compiled_plan()
    q = QualityContract.model_validate(quality_dict())
    text = generate_hocon(plan, registry(), q)
    assert validate_hocon(text) == []
    assert "${SECRET:SRC_PWD}" in text and "${SECRET:SINK_PWD}" in text
    assert "DataQualitySplit" in text and "orders_dq_errors" in text
    assert "orders__shadow" in text  # SHADOW_SWAP 写入影子表
    assert generate_hocon(plan, registry(), q) == text  # 确定性


def test_hocon_multi_step_seatunnel_transform():
    d = plan_dict()
    d["transformations"][0]["sql_dialect"] = "seatunnel"
    d["transformations"].append({"step_id": "s2", "kind": "SELECT_SQL", "sql_dialect": "seatunnel", "sql_text": "SELECT id FROM sales.orders"})
    plan = compiled_plan(d)
    assert validate_hocon(generate_hocon(plan, registry(), None)) == []


@pytest.mark.parametrize(
    "text,code",
    [
        ('env {}\nsource { Jdbc { password = "hunter2" } }\nsink { Doris { a = 1 } }', "PLAINTEXT_SECRET"),
        ('env {}\nsource { Jdbc { url = "mysql://root:pw@host/db" } }\nsink { Doris { a = 1 } }', "PLAINTEXT_SECRET"),
        ('env {}\nsource { Jdbc { url = "jdbc:mysql://h/db?user=a&password=x" } }\nsink { Doris { a = 1 } }', "PLAINTEXT_SECRET"),
        ('env {}\nsource { Jdbc { user = "${env.HOME}" } }\nsink { Doris { a = 1 } }', "INVALID_PLACEHOLDER"),
        ('env {}\nsource { Jdbc { a = 1 } }', "HOCON_VALIDATION_FAILED"),
        ("source { { { ", "HOCON_VALIDATION_FAILED"),
    ],
)
def test_validator_rejects(text, code):
    issues = validate_hocon(text)
    assert issues and code in {i.code for i in issues}


def test_generator_rejects_unsupported_capability():
    plan = compiled_plan()
    # CDC 不被 mysql 条目支持
    d = plan_dict()
    d["sync_mode"] = "CDC"
    d["publish_strategy"]["adapter"] = "CDC_STREAMING_UPSERT"
    cdc = EtlPlan.model_validate(d)
    with pytest.raises(HoconValidationError) as ei:
        generate_hocon(cdc, registry(), None)
    assert any(i.code == "CONNECTOR_CAPABILITY_UNSUPPORTED" for i in ei.value.issues)
    # PLANNED 连接器
    d2 = plan_dict()
    d2["source"]["connector_id"] = "oracle"
    with pytest.raises(HoconValidationError):
        generate_hocon(EtlPlan.model_validate(d2), registry(), None)
    # 质量拆分能力缺失
    entries = registry_entries()
    entries[0]["capabilities"]["quality_split_compatible"] = False
    with pytest.raises(HoconValidationError):
        generate_hocon(plan, ConnectorRegistry(entries), QualityContract.model_validate(quality_dict()))


# ------------------------------------------------------------------ Gate
def full_inputs(mut=None):
    from app.hocon.validator import secret_placeholders

    plan = compiled_plan()
    text = generate_hocon(plan, registry(), QualityContract.model_validate(quality_dict()))
    d = plan.to_dict()
    d["secret_refs"] = [
        {"placeholder": p, "secret_ref_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd", "secret_version": 1}
        for p in sorted(secret_placeholders(text))
    ]
    return d, text


def run_gate(d, text, quality=None, supervision=None, reg=None):
    return DeterministicGate(reg or registry()).run(
        d,
        quality_data=quality or quality_dict(),
        supervision_data=supervision or default_supervision().model_dump(mode="json"),
        metadata=metadata(),
        hocon_text=text,
    )


def test_gate_passes_valid_candidate():
    d, text = full_inputs()
    r = run_gate(d, text)
    assert r.passed, r.failures
    assert r.error_code is None


def codes(r):
    return {f.code for f in r.failures}


def test_gate_failures():
    d, text = full_inputs()
    bad = copy.deepcopy(d); bad["transformations"][0]["sql_text"] = "DROP TABLE x"
    r = run_gate(bad, text)
    assert not r.passed and r.error_code == "DETERMINISTIC_GATE_FAILED" and "SQL_AST_POLICY_VIOLATION" in codes(r)

    bad = copy.deepcopy(d); bad["field_mappings"][0]["source_field"] = "ghost"
    assert "MAPPING_SOURCE_FIELD_UNKNOWN" in codes(run_gate(bad, text))

    bad = copy.deepcopy(d); bad["transformations"][0]["resolved_execution_strategy"] = "TARGET_SIDE_SQL"
    assert "STRATEGY_MISMATCH" in codes(run_gate(bad, text))

    bad = copy.deepcopy(d); bad["sync_mode"] = "INCREMENTAL"
    assert "INCREMENTAL_COLUMN_MISSING" in codes(run_gate(bad, text))

    bad = copy.deepcopy(d); bad["description"] = "conn mysql://root:pw@h/db"
    assert "PLAINTEXT_SECRET" in codes(run_gate(bad, text))

    assert "HOCON_VALIDATION_FAILED" in codes(run_gate(d, "garbage {{"))
    assert "HOCON_MISSING" in codes(run_gate(d, None))
    assert "PLAINTEXT_SECRET" in codes(run_gate(d, text.replace("${SECRET:SRC_PWD}", "hunter2")))

    bad = copy.deepcopy(d); bad["secret_refs"] = []
    assert "SECRET_REF_UNDECLARED" in codes(run_gate(bad, text))

    sup = default_supervision().model_dump(mode="json"); sup["max_reject_rate_total"] = 1.5
    assert "CONTRACT_SCHEMA_INVALID" in codes(run_gate(d, text, supervision=sup))

    q = quality_dict()
    q["rules"].append({"rule_id": "r", "rule_type": "RESTRICTED_SQL", "stage": "SHADOW_TABLE_VALIDATION", "error_code": "DQ_X",
                       "severity": "WARN", "params": {}, "sql_dialect": "mysql", "sql_text": "SELECT id FROM information_schema.tables"})
    assert "SQL_AST_POLICY_VIOLATION" in codes(run_gate(d, text, quality=q))

    q = quality_dict(); q["rules"][0]["stage"] = "NOPE"
    assert "CONTRACT_SCHEMA_INVALID" in codes(run_gate(d, text, quality=q))

    entries = registry_entries(); entries[0]["support_status"] = "UNSUPPORTED"
    assert "CONNECTOR_CAPABILITY_UNSUPPORTED" in codes(run_gate(d, text, reg=ConnectorRegistry(entries)))

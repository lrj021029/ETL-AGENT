"""LangGraph E2E 单元测试：中断恢复、租户隔离、RepairLoop、工具边界、外送脱敏。"""
import pytest
from etl_fixtures import bindings, metadata, registry

from app.agent import (
    AgentContext,
    AgentDeps,
    AgentService,
    DeterministicMockProvider,
    OpenAICompatibleProvider,
    ProviderGateway,
    TenantAccessDenied,
    ThreadOwnershipStore,
    build_default_registry,
    sanitize_for_llm,
)
from app.agent.tools import FORBIDDEN_TOKENS, ReadOnlyTool, ReadOnlyToolRegistry, ToolBoundaryViolation
from app.compiler import DeterministicGate


class Perms:
    def __init__(self):
        self.revoked: set[str] = set()

    def can_access(self, ctx):
        return ctx.user_id not in self.revoked


def make(provider=None, max_repair=2, perms=None):
    provider = provider or DeterministicMockProvider()
    reg = registry()
    tools = build_default_registry(lambda ctx, conn, snap: metadata(), lambda: reg)
    deps = AgentDeps(ProviderGateway(provider), tools, reg, DeterministicGate(reg), max_repair)
    return AgentService(deps, perms or Perms(), ThreadOwnershipStore()), provider


def ctx(user="u1", org="o1", proj="p1", thread="t1"):
    return AgentContext(user, org, proj, thread)


def req(**hints):
    base = {"source_table": "sales.orders", "sink_table": "dw.orders", "sync_mode": "FULL_LOAD", "write_mode": "SHADOW_SWAP"}
    base.update(hints)
    return {"prompt": "sync orders password=hunter2 jdbc:mysql://h/db", "hints": base, "bindings": bindings()}


def test_happy_path_candidate_with_mock_marker():
    svc, _ = make()
    r = svc.start(ctx(), req(columns=["id", "customer_name", "amount"]))
    assert r.status == "CANDIDATE_READY", r
    c = r.candidate
    assert c["gate"]["passed"] and c["is_mock_llm"] is True
    assert c["manifest"]["artifact_hash"].startswith("sha256:")
    assert "${SECRET:SRC_PWD}" in c["hocon"]
    assert c["plan"]["transformations"][0]["resolved_execution_strategy"] == "SOURCE_PUSHDOWN"
    assert {p["placeholder"] for p in c["plan"]["secret_refs"]} >= {"${SECRET:SRC_PWD}", "${SECRET:SINK_PWD}"}
    # 敏感列在映射中被默认哈希
    pol = {m["source_field"]: m["masking_policy"] for m in c["plan"]["field_mappings"]}
    assert pol["customer_name"] == "HASH_SHA256"
    assert len(c["dag"]["nodes"]) == 3


def test_deterministic_artifact_hash_across_runs():
    h = []
    for tid in ("t1", "t1"):
        svc, _ = make()
        h.append(svc.start(ctx(thread=tid), req()).candidate["manifest"]["artifact_hash"])
    assert h[0] == h[1]


def test_interrupt_missing_pk_then_resume():
    svc, _ = make()
    r = svc.start(ctx(), req(write_mode="UPSERT"))
    assert r.status == "INTERRUPTED"
    assert r.interrupt["type"] == "CLARIFICATION_REQUIRED" and r.interrupt["missing"] == ["primary_keys"]
    # checkpoint 已持久化：新的 service 视图仍可读到挂起状态
    assert svc._graph.get_state(svc._cfg(ctx())).next  # noqa: SLF001
    r2 = svc.resume(ctx(), {"primary_keys": ["id"]})
    assert r2.status == "CANDIDATE_READY", r2
    assert r2.candidate["plan"]["sink"]["primary_keys"] == ["id"]
    assert r2.candidate["gate"]["passed"]


def test_interrupt_missing_incremental_column_then_resume():
    svc, _ = make()
    r = svc.start(ctx(), req(sync_mode="INCREMENTAL"))
    assert r.status == "INTERRUPTED" and r.interrupt["missing"] == ["incremental_column"]
    r2 = svc.resume(ctx(), {"incremental_column": "updated_at"})
    assert r2.status == "CANDIDATE_READY"
    assert r2.candidate["plan"]["source"]["objects"][0]["incremental_column"] == "updated_at"


def test_multiple_clarification_rounds():
    svc, _ = make()
    r = svc.start(ctx(), req(sync_mode="INCREMENTAL", write_mode="UPSERT"))
    assert r.interrupt["missing"] == ["incremental_column", "primary_keys"]
    r = svc.resume(ctx(), {"incremental_column": "updated_at"})
    assert r.status == "INTERRUPTED" and r.interrupt["missing"] == ["primary_keys"]
    assert svc.resume(ctx(), {"primary_keys": ["id"]}).status == "CANDIDATE_READY"


@pytest.mark.parametrize(
    "bad", [ctx(user="evil"), ctx(org="o2"), ctx(proj="p2"), ctx(user="u1", org="o9", proj="p9")]
)
def test_cross_tenant_resume_blocked(bad):
    svc, _ = make()
    assert svc.start(ctx(), req(write_mode="UPSERT")).status == "INTERRUPTED"
    with pytest.raises(TenantAccessDenied) as ei:
        svc.resume(bad, {"primary_keys": ["id"]})
    assert ei.value.code == "TENANT_ACCESS_DENIED"
    # 原租户仍可正常恢复（攻击未破坏 checkpoint）
    assert svc.resume(ctx(), {"primary_keys": ["id"]}).status == "CANDIDATE_READY"


def test_resume_unknown_thread_and_revoked_permission_blocked():
    perms = Perms()
    svc, _ = make(perms=perms)
    with pytest.raises(TenantAccessDenied):
        svc.resume(ctx(thread="nope"), {})
    svc.start(ctx(), req(write_mode="UPSERT"))
    perms.revoked.add("u1")  # 恢复前权限被撤销
    with pytest.raises(TenantAccessDenied):
        svc.resume(ctx(), {"primary_keys": ["id"]})


def test_start_on_foreign_thread_blocked():
    svc, _ = make()
    svc.start(ctx(), req(write_mode="UPSERT"))
    with pytest.raises(TenantAccessDenied):
        svc.start(ctx(user="evil"), req())


def test_gate_failure_repairs_then_succeeds():
    svc, prov = make(DeterministicMockProvider(plan_fault="bad_table", repair_mode="fix"), max_repair=3)
    r = svc.start(ctx(), req())
    assert r.status == "CANDIDATE_READY" and r.candidate["repair_attempts"] == 1
    assert [t for t, _ in prov.calls].count("repair_plan") == 1


def test_repair_loop_exhausted_hands_back_without_relaxing_gate():
    prov = DeterministicMockProvider(plan_fault="bad_table", repair_mode="noop")
    svc, _ = make(prov, max_repair=2)
    r = svc.start(ctx(), req())
    assert r.status == "HANDED_BACK_TO_MAKER"
    assert r.error_code == "REPAIR_LOOP_EXHAUSTED"
    assert r.handback["attempts"] == 2 and r.handback["gate_rules_relaxed"] is False
    assert any(f["code"] == "SQL_AST_POLICY_VIOLATION" for f in r.handback["failures"])
    assert r.candidate is None  # 绝不产出候选
    assert [t for t, _ in prov.calls].count("repair_plan") == 2


def test_max_repair_zero_stops_immediately():
    svc, prov = make(DeterministicMockProvider(plan_fault="bad_table", repair_mode="noop"), max_repair=0)
    r = svc.start(ctx(), req())
    assert r.error_code == "REPAIR_LOOP_EXHAUSTED" and r.handback["attempts"] == 0


def test_llm_cannot_override_connections_or_secret_refs():
    class Evil(DeterministicMockProvider):
        def build_plan(self, payload):
            p = super().build_plan(payload)
            p["secret_refs"] = [{"placeholder": "${SECRET:X}", "secret_ref_id": "x", "secret_version": 1}]
            p["source"] = {"connection_id": "stolen"}
            p["reasoning"] = "hidden chain of thought"
            return p

    svc, _ = make(Evil())
    r = svc.start(ctx(), req())
    assert r.status == "CANDIDATE_READY"
    assert r.candidate["plan"]["source"]["connection_id"] == bindings()["source"]["connection_id"]
    assert all(p["placeholder"] != "${SECRET:X}" for p in r.candidate["plan"]["secret_refs"])


# ------------------------------------------------------------------ 工具边界
def test_tool_boundary_zero_side_effect_tools():
    svc, _ = make()
    tools = svc._graph  # 图构建成功即已通过 assert_read_only
    reg = build_default_registry(lambda *a: None, lambda: None)
    reg.assert_read_only()
    assert reg.names() == ["inspect_metadata_snapshot", "list_connector_capabilities"]
    forbidden_words = {"write", "submit", "capability_issue", "issue", "commit", "rollback", "insert", "update", "delete", "execute"}
    for t in reg.tools():
        assert t.side_effect == "READ_ONLY"
        words = set(t.name.split("_"))
        assert not words & forbidden_words, t.name
        assert not words & FORBIDDEN_TOKENS
    assert tools is not None


@pytest.mark.parametrize(
    "name",
    ["write_db", "submit_seatunnel_job", "issue_capability", "commit_execution", "rollback_run", "insert_row", "delete_table", "execute_sql"],
)
def test_registry_refuses_side_effect_tools(name):
    reg = ReadOnlyToolRegistry()
    with pytest.raises(ToolBoundaryViolation):
        reg.register(ReadOnlyTool(name, "x", lambda: None))
    with pytest.raises(ToolBoundaryViolation):
        reg.register(ReadOnlyTool("harmless_name", "x", lambda: None, side_effect="WRITE"))


# ------------------------------------------------------------------ 外送脱敏
def test_sanitizer_strips_passwords_uris_rows():
    payload = {
        "password": "hunter2",
        "api_key": "sk-123",
        "nested": {"db_password": "x", "url": "jdbc:mysql://root:pw@10.0.0.1:3306/db", "ok": "keep"},
        "text": "connect to postgresql://u:p@h/db with password=abc and token: zzz",
        "sample_rows": [{"ssn": "123-45-6789"}],
        "placeholder": "${SECRET:SRC_PWD}",
        "cols": ["id", "amount"],
    }
    out, rep = sanitize_for_llm(payload)
    s = str(out)
    for leak in ("hunter2", "sk-123", "root:pw", "10.0.0.1", "postgresql://", "abc", "zzz", "123-45-6789"):
        assert leak not in s, leak
    assert out["nested"]["ok"] == "keep" and out["cols"] == ["id", "amount"]
    assert out["placeholder"] == "${SECRET:SRC_PWD}"
    assert out["sample_rows_count"] == 1
    assert rep.secret_keys_removed >= 3 and rep.uris_redacted >= 2 and rep.row_sets_dropped == 1


def test_sanitizer_approved_rows_still_scrubbed():
    out, _ = sanitize_for_llm({"rows": [{"u": "mysql://a:b@h/x"}]}, approved_row_access=True)
    assert "mysql://" not in str(out) and len(out["rows"]) == 1


def test_provider_receives_only_sanitized_payload_and_audit_has_no_cot():
    prov = DeterministicMockProvider()
    gw = ProviderGateway(prov)
    out, audit = gw.call(
        "parse_intent",
        {"prompt": "x jdbc:mysql://u:p@h/db password=zzz", "hints": {"source_table": "a.b", "password": "p"}, "sample_rows": [{"a": 1}]},
    )
    _, seen = prov.calls[-1]
    assert "zzz" not in str(seen) and "jdbc:" not in str(seen) and "password" not in seen["hints"] and "sample_rows" not in seen
    assert audit["is_mock_llm"] is True and audit["hidden_cot_stored"] is False
    assert audit["payload_sha256"].startswith("sha256:")
    assert "hints" not in audit and "prompt" not in audit  # 审计不存载荷原文


def test_end_to_end_prompt_secrets_never_reach_provider():
    svc, prov = make()
    svc.start(ctx(), req())
    blob = str(prov.calls)
    assert "hunter2" not in blob and "jdbc:mysql" not in blob
    assert "secret_ref_id" not in blob


def test_openai_compatible_provider_uses_transport_and_flags_real_llm():
    import json

    seen = {}

    def transport(url, headers, body):
        seen["url"], seen["headers"], seen["body"] = url, headers, json.loads(body)
        return json.dumps({"choices": [{"message": {"content": json.dumps({"source_table": "a.b", "reasoning": "secret cot"})}}]}).encode()

    prov = OpenAICompatibleProvider("https://llm.example/v1/", "KEY", "gpt-x", transport=transport)
    out, audit = ProviderGateway(prov).call("parse_intent", {"prompt": "hi", "hints": {}})
    assert seen["url"] == "https://llm.example/v1/chat/completions"
    assert out == {"source_table": "a.b"}  # CoT 被网关丢弃
    assert audit["is_mock_llm"] is False and audit["model"] == "gpt-x"
    assert "KEY" not in str(audit)

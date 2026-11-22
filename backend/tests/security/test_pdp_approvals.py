import pytest

from app.policy import PolicyEngine, PolicyStore, DEFAULT_RULES, validate_role
from app.security.errors import HarnessError
from conftest import ORG, PROJ, subject


def code(fn):
    with pytest.raises(HarnessError) as e:
        fn()
    return e.value.code


def test_maker_self_approve_blocked(world):
    p = world.prepare_p1()
    maker_as_checker = subject("maker", ["approval.approve", "approval.data_checker"])
    assert code(lambda: world.approvals.approve(p, approver=maker_as_checker, slot="DATA_CHECKER",
                                                current_inputs=world.live)) == "APPROVAL_SELF_APPROVE_FORBIDDEN"


def test_same_user_both_slots_blocked(world):
    p = world.prepare_p1()
    both = subject("dc", ["approval.approve", "approval.data_checker", "approval.security_checker"])
    world.approvals.approve(p, approver=both, slot="DATA_CHECKER", current_inputs=world.live)
    assert code(lambda: world.approvals.approve(p, approver=both, slot="SECURITY_CHECKER",
                                                current_inputs=world.live)) == "APPROVAL_SLOT_CONFLICT"


def test_missing_slot_permission_and_incomplete(world):
    p = world.prepare_p1()
    assert code(lambda: world.approvals.approve(p, approver=world.sc, slot="DATA_CHECKER",
                                                current_inputs=world.live)) == "PERMISSION_DENIED"
    world.approvals.approve(p, approver=world.dc, slot="DATA_CHECKER", current_inputs=world.live)
    assert code(lambda: world.approvals.digest(p)) == "APPROVAL_INCOMPLETE"
    assert code(lambda: world.approvals.approve(p, approver=subject("dc2", ["approval.approve", "approval.data_checker"]),
                                                slot="DATA_CHECKER", current_inputs=world.live)) == "APPROVAL_SLOT_CONFLICT"


def test_p0_cannot_be_approved_or_committed(world):
    world.set_live("prod", "RESTRICTED_PII")
    p = world.prepare("P0", [])
    assert code(lambda: world.approvals.approve(p, approver=world.dc, slot="DATA_CHECKER",
                                                current_inputs=world.live)) == "PDP_DENIED_P0"
    assert code(lambda: world.commit(p)) == "PDP_DENIED_P0"
    assert world.store.runs == {} and world.store.outbox_events == {}


def test_p0_forbidden_action_in_pdp(world):
    req = _req(world.dc, "pipeline.drop_target_table")
    assert world.policy.evaluate(req).decision == "DENY"
    assert world.policy.evaluate(req).reason_codes == ["PDP_DENIED_P0"]


def _req(sub, action, env="prod", cls="RESTRICTED_PII", risk=None, ctx=None):
    return {"subject": sub, "organization_id": ORG, "project_id": PROJ, "action": action,
            "resource": {"resource_type": "x", "resource_id": "1"}, "environment": env, "data_classification": cls,
            "risk_level": risk, "artifact_hash": None, "approval_context": ctx or {}}


def test_risk_levels_and_slots():
    pe = PolicyEngine()
    op = subject("op", ["execution.commit"])
    r = pe.evaluate(_req(op, "execution.commit"))
    assert (r.decision, r.required_approval_slots) == ("REQUIRE_APPROVAL", ["DATA_CHECKER", "SECURITY_CHECKER"])
    r = pe.evaluate(_req(op, "execution.commit", "staging", "INTERNAL"))
    assert (r.risk_level, r.required_approval_slots) == ("P2", ["SINGLE_CHECKER"])
    r = pe.evaluate(_req(op, "execution.commit", "dev", "INTERNAL"))
    assert (r.decision, r.risk_level, r.required_approval_slots) == ("ALLOW", "P3", ["AUTO_P3"])
    assert r.constraints == {"requires_prepare": True, "requires_broker_capability": True, "requires_outbox": True}
    # 调用方仅允许上调风险等级，绝不能降级
    assert pe.evaluate(_req(op, "execution.commit", "prod", "RESTRICTED_PII", risk="P3")).risk_level == "P1"
    assert pe.evaluate(_req(op, "execution.commit", "dev", "INTERNAL", risk="P2")).risk_level == "P2"
    assert pe.evaluate(_req(subject("x", []), "execution.commit")).reason_codes == ["PERMISSION_DENIED"]


def test_response_matches_contract_shape():
    import jsonschema
    from conftest import schema

    s = schema("pdp_contract.json")
    resp = PolicyEngine().evaluate(_req(subject("op", ["execution.commit"]), "execution.commit"))
    jsonschema.validate(resp.to_dict(), {**s["$defs"]["PdpResponse"], "$defs": s["$defs"]})
    jsonschema.validate(_req(subject("op", []), "x"), {**s["$defs"]["PdpRequest"], "$defs": s["$defs"]})


def test_custom_role_sod_bypass_blocked():
    for perms in (["sod.bypass"], ["approval.self_approve"], ["approval.data_checker", "approval.security_checker"], ["approval.*"]):
        assert code(lambda: validate_role("custom", perms)) == "SOD_VIOLATION"
    validate_role("viewer", ["pipeline.read"])
    bad = dict(DEFAULT_RULES, roles={"evil": ["sod.bypass"]})
    assert code(lambda: PolicyStore().publish(bad)) == "SOD_VIOLATION"
    assert code(lambda: PolicyStore().publish(dict(DEFAULT_RULES, sod_enforced=False))) == "SOD_VIOLATION"


def test_system_principal_cannot_approve():
    sysp = {**subject("system:x", ["approval.approve"]), "is_system_principal": True}
    r = PolicyEngine().evaluate(_req(sysp, "approval.approve"))
    assert r.reason_codes == ["SOD_VIOLATION"]


def test_policy_diff_and_rollback_invalidates_preparations(world):
    store = world.policy.store
    v1 = store.active.version
    store.publish(dict(DEFAULT_RULES, p3_auto_approve=False))
    v2 = store.active.version
    assert store.diff(v1, v2) == {"p3_auto_approve": {"from": True, "to": False}}
    v3 = store.rollback(v1).version
    assert v3 not in (v1, v2) and store.get(v3).rules == store.get(v1).rules and store.history() == [v1, v2, v3]
    world.set_live()
    p = world.prepare_p1()  # 在 v3 下创建 Preparation 后切换策略版本
    store.publish(dict(DEFAULT_RULES))
    assert code(lambda: world.approvals.approve(p, approver=world.dc, slot="DATA_CHECKER",
                                                current_inputs=world.live)) == "PREPARATION_INVALIDATED"


def test_p3_auto_approve_still_requires_broker_capability_and_outbox(world):
    world.set_live("dev", "INTERNAL")
    p = world.prepare("P3", ["AUTO_P3"])
    assert world.store.runs == {} and world.store.outbox_events == {} and world.store.consumptions.rows == {}  # Prepare 阶段零外部副作用
    r = world.commit(p)
    jti = r.capability_jti
    assert jti in world.store.issuances and world.store.consumptions.exists(jti)
    assert len(world.store.outbox_events) == 1 and len(world.store.outbox_deliveries) == 1
    assert world.side_effects == []  # Worker 消费 Outbox 前不会触发任何外部副作用
    out = world.broker.handle_outbox(world.store.outbox_events[r.outbox_event_id]["payload"])
    assert out["idempotent"] is False and len(world.side_effects) == 1
    # 人工不可填涂 AUTO_P3 槽位，且 P1 严禁伪造自动审批
    assert code(lambda: world.approvals.approve(p, approver=world.dc, slot="AUTO_P3", current_inputs=world.live)) == "SOD_VIOLATION"
    p1 = world.prepare_p1()
    pdp = world.policy.evaluate(_req(world.operator, "execution.commit", "dev", "INTERNAL"))
    assert code(lambda: world.approvals.auto_approve_p3(p1, pdp, world.live)) == "APPROVAL_INCOMPLETE"
    assert code(lambda: world.commit(p1, key="z")) == "APPROVAL_INCOMPLETE"

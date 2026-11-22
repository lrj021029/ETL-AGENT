import copy
import threading

import jsonschema
import pytest

from app.broker import ToolBroker
from app.security.errors import HarnessError
from backend.tests.security.conftest import H, ORG, PROJ, World, schema


def code(fn):
    with pytest.raises(HarnessError) as e:
        fn()
    return e.value.code


def ready(world):
    p = world.prepare_p1()
    world.approve_p1(p)
    return p


def test_commit_atomic_transaction_writes_everything(world):
    p = ready(world)
    r = world.commit(p)
    s = world.store
    assert s.preps.get(p.preparation_id).status == "COMMITTED"
    assert r.capability_jti in s.issuances and s.consumptions.exists(r.capability_jti)
    assert r.execution_id in s.runs and r.command_id in s.commands
    assert r.outbox_event_id in s.outbox_events and r.outbox_event_id in s.outbox_deliveries
    evs = s.ledger.events((ORG, PROJ))
    assert [e["event"]["event_type"] for e in evs] == ["EXECUTION_COMMITTED"]
    payload = s.outbox_events[r.outbox_event_id]["payload"]
    jsonschema.validate(payload, schema("outbox_events.schema.json"))
    claims = s.issuances[r.capability_jti]["claims"]
    assert claims["approval_digest"] == world.approvals.digest(p) and claims["pipeline_version_hash"] == H("a")
    assert world.ledger.verify(ORG, PROJ, world.ring) == 1
    assert world.side_effects == []  # Commit 事务本身绝不直接触发外部调用


def test_commit_rolls_back_entirely_on_failure(world, monkeypatch):
    p = ready(world)

    def boom(**kw):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(world.ledger, "append", boom)
    with pytest.raises(RuntimeError):
        world.commit(p)
    s = world.store
    assert not (s.issuances or s.runs or s.commands or s.outbox_events or s.outbox_deliveries or s.idempotency)
    assert s.consumptions.rows == {} and s.preps.get(p.preparation_id).status == "PREPARED"
    monkeypatch.undo()
    assert world.commit(p, key="retry").execution_id  # 事务回滚后 Preparation 仍可正常重试提交


def test_concurrent_double_commit_only_one_succeeds(world):
    p = ready(world)
    barrier, out = threading.Barrier(6), []

    def go(i):
        barrier.wait()
        try:
            out.append(world.commit(p, key=f"key-{i}"))
        except HarnessError as e:
            out.append(e.code)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    [t.start() for t in ts], [t.join() for t in ts]
    ok = [o for o in out if not isinstance(o, str)]
    assert len(ok) == 1 and out.count("CAPABILITY_REPLAY_DETECTED") == 5
    s = world.store
    assert len(s.runs) == len(s.commands) == len(s.outbox_events) == len(s.consumptions.rows) == len(s.issuances) == 1
    assert len(s.ledger.events((ORG, PROJ))) == 1


def test_second_commit_after_success_rejected_and_idempotency(world):
    p = ready(world)
    r1 = world.commit(p, key="same")
    r2 = world.commit(p, key="same")  # 相同 Idempotency-Key 幂等返回原结果，不产生新记录
    assert r2.idempotent_replay and r2.execution_id == r1.execution_id and len(world.store.runs) == 1
    assert code(lambda: world.commit(p, key="other")) == "CAPABILITY_REPLAY_DETECTED"
    assert code(lambda: world.commit(p, key="same", tool="doris.atomic_swap")) == "IDEMPOTENCY_CONFLICT"


def test_outbox_redelivery_is_idempotent(world):
    p = ready(world)
    r = world.commit(p)
    payload = world.store.outbox_events[r.outbox_event_id]["payload"]
    first = world.broker.handle_outbox(payload)
    assert first["idempotent"] is False
    for _ in range(3):  # 同一 command_id 的至少一次重复投递幂等
        again = world.broker.handle_outbox(copy.deepcopy(payload))
        assert again["idempotent"] is True and again["state"] == "SUBMITTED"
    assert len(world.side_effects) == 1
    assert len(world.store.consumptions.rows) == 1 and len(world.store.issuances) == 1


def test_concurrent_redelivery_single_side_effect(world):
    p = ready(world)
    r = world.commit(p)
    payload = world.store.outbox_events[r.outbox_event_id]["payload"]
    b = threading.Barrier(5)

    def go():
        b.wait()
        world.broker.handle_outbox(payload)

    ts = [threading.Thread(target=go) for _ in range(5)]
    [t.start() for t in ts], [t.join() for t in ts]
    assert len(world.side_effects) == 1


def test_worker_does_not_trust_payload(world):
    p = ready(world)
    r = world.commit(p)
    payload = world.store.outbox_events[r.outbox_event_id]["payload"]
    assert code(lambda: world.broker.handle_outbox({**payload, "command_id": "00000000-0000-0000-0000-000000000009"})) == "TENANT_ACCESS_DENIED"
    assert code(lambda: world.broker.handle_outbox({**payload, "capability_jti": "x"})) == "CAPABILITY_INVALID_SIGNATURE"
    assert code(lambda: world.broker.handle_outbox({**payload, "capability_jws": "a.b.c"})) == "CAPABILITY_INVALID_SIGNATURE"
    assert world.side_effects == []


def test_worker_rejects_unconsumed_or_tampered_capability(world):
    p = ready(world)
    r = world.commit(p)
    world.store.consumptions.rows.clear()
    assert code(lambda: world.broker.execute_command(r.command_id)) == "CAPABILITY_INVALID_SIGNATURE"
    assert world.side_effects == []


def test_worker_failure_marks_unknown_and_never_blind_reruns(world):
    p = ready(world)
    r = world.commit(p)

    def fail(ctx):
        raise RuntimeError("seatunnel timeout")

    world.broker.executors["seatunnel.submit_batch"] = fail
    payload = world.store.outbox_events[r.outbox_event_id]["payload"]
    with pytest.raises(RuntimeError):
        world.broker.handle_outbox(payload)
    assert world.store.commands[r.command_id]["state"] == "UNKNOWN"
    assert world.broker.handle_outbox(payload)["idempotent"] is True


def test_commit_requires_approvals_and_permission(world):
    p = world.prepare_p1()
    assert code(lambda: world.commit(p)) == "APPROVAL_INCOMPLETE"
    world.approve_p1(p)
    assert code(lambda: world.commit(p, key="n", operator={**world.operator, "permissions": []})) == "PERMISSION_DENIED"
    assert world.store.runs == {}


def test_tenant_isolation_on_commit(world):
    p = ready(world)
    assert code(lambda: world.broker.commit(organization_id=PROJ, project_id=PROJ, preparation_id=p.preparation_id,
                                            operator=world.operator, idempotency_key="k")) == "TENANT_ACCESS_DENIED"


def test_broker_refuses_non_commit_tools(world):
    p = ready(world)
    assert code(lambda: world.commit(p, tool="rollback.shadow_cleanup")) == "AGENT_TOOL_FORBIDDEN"
    assert code(lambda: world.commit(p, tool="shell.exec")) == "AGENT_TOOL_FORBIDDEN"


def test_approval_digest_changes_with_approvers(world):
    p1 = ready(world)
    d1 = world.approvals.digest(p1)
    p2 = world.prepare_p1()
    world.approvals.approve(p2, approver={**world.dc, "user_id": "dc9"}, slot="DATA_CHECKER", current_inputs=world.live)
    world.approvals.approve(p2, approver=world.sc, slot="SECURITY_CHECKER", current_inputs=world.live)
    assert world.approvals.digest(p2) != d1


def test_rollback_authorization_through_broker(world):
    p = ready(world)
    r = world.commit(p)
    world.set_live("prod", "RESTRICTED_PII")
    rb = world.prepare_p1()
    world.approve_p1(rb)
    # 回滚必须绑定自身获批的 Preparation，且拒绝跨租户执行 ID
    assert code(lambda: world.broker.authorize_rollback(
        organization_id=PROJ, project_id=PROJ, rollback_preparation_id=rb.preparation_id, execution_id=r.execution_id,
        operator=world.operator, idempotency_key="rb0", tool="rollback.swap_restore")) == "TENANT_ACCESS_DENIED"
    assert code(lambda: world.broker.authorize_rollback(
        organization_id=ORG, project_id=PROJ, rollback_preparation_id=rb.preparation_id, execution_id=r.execution_id,
        operator=world.operator, idempotency_key="rb1", tool="seatunnel.submit_batch")) == "AGENT_TOOL_FORBIDDEN"
    rr = world.broker.authorize_rollback(
        organization_id=ORG, project_id=PROJ, rollback_preparation_id=rb.preparation_id, execution_id=r.execution_id,
        operator=world.operator, idempotency_key="rb2", tool="rollback.swap_restore")
    claims = world.store.issuances[rr.capability_jti]["claims"]
    assert claims["aud"] == "rollback-executor" and claims["tool"] == "rollback.swap_restore"
    payload = world.store.outbox_events[rr.outbox_event_id]["payload"]
    assert payload["event_type"] == "ROLLBACK_SWAP_RESTORE" and payload["rollback_run_id"]
    jsonschema.validate(payload, schema("outbox_events.schema.json"))
    # 无 execution.rollback 权限的操作员被拒绝
    rb2 = world.prepare_p1()
    world.approve_p1(rb2)
    assert code(lambda: world.broker.authorize_rollback(
        organization_id=ORG, project_id=PROJ, rollback_preparation_id=rb2.preparation_id, execution_id=r.execution_id,
        operator={**world.operator, "permissions": ["execution.commit"]}, idempotency_key="rb3",
        tool="rollback.swap_restore")) == "PERMISSION_DENIED"
    world.broker.handle_outbox(payload)
    assert world.side_effects[-1][0] == "rollback.swap_restore"


def test_evidence_ledger_stays_verifiable_after_flow(world):
    for i in range(3):
        p = ready(world)
        world.commit(p, key=f"k{i}")
    assert world.ledger.verify(ORG, PROJ, world.ring) == 3

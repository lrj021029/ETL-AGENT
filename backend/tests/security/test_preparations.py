import copy

import pytest

from app.preparations import classify_change, fingerprint
from app.security.errors import HarnessError
from conftest import H, ORG, PROJ


def code(fn):
    with pytest.raises(HarnessError) as e:
        fn()
    return e.value.code


DRIFTS = {
    "pipeline_version_hash": lambda w: w.live.update(pipeline_version_hash=H("9")),
    "metadata_snapshot_fingerprint": lambda w: w.live.update(metadata_snapshot_fingerprint=H("8")),
    "secret_version": lambda w: w.live.update(secret_versions={"SRC_PWD": 3}),
    "policy_version": lambda w: w.live.update(policy_version="policy-999"),
}


@pytest.mark.parametrize("name", list(DRIFTS))
def test_fingerprint_drift_invalidates_and_blocks_commit(world, name):
    p = world.prepare_p1()
    world.approve_p1(p)
    DRIFTS[name](world)
    assert code(lambda: world.commit(p)) == "PREPARATION_INVALIDATED"
    assert world.store.preps.get(p.preparation_id).status == "INVALIDATED"
    assert world.store.runs == {} and world.store.issuances == {} and world.store.consumptions.rows == {}
    world.set_live()  # 即使回退漂移输入，已失效的 Preparation 也不可复活
    assert code(lambda: world.commit(p, key="again")) == "PREPARATION_INVALIDATED"


def test_active_policy_change_invalidates(world):
    from app.policy import DEFAULT_RULES

    p = world.prepare_p1()
    world.approve_p1(p)
    world.policy.store.publish(dict(DEFAULT_RULES))
    assert code(lambda: world.commit(p)) == "PREPARATION_INVALIDATED"


def test_change_classifier_deterministic_and_ignores_cosmetics(world):
    a = copy.deepcopy(world.live)
    b = {**a, "display_name": "x", "description": "y", "created_at": "t"}
    assert classify_change(a, b) == {"invalidates": False, "changed": [], "ignored": ["created_at", "description", "display_name"]}
    assert fingerprint(a) == fingerprint(b) == fingerprint(dict(reversed(list(a.items()))))
    c = {**a, "pipeline_version_hash": H("1"), "secret_versions": {"SRC_PWD": 9}}
    assert classify_change(a, c) == {"invalidates": True, "changed": ["pipeline_version_hash", "secret_versions"], "ignored": []}


def test_preparation_expiry(world):
    p = world.prepare_p1()
    world.approve_p1(p)
    world.clock.t += 25 * 3600
    assert code(lambda: world.commit(p)) == "PREPARATION_EXPIRED"


def test_prepare_has_no_side_effects(world):
    world.prepare_p1()
    s = world.store
    assert not (s.runs or s.commands or s.issuances or s.outbox_events or s.consumptions.rows or s.ledger.chains)


def test_missing_fingerprint_inputs_rejected(world):
    bad = copy.deepcopy(world.live)
    del bad["secret_versions"]
    assert code(lambda: world.preps.prepare(maker_id="m", inputs=bad, risk_level="P1", required_slots=[])) == "PREPARATION_INVALIDATED"


# -------- 受管定时调度（StandingSchedulePreparation）
def _schedule(world, **kw):
    world.set_live("dev", "INTERNAL")
    world.live["risk_level"] = "P3"
    sp = world.preps.prepare_schedule(
        maker_id="maker", inputs=copy.deepcopy(world.live), risk_level="P3", required_slots=["AUTO_P3"],
        pipeline_version_id="pv-1", cron="0 * * * *", max_budget_per_run=1, total_budget=kw.pop("total", 2), **kw)
    world.live["budget"] = {"per_run": 1, "total": sp.total_budget}
    return sp


def test_schedule_fire_uses_full_broker_path(world):
    sp = _schedule(world)
    r = world.broker.commit_scheduled(sp, world.clock.t)
    assert r.capability_jti in world.store.consumptions.rows and len(world.store.outbox_events) == 1
    assert world.store.issuances[r.capability_jti]["claims"]["sub"] == "system:scheduler"
    again = world.broker.commit_scheduled(sp, world.clock.t)  # 同一触发时间点重入幂等
    assert again.idempotent_replay and len(world.store.outbox_events) == 1


def test_schedule_concurrency_forbid_misfire_skip_budget(world):
    sp = _schedule(world, total=1)
    assert world.broker.commit_scheduled(sp, world.clock.t) is not None
    assert world.broker.commit_scheduled(sp, world.clock.t + 60) is None  # 前序运行未结束 -> CONCURRENCY_FORBID 跳过
    world.clock.t += 10_000
    for r in world.store.runs.values():
        r["status"] = "SUCCEEDED"
    assert world.broker.commit_scheduled(sp, world.clock.t - 5000) is None  # 超过容忍窗口 -> MISFIRE_SKIP 跳过
    assert code(lambda: world.broker.commit_scheduled(sp, world.clock.t)) == "PREPARATION_INVALIDATED"  # 预算耗尽阻断


def test_schedule_fingerprint_drift_blocks_fire(world):
    sp = _schedule(world)
    world.live["pipeline_version_hash"] = H("7")
    assert code(lambda: world.broker.commit_scheduled(sp, world.clock.t)) == "PREPARATION_INVALIDATED"
    assert world.store.runs == {}


def test_schedule_requires_pinned_version(world):
    world.set_live("dev", "INTERNAL")
    assert code(lambda: world.preps.prepare_schedule(
        maker_id="m", inputs=copy.deepcopy(world.live), risk_level="P3", required_slots=["AUTO_P3"], pipeline_version_id="x",
        cron="* * * * *", max_budget_per_run=1, total_budget=1, pipeline_version_binding="LATEST")) == "PREPARATION_INVALIDATED"

import copy

import pytest

from app.capabilities.signing import KeyRing, LocalEd25519Signer, b64u_encode
from app.evidence import EvidenceLedger, InMemoryLedgerStore, verify_chain
from app.security.canonical import GENESIS_PREVIOUS_HASH, canonical_bytes, canonical_digest, event_hash
from app.security.errors import HarnessError

TAMPER = "EVIDENCE_CHAIN_TAMPER_DETECTED"


def test_canonical_vectors_byte_for_byte(vectors):
    for v in vectors["canonical_json_vectors"]:
        got = canonical_bytes(v["input"])
        assert got == v["expected_canonical"].encode("utf-8"), v["name"]
        assert canonical_digest(v["input"]) == v["expected_sha256"], v["name"]


def test_canonical_edge_rules():
    assert canonical_bytes({"a": 1.0, "b": -0.0, "c": 1.5}) == b'{"a":1,"b":0,"c":1.5}'
    with pytest.raises(ValueError):
        canonical_bytes({"x": float("nan")})
    assert canonical_bytes({"k": None}) != canonical_bytes({})


def test_evidence_vectors_hash_chain(vectors):
    ev = vectors["evidence_chain_vectors"]
    prev = ev["genesis_previous_hash"]
    assert prev == GENESIS_PREVIOUS_HASH
    for item in ev["events"]:
        assert item["previous_hash"] == prev
        h = event_hash(item["event"], prev)
        assert h == item["expected_event_hash"]
        prev = h


def _ledger_from_vectors(vectors, every=2):
    signer = LocalEd25519Signer("etl-agent-evidence:v1")
    ring = KeyRing()
    signer.register(ring)
    store = InMemoryLedgerStore()
    led = EvidenceLedger(store, signer, checkpoint_every=every, clock=lambda: "2026-11-22T00:02:00Z")
    for item in vectors["evidence_chain_vectors"]["events"]:
        e = item["event"]
        rec = led.append(organization_id=e["organization_id"], project_id=e["project_id"], actor=e["actor"],
                         event_type=e["event_type"], resource_ref=e["resource_ref"], artifact_hash=e["artifact_hash"],
                         payload_digest=e["payload_digest"], event_id=e["event_id"], timestamp=e["timestamp"])
        assert rec["event"] == e
        assert rec["event_hash"] == item["expected_event_hash"]
    e0 = vectors["evidence_chain_vectors"]["events"][0]["event"]
    return led, store, ring, (e0["organization_id"], e0["project_id"])


def test_ledger_reproduces_vectors_and_verifies(vectors):
    led, store, ring, chain = _ledger_from_vectors(vectors)
    assert led.verify(*chain, ring) == 2
    assert len(store.checkpoints(chain)) == 1  # checkpoint at seq 2


def _big(n=7, every=3):
    signer = LocalEd25519Signer("etl-agent-evidence:v1")
    ring = KeyRing()
    signer.register(ring)
    store = InMemoryLedgerStore()
    led = EvidenceLedger(store, signer, checkpoint_every=every)
    for i in range(n):
        led.append(organization_id="o", project_id="p", actor="a", event_type="T", resource_ref=f"r:{i}",
                   artifact_hash=None, payload={"i": i})
    return led, store, ring, ("o", "p")


def _verify(store, ring, chain, recs=None, cps=None):
    return verify_chain(recs if recs is not None else store.events(chain),
                        cps if cps is not None else store.checkpoints(chain), ring)


def test_ledger_clean_chain_ok():
    led, store, ring, chain = _big()
    assert _verify(store, ring, chain) == 7


def test_tamper_modify_event_field():
    led, store, ring, chain = _big()
    recs = copy.deepcopy(store.events(chain))
    recs[3]["event"]["actor"] = "mallory"
    with pytest.raises(HarnessError) as e:
        _verify(store, ring, chain, recs)
    assert e.value.code == TAMPER


def test_tamper_delete_middle_event():
    led, store, ring, chain = _big()
    recs = store.events(chain)
    del recs[2]
    with pytest.raises(HarnessError) as e:
        _verify(store, ring, chain, recs)
    assert e.value.code == TAMPER


def test_tamper_reorder_events():
    led, store, ring, chain = _big()
    recs = store.events(chain)
    recs[1], recs[2] = recs[2], recs[1]
    with pytest.raises(HarnessError) as e:
        _verify(store, ring, chain, recs)
    assert e.value.code == TAMPER


def test_tamper_forged_checkpoint_signature():
    led, store, ring, chain = _big()
    cps = copy.deepcopy(store.checkpoints(chain))
    attacker = LocalEd25519Signer("etl-agent-evidence:v1")  # same kid, different key
    cps[0]["signature_b64url"] = b64u_encode(attacker.sign(canonical_bytes(cps[0]["payload"])))
    with pytest.raises(HarnessError) as e:
        _verify(store, ring, chain, cps=cps)
    assert e.value.code == TAMPER
    cps = copy.deepcopy(store.checkpoints(chain))
    cps[0]["payload"]["last_event_hash"] = "sha256:" + "f" * 64  # payload edited, signature stale
    with pytest.raises(HarnessError):
        _verify(store, ring, chain, cps=cps)


def test_tamper_full_rewrite_with_recomputed_hashes_caught_by_checkpoint():
    led, store, ring, chain = _big()
    recs = copy.deepcopy(store.events(chain))
    prev = GENESIS_PREVIOUS_HASH
    recs[1]["event"]["actor"] = "mallory"
    for r in recs:  # attacker recomputes the whole chain consistently
        r["previous_hash"] = prev
        r["event_hash"] = event_hash(r["event"], prev)
        prev = r["event_hash"]
    with pytest.raises(HarnessError) as e:
        _verify(store, ring, chain, recs)
    assert e.value.code == TAMPER and "checkpoint" in e.value.message


def test_tamper_truncation_below_checkpoint():
    led, store, ring, chain = _big()
    with pytest.raises(HarnessError):
        _verify(store, ring, chain, store.events(chain)[:2])


def test_append_only_sequence_conflict():
    led, store, ring, chain = _big(3)
    with pytest.raises(HarnessError):
        store.append(chain, {"event": {"sequence_no": 2}})


def test_payload_is_redacted_before_digest():
    led, store, ring, chain = _big(1, 50)
    a = led.append(organization_id="o", project_id="p", actor="a", event_type="T", resource_ref="r",
                   artifact_hash=None, payload={"password": "hunter2"})
    b = led.append(organization_id="o", project_id="p", actor="a", event_type="T", resource_ref="r",
                   artifact_hash=None, payload={"password": "other"})
    assert a["event"]["payload_digest"] == b["event"]["payload_digest"]

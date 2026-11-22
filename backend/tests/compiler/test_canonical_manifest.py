"""Canonical JSON / Manifest SHA256 与冻结测试向量逐字节比对；不可变性检查。"""
import json
import pathlib

import pytest

from app.artifacts import ArtifactManifest, build_pipeline_version, canonical_json, evidence_event_hash, sha256_digest

VECTORS = json.loads(
    (pathlib.Path(__file__).resolve().parents[3] / "contracts" / "crypto" / "test_vectors.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("v", VECTORS["canonical_json_vectors"], ids=lambda v: v["name"])
def test_canonical_vectors_exact(v):
    assert canonical_json(v["input"]) == v["expected_canonical"]
    assert canonical_json(v["input"]).encode("utf-8") == v["expected_canonical"].encode("utf-8")
    assert sha256_digest(v["input"]) == v["expected_sha256"]


def test_evidence_chain_vectors():
    chain = VECTORS["evidence_chain_vectors"]
    prev = chain["genesis_previous_hash"]
    for e in chain["events"]:
        assert e["previous_hash"] == prev
        prev = evidence_event_hash(e["event"], e["previous_hash"])
        assert prev == e["expected_event_hash"]


def test_manifest_hash_matches_vector_and_ignores_timestamps():
    sample = VECTORS["canonical_json_vectors"][1]
    m1 = ArtifactManifest.build(sample["input"], created_at="2026-01-01T00:00:00Z")
    m2 = ArtifactManifest.build(sample["input"], created_at="2030-12-31T23:59:59Z")
    assert m1.artifact_hash == m2.artifact_hash == sample["expected_sha256"]
    # 内容里的动态字段同样不参与哈希
    with_ts = {**sample["input"], "created_at": "x", "updated_at": "y"}
    assert ArtifactManifest.build(with_ts).artifact_hash == sample["expected_sha256"]
    assert m1.verify()


def test_manifest_is_immutable():
    m = ArtifactManifest.build({"a": 1})
    with pytest.raises(Exception):
        m.artifact_hash = "sha256:x"  # type: ignore[misc]


def test_float_normalisation_and_nan_rejected():
    assert canonical_json({"a": 1.0, "b": -0.0, "c": 1.5}) == '{"a":1,"b":0,"c":1.5}'
    with pytest.raises(ValueError):
        canonical_json({"a": float("nan")})


def test_pipeline_version_hash_stable_and_content_sensitive():
    kw = dict(
        pipeline_id="p1", revision=1, plan={"a": 1}, quality_contract={"q": 1}, supervision_contract={"s": 1},
        hocon_text="x", sql_hashes={"s1": "sha256:aa"},
        secret_refs=[{"placeholder": "${SECRET:A}", "secret_version": 1, "secret_ref_id": "u"}],
    )
    a = build_pipeline_version(**kw, created_at="t1")
    b = build_pipeline_version(**kw, created_at="t2")
    assert a.artifact_hash == b.artifact_hash
    c = build_pipeline_version(**{**kw, "hocon_text": "y"})
    assert c.artifact_hash != a.artifact_hash

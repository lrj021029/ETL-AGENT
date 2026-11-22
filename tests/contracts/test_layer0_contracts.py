"""M0 Layer 0 Contract Validation Suite.

Validates JSON schemas, YAML contracts, cryptographic canonicalization vectors,
and immutability of docs/PRD.md and docs/TECHNICAL_SPEC.md baseline presence.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"


def _canonicalize(obj):
    if isinstance(obj, float) and obj == 0.0:
        return 0
    if isinstance(obj, dict):
        return {k: _canonicalize(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [_canonicalize(v) for v in obj]
    return obj


def _jcs(obj) -> str:
    return json.dumps(
        _canonicalize(obj),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def test_required_docs_and_adrs_exist():
    assert (ROOT / "docs" / "PRD.md").is_file()
    assert (ROOT / "docs" / "TECHNICAL_SPEC.md").is_file()
    assert (ROOT / "docs" / "implementation" / "ENVIRONMENT_OVERRIDE.md").is_file()
    assert (ROOT / "docs" / "implementation" / "DECISION_REGISTER.md").is_file()
    for adr in [
        "ADR-001-environment-and-compose.md",
        "ADR-002-rls-and-background-context.md",
        "ADR-003-harness-capability-and-schedule.md",
        "ADR-004-quality-two-stage-and-supervision.md",
        "ADR-005-canonicalization-and-evidence-ledger.md",
    ]:
        assert (ROOT / "docs" / "adr" / adr).is_file(), f"Missing {adr}"


def test_json_schemas_are_valid_json():
    schema_files = [
        CONTRACTS / "schemas" / "etl_plan.schema.json",
        CONTRACTS / "schemas" / "quality_contract.schema.json",
        CONTRACTS / "schemas" / "runtime_supervision.schema.json",
        CONTRACTS / "schemas" / "connector_registry.schema.json",
        CONTRACTS / "harness" / "pdp_contract.json",
        CONTRACTS / "harness" / "capability_jws.schema.json",
        CONTRACTS / "harness" / "outbox_events.schema.json",
    ]
    for path in schema_files:
        assert path.is_file(), f"Missing schema {path}"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "$schema" in data
        assert "title" in data


def test_canonical_json_and_evidence_chain_vectors():
    vectors_path = CONTRACTS / "crypto" / "test_vectors.json"
    vectors = json.loads(vectors_path.read_text(encoding="utf-8"))

    for vec in vectors["canonical_json_vectors"]:
        canonical = _jcs(vec["input"])
        assert canonical == vec["expected_canonical"], vec["name"]
        digest = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        assert digest == vec["expected_sha256"], vec["name"]

    chain = vectors["evidence_chain_vectors"]
    prev = chain["genesis_previous_hash"]
    for item in chain["events"]:
        assert item["previous_hash"] == prev
        payload = _jcs(item["event"]).encode("utf-8") + b"\n" + prev.encode("utf-8")
        computed = "sha256:" + hashlib.sha256(payload).hexdigest()
        assert computed == item["expected_event_hash"]
        prev = computed


def test_yaml_contracts_present_and_nonempty():
    for rel in [
        "versions.lock.yaml",
        "harness/execution_state_machine.yaml",
        "openapi/openapi.yaml",
        "openapi/error_codes.yaml",
        "openapi/realtime_events.yaml",
    ]:
        p = CONTRACTS / rel
        assert p.is_file(), f"Missing {p}"
        assert len(p.read_text(encoding="utf-8").strip()) > 100

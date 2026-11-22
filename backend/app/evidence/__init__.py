"""Append-only, hash-chained Evidence Ledger with Ed25519 signed checkpoints (ADR-005)."""
from __future__ import annotations

import base64
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from app.capabilities.signing import KeyRing, Signer, b64u_decode, b64u_encode
from app.security.canonical import GENESIS_PREVIOUS_HASH, canonical_bytes, canonical_digest, event_hash
from app.security.errors import HarnessError
from app.security.redaction import redact

EVENT_FIELDS = (
    "event_id", "sequence_no", "timestamp", "actor", "organization_id", "project_id",
    "event_type", "resource_ref", "artifact_hash", "payload_digest",
)
TAMPER = "EVIDENCE_CHAIN_TAMPER_DETECTED"


class LedgerStore(Protocol):
    """Bind to `evidence_events` / `evidence_checkpoints` in integration. Appends must be
    atomic and unique on (organization_id, project_id, sequence_no)."""

    def tail(self, chain: tuple[str, str]) -> dict | None: ...
    def append(self, chain: tuple[str, str], record: dict) -> None: ...
    def events(self, chain: tuple[str, str]) -> list[dict]: ...
    def add_checkpoint(self, chain: tuple[str, str], cp: dict) -> None: ...
    def checkpoints(self, chain: tuple[str, str]) -> list[dict]: ...


class InMemoryLedgerStore:
    def __init__(self) -> None:
        self.chains: dict[tuple[str, str], list[dict]] = {}
        self.cps: dict[tuple[str, str], list[dict]] = {}

    def tail(self, chain):
        c = self.chains.get(chain)
        return c[-1] if c else None

    def append(self, chain, record):
        c = self.chains.setdefault(chain, [])
        if record["event"]["sequence_no"] != len(c) + 1:
            raise HarnessError(TAMPER, "sequence_no conflict (unique violation)")
        c.append(record)

    def events(self, chain):
        return [dict(r) for r in self.chains.get(chain, [])]

    def add_checkpoint(self, chain, cp):
        self.cps.setdefault(chain, []).append(cp)

    def checkpoints(self, chain):
        return list(self.cps.get(chain, []))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EvidenceLedger:
    def __init__(self, store: LedgerStore, signer: Signer, checkpoint_every: int = 50,
                 clock: Callable[[], str] = _now, id_factory: Callable[[], str] | None = None):
        import uuid

        self.store, self.signer, self.n = store, signer, checkpoint_every
        self._clock = clock
        self._id = id_factory or (lambda: str(uuid.uuid4()))
        self._lock = threading.RLock()

    def append(self, *, organization_id: str, project_id: str, actor: str, event_type: str,
               resource_ref: str, artifact_hash: str | None, payload: Any = None,
               payload_digest: str | None = None, event_id: str | None = None,
               timestamp: str | None = None) -> dict:
        chain = (organization_id, project_id)
        if payload_digest is None:
            payload_digest = canonical_digest(redact(payload if payload is not None else {}))
        with self._lock:
            tail = self.store.tail(chain)
            seq = 1 if tail is None else tail["event"]["sequence_no"] + 1
            prev = GENESIS_PREVIOUS_HASH if tail is None else tail["event_hash"]
            event = {
                "event_id": event_id or self._id(), "sequence_no": seq,
                "timestamp": timestamp or self._clock(), "actor": actor,
                "organization_id": organization_id, "project_id": project_id,
                "event_type": event_type, "resource_ref": resource_ref,
                "artifact_hash": artifact_hash, "payload_digest": payload_digest,
            }
            rec = {"event": event, "previous_hash": prev, "event_hash": event_hash(event, prev)}
            self.store.append(chain, rec)
            if self.n and seq % self.n == 0:
                self.checkpoint(organization_id, project_id)
            return rec

    def checkpoint(self, organization_id: str, project_id: str) -> dict:
        chain = (organization_id, project_id)
        with self._lock:
            tail = self.store.tail(chain)
            if tail is None:
                raise HarnessError("EVIDENCE_EMPTY_CHAIN", "no events to checkpoint")
            payload = {
                "organization_id": organization_id, "project_id": project_id,
                "last_sequence_no": tail["event"]["sequence_no"],
                "last_event_hash": tail["event_hash"], "event_count": tail["event"]["sequence_no"],
                "created_at": self._clock(),
            }
            sig = self.signer.sign(canonical_bytes(payload))
            cp = {"payload": payload, "signing_key_id": self.signer.kid, "signature_b64url": b64u_encode(sig)}
            self.store.add_checkpoint(chain, cp)
            return cp

    def verify(self, organization_id: str, project_id: str, keyring: KeyRing) -> int:
        chain = (organization_id, project_id)
        return verify_chain(self.store.events(chain), self.store.checkpoints(chain), keyring,
                            organization_id=organization_id, project_id=project_id)


def verify_chain(records: list[dict], checkpoints: list[dict], keyring: KeyRing, *,
                 organization_id: str | None = None, project_id: str | None = None) -> int:
    """Full verification. Raises HarnessError(EVIDENCE_CHAIN_TAMPER_DETECTED). Returns event count."""
    prev = GENESIS_PREVIOUS_HASH
    by_seq: dict[int, str] = {}
    for i, rec in enumerate(records, start=1):
        ev = rec.get("event", {})
        if set(ev) != set(EVENT_FIELDS):
            raise HarnessError(TAMPER, f"event field set altered at position {i}")
        if ev["sequence_no"] != i:
            raise HarnessError(TAMPER, f"sequence gap/reorder at position {i}")
        if organization_id and (ev["organization_id"], ev["project_id"]) != (organization_id, project_id):
            raise HarnessError(TAMPER, f"foreign event in chain at {i}")
        if rec["previous_hash"] != prev:
            raise HarnessError(TAMPER, f"previous_hash mismatch at {i}")
        if event_hash(ev, prev) != rec["event_hash"]:
            raise HarnessError(TAMPER, f"event_hash mismatch at {i}")
        prev = rec["event_hash"]
        by_seq[i] = prev
    for cp in checkpoints:
        p = cp["payload"]
        try:
            pub = keyring.get(cp["signing_key_id"])
            pub.verify(b64u_decode(cp["signature_b64url"]), canonical_bytes(p))
        except Exception:  # noqa: BLE001
            raise HarnessError(TAMPER, "checkpoint signature invalid") from None
        if by_seq.get(p["last_sequence_no"]) != p["last_event_hash"]:
            raise HarnessError(TAMPER, "checkpoint does not match chain (truncation or rewrite)")
    if checkpoints:
        last = max(c["payload"]["last_sequence_no"] for c in checkpoints)
        if len(records) < last:
            raise HarnessError(TAMPER, "chain truncated below checkpoint")
    return len(records)

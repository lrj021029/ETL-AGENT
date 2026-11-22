"""Preparation (zero side effects) + deterministic fingerprint & change classification."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Protocol

from app.security.canonical import canonical_digest
from app.security.errors import HarnessError

# Fields that, when changed, MUST invalidate an existing Preparation (frozen inputs).
BLOCKING_FIELDS = (
    "pipeline_version_hash", "metadata_snapshot_fingerprint", "secret_versions", "policy_version",
    "artifact_hash", "data_classification", "environment", "project_id", "organization_id", "risk_level", "budget", "tool",
)
# Cosmetic fields that never invalidate (and are excluded from the fingerprint).
IGNORED_FIELDS = ("display_name", "description", "labels", "created_at", "updated_at")
PREP_TTL_SECONDS = 24 * 3600


def freeze_inputs(inputs: dict) -> dict:
    missing = [f for f in ("pipeline_version_hash", "metadata_snapshot_fingerprint", "secret_versions", "policy_version") if f not in inputs]
    if missing:
        raise HarnessError("PREPARATION_INVALIDATED", f"missing fingerprint inputs: {missing}")
    return {k: inputs[k] for k in BLOCKING_FIELDS if k in inputs}


def fingerprint(inputs: dict) -> str:
    return canonical_digest(freeze_inputs(inputs))


def classify_change(old: dict, new: dict) -> dict:
    """Deterministic change classifier: returns {'invalidates': bool, 'changed': [...], 'ignored': [...]}."""
    old_f, new_f = freeze_inputs(old), freeze_inputs(new)
    changed = sorted(k for k in set(old_f) | set(new_f) if old_f.get(k) != new_f.get(k))
    ignored = sorted(k for k in IGNORED_FIELDS if old.get(k) != new.get(k))
    return {"invalidates": bool(changed), "changed": changed, "ignored": ignored}


@dataclass
class Preparation:
    preparation_id: str
    organization_id: str
    project_id: str
    maker_id: str
    inputs: dict
    fingerprint: str
    risk_level: str
    required_slots: list[str]
    policy_version: str
    created_at: float
    expires_at: float
    status: str = "PREPARED"  # PREPARED | COMMITTED | INVALIDATED
    kind: str = "ONE_SHOT"


class PreparationStore(Protocol):
    def save(self, p: Preparation) -> None: ...
    def get(self, preparation_id: str) -> Preparation | None: ...
    def compare_and_set_status(self, preparation_id: str, expected: str, new: str) -> bool: ...


class InMemoryPreparationStore:
    def __init__(self) -> None:
        import threading

        self._d: dict[str, Preparation] = {}
        self._lock = threading.Lock()

    def save(self, p: Preparation) -> None:
        with self._lock:
            self._d[p.preparation_id] = p

    def get(self, preparation_id: str) -> Preparation | None:
        return self._d.get(preparation_id)

    def compare_and_set_status(self, preparation_id: str, expected: str, new: str) -> bool:
        with self._lock:
            p = self._d[preparation_id]
            if p.status != expected:
                return False
            p.status = new
            return True


class PreparationService:
    """`prepare` only freezes inputs and persists one Preparation record: no external side
    effects, no capability, no execution."""

    def __init__(self, store: PreparationStore, clock: Callable[[], float] = time.time, ttl: int = PREP_TTL_SECONDS):
        self.store, self._clock, self.ttl = store, clock, ttl

    def prepare(self, *, maker_id: str, inputs: dict, risk_level: str, required_slots: list[str]) -> Preparation:
        frozen = freeze_inputs(inputs)
        now = self._clock()
        p = Preparation(
            preparation_id=str(uuid.uuid4()), organization_id=frozen["organization_id"], project_id=frozen["project_id"],
            maker_id=maker_id, inputs=frozen, fingerprint=fingerprint(frozen), risk_level=risk_level,
            required_slots=list(required_slots), policy_version=frozen["policy_version"],
            created_at=now, expires_at=now + self.ttl,
        )
        self.store.save(p)
        return p

    def assert_valid(self, p: Preparation, current_inputs: dict, now: float | None = None) -> None:
        """Re-verify against live inputs. Raises PREPARATION_INVALIDATED / PREPARATION_EXPIRED."""
        now = self._clock() if now is None else now
        if p.status == "INVALIDATED":
            raise HarnessError("PREPARATION_INVALIDATED", "preparation already invalidated")
        if now >= p.expires_at:
            raise HarnessError("PREPARATION_EXPIRED", "preparation expired")
        res = classify_change(p.inputs, current_inputs)
        if res["invalidates"] or fingerprint(current_inputs) != p.fingerprint:
            p.status = "INVALIDATED"
            self.store.save(p)
            raise HarnessError("PREPARATION_INVALIDATED", "fingerprint drift", {"changed": res["changed"]})


@dataclass
class StandingSchedulePreparation(Preparation):
    pipeline_version_id: str = ""
    cron: str = ""
    concurrency_policy: str = "FORBID"
    misfire_policy: str = "SKIP"
    pipeline_version_binding: str = "PINNED"
    misfire_grace_seconds: int = 300
    max_budget_per_run: float = 0.0
    total_budget: float = 0.0
    spent_budget: float = 0.0
    kind: str = "STANDING_SCHEDULE"
    approvals_ref: str | None = None


class StandingScheduleService(PreparationService):
    def prepare_schedule(self, *, maker_id: str, inputs: dict, risk_level: str, required_slots: list[str],
                         pipeline_version_id: str, cron: str, max_budget_per_run: float, total_budget: float,
                         concurrency_policy: str = "FORBID", misfire_policy: str = "SKIP",
                         pipeline_version_binding: str = "PINNED") -> StandingSchedulePreparation:
        if pipeline_version_binding != "PINNED":
            raise HarnessError("PREPARATION_INVALIDATED", "schedules must pin a pipeline_version")
        base = self.prepare(maker_id=maker_id, inputs={**inputs, "budget": {"per_run": max_budget_per_run, "total": total_budget}},
                            risk_level=risk_level, required_slots=required_slots)
        sp = StandingSchedulePreparation(
            **{k: v for k, v in base.__dict__.items() if k != "kind"}, pipeline_version_id=pipeline_version_id, cron=cron,
            concurrency_policy=concurrency_policy, misfire_policy=misfire_policy,
            pipeline_version_binding=pipeline_version_binding, max_budget_per_run=max_budget_per_run,
            total_budget=total_budget,
        )
        self.store.save(sp)
        return sp

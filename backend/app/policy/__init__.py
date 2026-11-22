"""Versioned RBAC + ABAC Policy Decision Point (P0/P1/P2/P3 + SoD)."""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from typing import Any

from app.security.canonical import canonical_digest
from app.security.errors import HarnessError

RISK_ORDER = {"P3": 0, "P2": 1, "P1": 2, "P0": 3}
SLOT_PERMISSION = {
    "DATA_CHECKER": "approval.data_checker", "SECURITY_CHECKER": "approval.security_checker",
    "SINGLE_CHECKER": "approval.single_checker",
}
# Permissions that may never be granted by any (custom) role.
SOD_BYPASS_PERMISSIONS = {"sod.bypass", "approval.self_approve", "approval.bypass", "policy.override_p0", "approval.any_slot"}

DEFAULT_RULES: dict[str, Any] = {
    "forbidden_actions": ["pipeline.drop_target_table", "secret.export_plaintext", "policy.disable_sod"],
    "p0_if": {"data_classification_prod_unmasked_pii_export": True},
    "p3_auto_approve": True,
    "p3_max_data_classification": "INTERNAL",
    "risk_by_environment": {"dev": "P3", "staging": "P2", "prod": "P1"},
    "risk_by_classification": {"PUBLIC": "P3", "INTERNAL": "P3", "CONFIDENTIAL": "P2", "RESTRICTED_PII": "P1"},
    "sod_enforced": True,
    "action_permissions": {
        "execution.commit": "execution.commit", "execution.rollback": "execution.rollback",
        "approval.approve": "approval.approve", "schedule.commit": "schedule.manage",
    },
}


@dataclass(frozen=True)
class PolicyVersion:
    version: str
    rules: dict
    digest: str
    parent: str | None = None


def _validate_rules(rules: dict) -> None:
    if rules.get("sod_enforced") is not True:
        raise HarnessError("SOD_VIOLATION", "policy may not disable SoD enforcement")
    if not rules.get("forbidden_actions"):
        raise HarnessError("PDP_DENIED_P0", "policy must retain P0 forbidden actions")
    for role, perms in (rules.get("roles") or {}).items():
        validate_role(role, perms)


def validate_role(name: str, permissions: list[str] | set[str]) -> None:
    """Custom roles can never carry SoD bypass, nor both checker slots at once."""
    perms = set(permissions)
    bad = perms & SOD_BYPASS_PERMISSIONS
    if bad:
        raise HarnessError("SOD_VIOLATION", f"role '{name}' grants SoD bypass: {sorted(bad)}")
    if {"approval.data_checker", "approval.security_checker"} <= perms:
        raise HarnessError("SOD_VIOLATION", f"role '{name}' combines both checker slots")
    if "approval.*" in perms or "*" in perms:
        raise HarnessError("SOD_VIOLATION", f"role '{name}' uses wildcard approval grant")


class PolicyStore:
    """Versioned policy history with diff and rollback. Bind persistence to a `policy_versions` table."""

    def __init__(self, initial_rules: dict | None = None, initial_version: str = "policy-1"):
        self._lock = threading.RLock()
        self._versions: dict[str, PolicyVersion] = {}
        self._order: list[str] = []
        self._active: str | None = None
        self.publish(initial_rules or DEFAULT_RULES, version=initial_version)

    @property
    def active(self) -> PolicyVersion:
        return self._versions[self._active]  # type: ignore[index]

    def get(self, version: str) -> PolicyVersion:
        return self._versions[version]

    def history(self) -> list[str]:
        return list(self._order)

    def publish(self, rules: dict, version: str | None = None) -> PolicyVersion:
        _validate_rules(rules)
        with self._lock:
            version = version or f"policy-{len(self._order) + 1}"
            if version in self._versions:
                raise HarnessError("POLICY_VERSION_EXISTS", version)
            pv = PolicyVersion(version, copy.deepcopy(rules), canonical_digest(rules), self._active)
            self._versions[version] = pv
            self._order.append(version)
            self._active = version
            return pv

    def diff(self, a: str, b: str) -> dict:
        ra, rb = self._versions[a].rules, self._versions[b].rules
        keys = set(ra) | set(rb)
        return {k: {"from": ra.get(k), "to": rb.get(k)} for k in sorted(keys) if ra.get(k) != rb.get(k)}

    def rollback(self, to_version: str) -> PolicyVersion:
        """Rollback = publish a NEW version with the old rules (history is append-only). The
        policy_version changes, so every in-flight Preparation is invalidated."""
        with self._lock:
            old = self._versions[to_version]
            return self.publish(old.rules, version=f"policy-{len(self._order) + 1}")


@dataclass
class PdpResponse:
    decision: str
    risk_level: str
    required_approval_slots: list[str]
    constraints: dict = field(default_factory=dict)
    reason_codes: list[str] = field(default_factory=list)
    policy_version: str = ""

    def to_dict(self) -> dict:
        return {
            "decision": self.decision, "risk_level": self.risk_level,
            "required_approval_slots": self.required_approval_slots, "constraints": self.constraints,
            "reason_codes": self.reason_codes, "policy_version": self.policy_version,
        }


class PolicyEngine:
    def __init__(self, store: PolicyStore | None = None):
        self.store = store or PolicyStore()

    def classify_risk(self, req: dict, rules: dict) -> str:
        if req["action"] in rules["forbidden_actions"]:
            return "P0"
        r = max(
            rules["risk_by_environment"].get(req["environment"], "P1"),
            rules["risk_by_classification"].get(req["data_classification"], "P1"),
            key=lambda x: RISK_ORDER[x],
        )
        if req.get("risk_level"):  # caller may only escalate, never lower
            r = max(r, req["risk_level"], key=lambda x: RISK_ORDER[x])
        return r

    def evaluate(self, req: dict, policy_version: str | None = None) -> PdpResponse:
        pv = self.store.get(policy_version) if policy_version else self.store.active
        rules = pv.rules
        risk = self.classify_risk(req, rules)
        sub = req["subject"]

        def resp(decision, slots, reasons, constraints=None):
            return PdpResponse(decision, risk, slots, constraints or {}, reasons, pv.version)

        if risk == "P0":
            return resp("DENY", [], ["PDP_DENIED_P0"])
        action = req["action"]
        needed = rules["action_permissions"].get(action, action)
        if needed not in sub.get("permissions", []):
            return resp("DENY", [], ["PERMISSION_DENIED"])
        if sub.get("is_system_principal") and action.startswith("approval."):
            return resp("DENY", [], ["SOD_VIOLATION"])
        ctx = req.get("approval_context") or {}
        if action == "approval.approve":
            maker = ctx.get("maker_id")
            if maker and maker == sub["user_id"]:
                return resp("DENY", [], ["APPROVAL_SELF_APPROVE_FORBIDDEN"])
            for slot, uid in (ctx.get("existing_approvers") or {}).items():
                if uid == sub["user_id"]:
                    return resp("DENY", [], ["APPROVAL_SLOT_CONFLICT"])
        constraints = {"requires_prepare": True, "requires_broker_capability": True, "requires_outbox": True}
        if risk == "P1":
            return resp("REQUIRE_APPROVAL", ["DATA_CHECKER", "SECURITY_CHECKER"], ["P1_DUAL_APPROVAL"], constraints)
        if risk == "P2":
            return resp("REQUIRE_APPROVAL", ["SINGLE_CHECKER"], ["P2_SINGLE_APPROVAL"], constraints)
        if rules.get("p3_auto_approve"):
            return resp("ALLOW", ["AUTO_P3"], ["P3_AUTO_APPROVE"], constraints)
        return resp("REQUIRE_APPROVAL", ["SINGLE_CHECKER"], ["P3_MANUAL"], constraints)

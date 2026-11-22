"""Maker-Checker 审批槽位引擎（支持 DATA_CHECKER / SECURITY_CHECKER / SINGLE_CHECKER / AUTO_P3）。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from app.policy import PdpResponse, PolicyEngine, SLOT_PERMISSION
from app.preparations import Preparation, PreparationService
from app.security.canonical import canonical_digest
from app.security.errors import HarnessError

AUTO_APPROVER = "system:auto_p3"
_DENY_REASON_TO_CODE = {
    "PDP_DENIED_P0": "PDP_DENIED_P0",
    "APPROVAL_SELF_APPROVE_FORBIDDEN": "APPROVAL_SELF_APPROVE_FORBIDDEN",
    "APPROVAL_SLOT_CONFLICT": "APPROVAL_SLOT_CONFLICT",
    "SOD_VIOLATION": "SOD_VIOLATION",
    "PERMISSION_DENIED": "PERMISSION_DENIED",
}


@dataclass(frozen=True)
class Approval:
    """单槽位审批记录：绑定 `preparation_id`、槽位名称、审批人 ID 与时间戳。"""

    preparation_id: str
    slot: str
    approver_id: str
    approved_at: float


class ApprovalStore(Protocol):
    """审批记录持久化协议（集成阶段绑定至 PostgreSQL `approvals` 表）。"""

    def add(self, a: Approval) -> None: ...
    def for_preparation(self, preparation_id: str) -> list[Approval]: ...


class InMemoryApprovalStore:
    """线程安全的内存审批存储。"""

    def __init__(self) -> None:
        self._d: dict[str, list[Approval]] = {}
        self._lock = threading.Lock()

    def add(self, a: Approval) -> None:
        with self._lock:
            self._d.setdefault(a.preparation_id, []).append(a)

    def for_preparation(self, preparation_id: str) -> list[Approval]:
        return list(self._d.get(preparation_id, []))


def approval_digest(prep: Preparation, approvals: list[Approval]) -> str:
    """计算审批摘要（`approval_digest`）：覆盖 Preparation 指纹、流水线版本哈希、策略版本及排序后的槽位审批人列表。"""
    return canonical_digest({
        "preparation_id": prep.preparation_id, "preparation_fingerprint": prep.fingerprint,
        "pipeline_version_hash": prep.inputs["pipeline_version_hash"], "policy_version": prep.policy_version,
        "risk_level": prep.risk_level,
        "approvals": sorted(({"slot": a.slot, "approver_id": a.approver_id} for a in approvals),
                            key=lambda x: (x["slot"], x["approver_id"])),
    })


class ApprovalEngine:
    """审批引擎：强制阻断 Maker 自批（`APPROVAL_SELF_APPROVE_FORBIDDEN`）、同一用户占双槽（`APPROVAL_SLOT_CONFLICT`）与 P0 审批。"""

    def __init__(self, store: ApprovalStore, policy: PolicyEngine, preparations: PreparationService,
                 clock: Callable[[], float] = time.time):
        self.store, self.policy, self.preps, self._clock = store, policy, preparations, clock
        self._lock = threading.RLock()

    def approve(self, prep: Preparation, *, approver: dict, slot: str, current_inputs: dict) -> Approval:
        """执行人工槽位审批；`approver` 为符合 PDP 契约的 `subject` 字典。"""
        with self._lock:
            self.preps.assert_valid(prep, current_inputs)
            if prep.policy_version != self.policy.store.active.version:
                raise HarnessError("PREPARATION_INVALIDATED", "policy version changed")
            existing = self.store.for_preparation(prep.preparation_id)
            req = {
                "subject": approver, "organization_id": prep.organization_id, "project_id": prep.project_id,
                "action": "approval.approve", "resource": {"resource_type": "preparation", "resource_id": prep.preparation_id},
                "environment": prep.inputs.get("environment", "prod"),
                "data_classification": prep.inputs.get("data_classification", "RESTRICTED_PII"),
                "risk_level": prep.risk_level, "artifact_hash": prep.inputs.get("artifact_hash"),
                "approval_context": {"maker_id": prep.maker_id, "preparation_id": prep.preparation_id,
                                     "existing_approvers": {a.slot: a.approver_id for a in existing}},
            }
            res = self.policy.evaluate(req, policy_version=prep.policy_version)
            if res.decision == "DENY":
                code = next((_DENY_REASON_TO_CODE[r] for r in res.reason_codes if r in _DENY_REASON_TO_CODE), "PERMISSION_DENIED")
                raise HarnessError(code, "approval denied by policy", {"reasons": res.reason_codes})
            if slot == "AUTO_P3" or approver["user_id"] == AUTO_APPROVER:
                raise HarnessError("SOD_VIOLATION", "AUTO_P3 slot cannot be approved by a user")
            if slot not in prep.required_slots:
                raise HarnessError("APPROVAL_SLOT_CONFLICT", f"slot {slot} not required for this preparation")
            if SLOT_PERMISSION.get(slot) not in approver.get("permissions", []):
                raise HarnessError("PERMISSION_DENIED", f"missing permission for slot {slot}")
            if any(a.slot == slot for a in existing):
                raise HarnessError("APPROVAL_SLOT_CONFLICT", f"slot {slot} already approved")
            a = Approval(prep.preparation_id, slot, approver["user_id"], self._clock())
            self.store.add(a)
            return a

    def auto_approve_p3(self, prep: Preparation, pdp: PdpResponse, current_inputs: dict) -> Approval:
        """P3 自动审批：仅当 PDP 明确返回 `ALLOW` 与 `['AUTO_P3']` 时由 Tool Broker 内部调用，仍必须走完整 Capability + Outbox 路径。"""
        with self._lock:
            self.preps.assert_valid(prep, current_inputs)
            if not (pdp.decision == "ALLOW" and pdp.required_approval_slots == ["AUTO_P3"] and pdp.risk_level == "P3"
                    and prep.risk_level == "P3" and pdp.policy_version == prep.policy_version):
                raise HarnessError("PDP_DENIED_P0" if prep.risk_level == "P0" else "APPROVAL_INCOMPLETE",
                                   "preparation is not eligible for P3 auto-approval")
            existing = self.store.for_preparation(prep.preparation_id)
            if existing:
                return existing[0]
            a = Approval(prep.preparation_id, "AUTO_P3", AUTO_APPROVER, self._clock())
            self.store.add(a)
            return a

    def missing_slots(self, prep: Preparation) -> list[str]:
        """返回当前 Preparation 尚未集齐的必选审批槽位列表。"""
        have = {a.slot for a in self.store.for_preparation(prep.preparation_id)}
        return [s for s in prep.required_slots if s not in have]

    def digest(self, prep: Preparation) -> str:
        """校验所有必选槽位已集齐且满足 SoD 后，生成确定性 `approval_digest`。"""
        if prep.risk_level == "P0":
            raise HarnessError("PDP_DENIED_P0", "P0 can never be approved")
        if self.missing_slots(prep):
            raise HarnessError("APPROVAL_INCOMPLETE", "missing slots", {"missing": self.missing_slots(prep)})
        approvals = self.store.for_preparation(prep.preparation_id)
        users = [a.approver_id for a in approvals if a.approver_id != AUTO_APPROVER]
        if len(users) != len(set(users)):
            raise HarnessError("APPROVAL_SLOT_CONFLICT", "same user holds multiple slots")
        if prep.maker_id in users:
            raise HarnessError("APPROVAL_SELF_APPROVE_FORBIDDEN", "maker appears among approvers")
        return approval_digest(prep, approvals)


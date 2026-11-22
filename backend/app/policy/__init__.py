"""版本化 RBAC + ABAC 策略决策点（PDP 引擎：强制执行 P0/P1/P2/P3 风险分级与职责分离 SoD）。"""
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
# 严禁任何自定义角色授予的职责分离（SoD）旁路或越权权限集合
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
    """不可变策略版本快照：记录版本号、规则字典、Canonical 摘要与父版本。"""

    version: str
    rules: dict
    digest: str
    parent: str | None = None


def _validate_rules(rules: dict) -> None:
    """校验策略规则不违反系统级硬性安全不变量（SoD 不可关闭、P0 禁止列表不可清空、角色定义合法）。"""
    if rules.get("sod_enforced") is not True:
        raise HarnessError("SOD_VIOLATION", "policy may not disable SoD enforcement")
    if not rules.get("forbidden_actions"):
        raise HarnessError("PDP_DENIED_P0", "policy must retain P0 forbidden actions")
    for role, perms in (rules.get("roles") or {}).items():
        validate_role(role, perms)


def validate_role(name: str, permissions: list[str] | set[str]) -> None:
    """校验自定义角色：严禁携带 SoD 旁路权限、严禁同时拥有双审批槽位、严禁通配符审批授权。"""
    perms = set(permissions)
    bad = perms & SOD_BYPASS_PERMISSIONS
    if bad:
        raise HarnessError("SOD_VIOLATION", f"role '{name}' grants SoD bypass: {sorted(bad)}")
    if {"approval.data_checker", "approval.security_checker"} <= perms:
        raise HarnessError("SOD_VIOLATION", f"role '{name}' combines both checker slots")
    if "approval.*" in perms or "*" in perms:
        raise HarnessError("SOD_VIOLATION", f"role '{name}' uses wildcard approval grant")


class PolicyStore:
    """版本化策略存储：支持发布新版本、版本间差异比对（diff）与只追加回滚（rollback）。"""

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
        """校验并发布新策略版本，将其切换为当前活跃版本。"""
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
        """计算两个策略版本之间的规则差异。"""
        ra, rb = self._versions[a].rules, self._versions[b].rules
        keys = set(ra) | set(rb)
        return {k: {"from": ra.get(k), "to": rb.get(k)} for k in sorted(keys) if ra.get(k) != rb.get(k)}

    def rollback(self, to_version: str) -> PolicyVersion:
        """回滚至指定历史版本的规则：以仅追加方式生成新的 `policy_version`，使所有绑定旧版本号的在途 Preparation 自动失效。"""
        with self._lock:
            old = self._versions[to_version]
            return self.publish(old.rules, version=f"policy-{len(self._order) + 1}")


@dataclass
class PdpResponse:
    """PDP 评估结果对象（严格符合 `contracts/harness/pdp_contract.json` 的 `PdpResponse` Schema）。"""

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
    """RBAC + ABAC 策略决策引擎：计算风险等级（P0-P3）、所需审批槽位并强制执行 Maker-Checker SoD。"""

    def __init__(self, store: PolicyStore | None = None):
        self.store = store or PolicyStore()

    def classify_risk(self, req: dict, rules: dict) -> str:
        """根据动作黑名单、部署环境与数据分级推导风险等级；调用方传入的 `risk_level` 仅允许上调风险，绝不允许降级。"""
        if req["action"] in rules["forbidden_actions"]:
            return "P0"
        r = max(
            rules["risk_by_environment"].get(req["environment"], "P1"),
            rules["risk_by_classification"].get(req["data_classification"], "P1"),
            key=lambda x: RISK_ORDER[x],
        )
        if req.get("risk_level"):  # 调用方只能调高风险等级，不能降低
            r = max(r, req["risk_level"], key=lambda x: RISK_ORDER[x])
        return r

    def evaluate(self, req: dict, policy_version: str | None = None) -> PdpResponse:
        """对 `PdpRequest` 执行确定性策略评估，返回 `ALLOW` / `DENY` / `REQUIRE_APPROVAL`。"""
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


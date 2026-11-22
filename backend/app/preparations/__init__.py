"""Preparation 冻结准备（零外部副作用）、确定性指纹（PreparationFingerprint）、变更分级失效器与受管定时调度（StandingSchedulePreparation）。"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Protocol

from app.security.canonical import canonical_digest
from app.security.errors import HarnessError

# 一旦发生变更就必须使既有 Preparation 立即失效的冻结语义字段
BLOCKING_FIELDS = (
    "pipeline_version_hash", "metadata_snapshot_fingerprint", "secret_versions", "policy_version",
    "artifact_hash", "data_classification", "environment", "project_id", "organization_id", "risk_level", "budget", "tool",
)
# 纯展示或审计时间戳字段：变更不会触发 Preparation 失效，也不纳入指纹计算
IGNORED_FIELDS = ("display_name", "description", "labels", "created_at", "updated_at")
PREP_TTL_SECONDS = 24 * 3600


def freeze_inputs(inputs: dict) -> dict:
    """提取并校验参与指纹计算的冻结输入子集。"""
    missing = [f for f in ("pipeline_version_hash", "metadata_snapshot_fingerprint", "secret_versions", "policy_version") if f not in inputs]
    if missing:
        raise HarnessError("PREPARATION_INVALIDATED", f"missing fingerprint inputs: {missing}")
    return {k: inputs[k] for k in BLOCKING_FIELDS if k in inputs}


def fingerprint(inputs: dict) -> str:
    """计算冻结准备输入的 Canonical JSON SHA256 指纹。"""
    return canonical_digest(freeze_inputs(inputs))


def classify_change(old: dict, new: dict) -> dict:
    """确定性变更分类器：返回 `{'invalidates': bool, 'changed': [...], 'ignored': [...]}`。"""
    old_f, new_f = freeze_inputs(old), freeze_inputs(new)
    changed = sorted(k for k in set(old_f) | set(new_f) if old_f.get(k) != new_f.get(k))
    ignored = sorted(k for k in IGNORED_FIELDS if old.get(k) != new.get(k))
    return {"invalidates": bool(changed), "changed": changed, "ignored": ignored}


@dataclass
class Preparation:
    """冻结准备实体：绑定流水线版本哈希、元数据快照指纹、密钥版本与策略版本。"""

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
    status: str = "PREPARED"  # 状态枚举：PREPARED | COMMITTED | INVALIDATED
    kind: str = "ONE_SHOT"


class PreparationStore(Protocol):
    """Preparation 持久化协议（集成阶段绑定至 PostgreSQL `preparations` 表）。"""

    def save(self, p: Preparation) -> None: ...
    def get(self, preparation_id: str) -> Preparation | None: ...
    def compare_and_set_status(self, preparation_id: str, expected: str, new: str) -> bool: ...


class InMemoryPreparationStore:
    """线程安全的内存 Preparation 存储，支持原子 CAS 状态流转。"""

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
    """`prepare` 仅冻结输入并写入一条 Preparation 记录：零外部副作用、不签发 Capability、不触发执行。"""

    def __init__(self, store: PreparationStore, clock: Callable[[], float] = time.time, ttl: int = PREP_TTL_SECONDS):
        self.store, self._clock, self.ttl = store, clock, ttl

    def prepare(self, *, maker_id: str, inputs: dict, risk_level: str, required_slots: list[str]) -> Preparation:
        """创建单次冻结准备记录并计算确定性指纹。"""
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
        """针对当前实时上下文重验 Preparation：若已过期或发生阻塞级指纹漂移，将其标记为 `INVALIDATED` 并抛出异常。"""
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
    """受管 Cron 定时调度的常驻冻结准备（ADR-003）：强制固定 `pipeline_version_binding=PINNED` 并受预算上限约束。"""

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
    """受管 Cron 调度准备服务：创建绑定固定流水线版本、并发/错过策略与预算上限的 `StandingSchedulePreparation`。"""

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


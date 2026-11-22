"""Connector Capability Registry 的只读视图与能力校验（消费 connector_registry.schema.json 条目）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

CAPABILITY_UNSUPPORTED = "CONNECTOR_CAPABILITY_UNSUPPORTED"

_BLOCKED_STATUS = {"PLANNED", "UNSUPPORTED"}
_SYNC_CAP = {"FULL_LOAD": "full_load", "INCREMENTAL": "incremental", "CDC": "cdc"}


@dataclass(frozen=True)
class CapabilityIssue:
    code: str
    detail: str


class ConnectorRegistry:
    def __init__(self, entries: Iterable[dict[str, Any]]):
        self._entries = {e["connector_id"]: e for e in entries}

    def get(self, connector_id: str) -> dict[str, Any] | None:
        return self._entries.get(connector_id)

    def __contains__(self, connector_id: str) -> bool:
        return connector_id in self._entries


def check_plan_capabilities(
    registry: ConnectorRegistry,
    *,
    source_connector: str,
    sink_connector: str,
    sync_mode: str,
    write_mode: str,
    needs_quality_split: bool,
) -> list[CapabilityIssue]:
    """确定性能力校验；注册表中缺失 / PLANNED / UNSUPPORTED / 缺能力均拒绝。"""
    issues: list[CapabilityIssue] = []
    for cid, role in ((source_connector, "SOURCE"), (sink_connector, "SINK")):
        e = registry.get(cid)
        if e is None:
            issues.append(CapabilityIssue(CAPABILITY_UNSUPPORTED, f"{cid}: not in registry"))
            continue
        if e["support_status"] in _BLOCKED_STATUS:
            issues.append(CapabilityIssue(CAPABILITY_UNSUPPORTED, f"{cid}: status {e['support_status']}"))
        if role not in e["roles"]:
            issues.append(CapabilityIssue(CAPABILITY_UNSUPPORTED, f"{cid}: role {role} unsupported"))
        caps = e["capabilities"]
        if role == "SOURCE" and not caps.get(_SYNC_CAP[sync_mode], False):
            issues.append(CapabilityIssue(CAPABILITY_UNSUPPORTED, f"{cid}: {sync_mode} unsupported"))
        if role == "SINK" and write_mode == "UPSERT" and not caps.get("upsert", False):
            issues.append(CapabilityIssue(CAPABILITY_UNSUPPORTED, f"{cid}: upsert unsupported"))
        if role == "SINK" and write_mode == "SHADOW_SWAP":
            if not e["publish_rollback_limits"].get("shadow_table_supported", False):
                issues.append(CapabilityIssue(CAPABILITY_UNSUPPORTED, f"{cid}: shadow table unsupported"))
        if needs_quality_split and not caps.get("quality_split_compatible", False):
            issues.append(CapabilityIssue(CAPABILITY_UNSUPPORTED, f"{cid}: quality split unsupported"))
    return issues

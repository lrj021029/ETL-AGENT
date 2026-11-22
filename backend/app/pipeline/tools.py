"""元数据快照、Schema Diff、敏感字段脱敏预览，以及 Vue Flow DAG 构建器。"""
from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .models import EtlPlan


class TableMeta(BaseModel):
    """一张表的元数据：列名 -> 类型，主键，敏感列分类。"""

    model_config = ConfigDict(extra="forbid")

    columns: dict[str, str]
    primary_keys: list[str] = Field(default_factory=list)
    sensitive_columns: list[str] = Field(default_factory=list)


class MetadataSnapshot(BaseModel):
    """不可变元数据快照；tables 的 key 形如 'schema.table'。"""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    tables: dict[str, TableMeta]

    def column_allowlist(self) -> dict[str, set[str]]:
        return {k: set(v.columns) for k, v in self.tables.items()}


def schema_diff(old: dict[str, str], new: dict[str, str]) -> dict[str, Any]:
    """列级 Schema Diff：新增 / 删除 / 类型变更。"""
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = [
        {"column": c, "from": old[c], "to": new[c]} for c in sorted(set(old) & set(new)) if old[c] != new[c]
    ]
    return {"added": added, "removed": removed, "type_changed": changed, "compatible": not (removed or changed)}


def _mask(policy: str | None, value: Any) -> Any:
    if value is None:
        return None
    s = str(value)
    if policy == "HASH_SHA256":
        return "sha256:" + hashlib.sha256(s.encode()).hexdigest()[:16]
    if policy == "MASK_PARTIAL":
        return s[:1] + "*" * max(len(s) - 2, 1) + s[-1:] if len(s) > 2 else "*" * len(s)
    if policy == "REDACT":
        return "[REDACTED]"
    return value


def masking_preview(
    plan: EtlPlan, meta: MetadataSnapshot, sample_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """脱敏预览：敏感列没有显式脱敏策略时默认 REDACT（fail-closed）。"""
    sensitive: set[str] = set()
    for t in meta.tables.values():
        sensitive.update(t.sensitive_columns)
    policies = {m.source_field: m.masking_policy for m in plan.field_mappings}
    target = {m.source_field: m.target_field for m in plan.field_mappings}
    out = []
    for row in sample_rows:
        masked = {}
        for col, val in row.items():
            if col not in target:
                continue
            pol = policies.get(col)
            if col in sensitive and pol in (None, "NONE"):
                pol = "REDACT"
            masked[target[col]] = _mask(pol, val)
        out.append(masked)
    return out


def schema_alignment_diff(plan: EtlPlan, meta: MetadataSnapshot) -> list[dict[str, Any]]:
    """字段映射与元数据快照的对齐检查，返回问题列表。"""
    issues: list[dict[str, Any]] = []
    cols: dict[str, str] = {}
    for obj in plan.source.objects:
        t = meta.tables.get(obj.qualified)
        if t is None:
            issues.append({"code": "SOURCE_OBJECT_NOT_IN_SNAPSHOT", "object": obj.qualified})
            continue
        cols.update(t.columns)
    for m in plan.field_mappings:
        if m.source_object is None and m.transform_expr is not None:
            continue  # 派生字段
        if m.source_field not in cols and not m.transform_expr:
            issues.append({"code": "MAPPING_SOURCE_FIELD_UNKNOWN", "field": m.source_field})
    targets = [m.target_field for m in plan.field_mappings]
    for dup in sorted({t for t in targets if targets.count(t) > 1}):
        issues.append({"code": "MAPPING_TARGET_DUPLICATE", "field": dup})
    return issues


def build_dag(plan: EtlPlan) -> dict[str, list[dict[str, Any]]]:
    """生成 Vue Flow 的 nodes/edges（确定性 id 与布局）。"""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    x = 0

    def add(nid: str, kind: str, label: str, data: dict[str, Any]) -> str:
        nonlocal x
        nodes.append({"id": nid, "type": kind, "position": {"x": x, "y": 0}, "data": {"label": label, **data}})
        x += 260
        return nid

    prev = add("source", "source", plan.source.connector_id, {"objects": [o.qualified for o in plan.source.objects]})
    for i, t in enumerate(plan.transformations):
        nid = add(
            f"transform_{i}",
            "transform",
            t.step_id,
            {"kind": t.kind, "strategy": t.resolved_execution_strategy, "reasons": t.strategy_reason_codes},
        )
        edges.append({"id": f"e_{prev}_{nid}", "source": prev, "target": nid})
        prev = nid
    sink = add(
        "sink",
        "sink",
        f"{plan.sink.target_schema}.{plan.sink.target_table}",
        {"write_mode": plan.sink.write_mode, "connector": plan.sink.connector_id},
    )
    edges.append({"id": f"e_{prev}_{sink}", "source": prev, "target": sink})
    return {"nodes": nodes, "edges": edges}

"""受管只读元数据探查（Read-only Metadata Probe）、Schema 指纹计算与受控本地文件边界校验。

严格遵循 PRD §4.2、§7.1 与 TECHNICAL_SPEC §5.2、§5.5：
1. 本地文件路径必须位于预配置的受控挂载根目录下，严禁 `..` 路径穿越、符号链接逃逸和越界绝对路径（触发 `FILE_PATH_TRAVERSAL_BLOCKED`）。
2. 元数据探查仅执行只读查询并生成不可变快照摘要与确定性 SHA256 Schema Fingerprint。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.app.connectors.models import (
    ConnectorCapabilityUnsupportedError,
    FilePathTraversalBlockedError,
)
from backend.app.connectors.registry import ConnectorRegistry, get_default_connector_registry


@dataclass(frozen=True)
class ProbedColumn:
    """只读探查采集到的单列元数据。"""

    column_name: str
    data_type: str
    nullable: bool
    ordinal_position: int
    is_primary_key: bool = False
    default_expr: str | None = None
    comment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为标准化字典用于计算 Schema Fingerprint。"""
        return {
            "column_name": self.column_name,
            "data_type": self.data_type.upper().strip(),
            "nullable": self.nullable,
            "ordinal_position": self.ordinal_position,
            "is_primary_key": self.is_primary_key,
            "default_expr": self.default_expr,
            "comment": self.comment,
        }


@dataclass(frozen=True)
class ProbedObject:
    """只读探查采集到的表或视图对象元数据。"""

    schema_name: str
    object_name: str
    object_type: str
    columns: list[ProbedColumn]
    primary_keys: list[str] = field(default_factory=list)
    row_count_approx: int = 0
    bytes_approx: int = 0

    def to_dict(self) -> dict[str, Any]:
        """序列化对象结构。"""
        return {
            "schema_name": self.schema_name,
            "object_name": self.object_name,
            "object_type": self.object_type,
            "primary_keys": sorted(self.primary_keys),
            "row_count_approx": self.row_count_approx,
            "bytes_approx": self.bytes_approx,
            "columns": [c.to_dict() for c in sorted(self.columns, key=lambda x: x.ordinal_position)],
        }


def compute_schema_fingerprint(objects: list[ProbedObject]) -> str:
    """基于规范化 JSON（RFC 8785 键排序）计算表/列结构的确定性 SHA256 指纹。

    注意：近似行数与近似字节数等动态统计值不纳入结构指纹计算，以防数据写入被误判为 Schema Drift。
    """
    structural_payload = []
    for obj in sorted(objects, key=lambda o: (o.schema_name, o.object_name)):
        structural_payload.append(
            {
                "schema_name": obj.schema_name,
                "object_name": obj.object_name,
                "object_type": obj.object_type,
                "primary_keys": sorted(obj.primary_keys),
                "columns": [
                    {
                        "column_name": col.column_name,
                        "data_type": col.data_type.upper().strip(),
                        "nullable": col.nullable,
                        "ordinal_position": col.ordinal_position,
                        "is_primary_key": col.is_primary_key,
                    }
                    for col in sorted(obj.columns, key=lambda c: c.ordinal_position)
                ],
            }
        )
    canonical = json.dumps(
        structural_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ControlledFileBoundary:
    """受控本地文件挂载目录边界校验器。

    阻止任何包含 `..`、符号链接逃逸、盘符/根目录越界或敏感系统目录的文件访问请求。
    """

    def __init__(self, allowed_roots: list[Path | str]) -> None:
        if not allowed_roots:
            raise ValueError("allowed_roots 不能为空")
        self._allowed_roots = [Path(r).resolve() for r in allowed_roots]

    def validate_path(self, requested_path: str | Path) -> Path:
        """校验并返回受控根目录内的规范化安全路径；违规时抛出 `FilePathTraversalBlockedError`。"""
        raw_str = str(requested_path).strip()
        if not raw_str:
            raise FilePathTraversalBlockedError(
                "文件路径不能为空",
                detail={"requested_path": raw_str, "reason": "EMPTY_PATH"},
            )

        # 显式拦截包含 '..' 的原始路径片段或空字节注入
        normalized_slashes = raw_str.replace("\\", "/")
        parts = [p for p in normalized_slashes.split("/") if p]
        if ".." in parts or "\x00" in raw_str:
            raise FilePathTraversalBlockedError(
                f"检测到非法路径穿越片段: {raw_str}",
                detail={"requested_path": raw_str, "reason": "DOT_DOT_TRAVERSAL"},
            )

        candidate = Path(raw_str)
        # 若为相对路径，默认拼接至第一个受控挂载根目录
        if not candidate.is_absolute():
            candidate = self._allowed_roots[0] / candidate

        # 检查路径链上是否存在符号链接逃逸
        try:
            if candidate.is_symlink():
                raise FilePathTraversalBlockedError(
                    f"禁止通过符号链接访问本地文件: {raw_str}",
                    detail={"requested_path": raw_str, "reason": "SYMLINK_BLOCKED"},
                )
            resolved = candidate.resolve()
        except FilePathTraversalBlockedError:
            raise
        except Exception as exc:
            raise FilePathTraversalBlockedError(
                f"路径解析失败: {raw_str}",
                detail={"requested_path": raw_str, "error": str(exc)},
            ) from exc

        # 验证解析后的真实路径必须严格位于至少一个受控根目录之下
        is_inside = False
        for root in self._allowed_roots:
            try:
                resolved.relative_to(root)
                is_inside = True
                break
            except ValueError:
                continue

        if not is_inside:
            raise FilePathTraversalBlockedError(
                f"路径越出受控挂载根目录范围: {raw_str}",
                detail={
                    "requested_path": raw_str,
                    "resolved_path": str(resolved),
                    "reason": "OUTSIDE_CONTROLLED_ROOT",
                },
            )

        return resolved


class ReadOnlyMetadataProbeService:
    """数据源只读元数据探查与 Schema Drift 检测服务。"""

    def __init__(self, registry: ConnectorRegistry | None = None) -> None:
        self._registry = registry or get_default_connector_registry()

    def verify_probe_supported(self, connector_id: str) -> None:
        """检查连接器是否具备只读元数据探查能力。"""
        entry = self._registry.get_entry(connector_id)
        if not entry.capabilities.metadata_probe:
            raise ConnectorCapabilityUnsupportedError(
                f"连接器 {connector_id} 不支持只读元数据探查 (metadata_probe=false)",
                detail={"connector_id": connector_id, "capability": "metadata_probe"},
            )

    def detect_schema_drift(
        self,
        previous_objects: list[ProbedObject],
        current_objects: list[ProbedObject],
    ) -> dict[str, Any]:
        """对比两个不可变 MetadataSnapshot 的表/列结构，生成确定性 Drift 报告。"""
        prev_fp = compute_schema_fingerprint(previous_objects)
        curr_fp = compute_schema_fingerprint(current_objects)
        if prev_fp == curr_fp:
            return {
                "has_drift": False,
                "severity": "NONE",
                "from_fingerprint": prev_fp,
                "to_fingerprint": curr_fp,
                "added_columns": [],
                "removed_columns": [],
                "type_changed_columns": [],
            }

        prev_map = {
            (o.schema_name, o.object_name, c.column_name): c
            for o in previous_objects
            for c in o.columns
        }
        curr_map = {
            (o.schema_name, o.object_name, c.column_name): c
            for o in current_objects
            for c in o.columns
        }

        added = sorted(
            f"{s}.{t}.{c}" for (s, t, c) in (set(curr_map.keys()) - set(prev_map.keys()))
        )
        removed = sorted(
            f"{s}.{t}.{c}" for (s, t, c) in (set(prev_map.keys()) - set(curr_map.keys()))
        )
        type_changed = []
        for key in sorted(set(prev_map.keys()) & set(curr_map.keys())):
            p_col = prev_map[key]
            c_col = curr_map[key]
            if (
                p_col.data_type.upper().strip() != c_col.data_type.upper().strip()
                or p_col.nullable != c_col.nullable
            ):
                s, t, col_name = key
                type_changed.append(
                    {
                        "column": f"{s}.{t}.{col_name}",
                        "from_type": p_col.data_type,
                        "to_type": c_col.data_type,
                        "from_nullable": p_col.nullable,
                        "to_nullable": c_col.nullable,
                    }
                )

        # 删除列或类型/非空变更属于 BREAKING 漂移，仅新增可空列属于 COMPATIBLE
        severity = "BREAKING" if (removed or type_changed) else "COMPATIBLE"
        return {
            "has_drift": True,
            "severity": severity,
            "from_fingerprint": prev_fp,
            "to_fingerprint": curr_fp,
            "added_columns": added,
            "removed_columns": removed,
            "type_changed_columns": type_changed,
        }

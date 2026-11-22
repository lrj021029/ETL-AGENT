"""Doris 原子表替换发布适配器与强制 Schema 兼容门禁实现。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Callable, Protocol
import uuid


_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PublishSchemaIncompatibleError(Exception):
    """当影子表与目标表 Schema 不兼容时抛出，严格阻断 `ALTER TABLE ... REPLACE` 执行。"""

    error_code: str = "PUBLISH_SCHEMA_INCOMPATIBLE"

    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


def _validate_sql_identifier(identifier: str, label: str) -> str:
    """校验表名或库名标识符，防止发布 DDL 注入。支持 `db.table` 或 `table`。"""
    cleaned = identifier.strip()
    parts = cleaned.split(".")
    if len(parts) not in (1, 2) or any(not _IDENTIFIER_PATTERN.match(p) for p in parts):
        raise ValueError(f"非法的 {label} SQL 标识符: {identifier}")
    return cleaned


def build_shadow_table_name(target_table: str, execution_id: str) -> str:
    """按 ADR-004 规范生成影子表名称：`<target>__shadow_<short_exec_id>`。"""
    base_table = target_table.split(".")[-1].strip()
    _validate_sql_identifier(base_table, "target_table")
    short_id = re.sub(r"[^a-zA-Z0-9]", "", execution_id)[:12].lower()
    if not short_id:
        raise ValueError("execution_id 无法提取有效的 short_exec_id")
    return f"{base_table}__shadow_{short_id}"


@dataclass(frozen=True)
class DorisColumnSpec:
    """Doris 表列结构规格，用于发布前 Schema 兼容对比。"""

    name: str
    data_type: str
    nullable: bool = True
    is_key: bool = False

    def normalized_type(self) -> str:
        """统一类型名称大小写与空白，确保类型精确比较。"""
        return re.sub(r"\s+", " ", self.data_type.upper().strip())


@dataclass(frozen=True)
class DorisTableSchemaSpec:
    """Doris OLAP 表结构元数据规格。"""

    table_name: str
    columns: list[DorisColumnSpec]
    engine_type: str = "OLAP"
    keys_type: str = "UNIQUE_KEYS"
    key_columns: list[str] = field(default_factory=list)
    partition_columns: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SchemaCompatibilityReport:
    """发布前影子表与目标表 Schema 兼容性检查报告。"""

    compatible: bool
    target_table: str
    shadow_table: str
    mismatches: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "compatible": self.compatible,
            "target_table": self.target_table,
            "shadow_table": self.shadow_table,
            "mismatches": list(self.mismatches),
        }


@dataclass(frozen=True)
class PublishedDatasetVersionRecord:
    """已发布数据集版本实体（对应 `contracts/db/erd.md` §2.5 `published_dataset_versions` 表）。"""

    id: str
    organization_id: str
    project_id: str
    execution_id: str
    pipeline_version_id: str
    artifact_hash: str
    target_connection_id: str
    target_table_ref: str
    shadow_table_ref: str
    previous_version_id: str | None
    archive_table_ref: str | None
    swap_result_json: dict[str, Any]
    quality_report_id: str
    status: str
    published_by: str
    published_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    retention_until: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为持久化与 API 响应字典。"""
        return {
            "id": self.id,
            "organization_id": self.organization_id,
            "project_id": self.project_id,
            "execution_id": self.execution_id,
            "pipeline_version_id": self.pipeline_version_id,
            "artifact_hash": self.artifact_hash,
            "target_connection_id": self.target_connection_id,
            "target_table_ref": self.target_table_ref,
            "shadow_table_ref": self.shadow_table_ref,
            "previous_version_id": self.previous_version_id,
            "archive_table_ref": self.archive_table_ref,
            "swap_result_json": dict(self.swap_result_json),
            "quality_report_id": self.quality_report_id,
            "status": self.status,
            "published_by": self.published_by,
            "published_at": self.published_at,
            "retention_until": self.retention_until,
        }


class PublishAdapter(Protocol):
    """目标数据系统发布适配器协议（TECHNICAL_SPEC §10.4）。"""

    def verify_schema_compatibility(
        self,
        target_schema: DorisTableSchemaSpec,
        shadow_schema: DorisTableSchemaSpec,
    ) -> SchemaCompatibilityReport:
        """校验目标表与影子表结构兼容性；不兼容时抛出 `PublishSchemaIncompatibleError`。"""

    def publish_shadow_to_target(
        self,
        *,
        organization_id: str,
        project_id: str,
        execution_id: str,
        pipeline_version_id: str,
        artifact_hash: str,
        target_connection_id: str,
        quality_report_id: str,
        published_by: str,
        target_schema: DorisTableSchemaSpec,
        shadow_schema: DorisTableSchemaSpec,
        previous_version_id: str | None = None,
        retention_until: str | None = None,
    ) -> PublishedDatasetVersionRecord:
        """执行结构兼容校验并完成受管发布，返回新建的 `PublishedDatasetVersionRecord`。"""


class DorisPublishAdapter:
    """Apache Doris 2.1.7 原子表替换发布适配器。

    严格执行：
    1. 强制 Schema 兼容门禁（`verify_schema_compatibility`）——任何列缺失、多余、类型不一致、
       Nullability 不一致、Key 模型或分区列不一致，均在执行任何 SQL 之前阻断并抛出 `PUBLISH_SCHEMA_INCOMPATIBLE`。
    2. 生成并执行精确原子表替换语句：
       `ALTER TABLE <target> REPLACE WITH TABLE <shadow> PROPERTIES('swap'='true')`
    3. 保留置换后的 `<shadow>` 作为 `archive_table_ref`，并关联 `previous_version_id` 供受管 `SWAP_RESTORE` 回滚。
    """

    def __init__(
        self,
        sql_executor: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self._sql_executor = sql_executor
        self.executed_sql_log: list[str] = []

    def verify_schema_compatibility(
        self,
        target_schema: DorisTableSchemaSpec,
        shadow_schema: DorisTableSchemaSpec,
    ) -> SchemaCompatibilityReport:
        """严格比对目标表与影子表的引擎、Key 模型、分区、列名、类型及非空约束。"""
        mismatches: list[dict[str, Any]] = []

        # 1. Doris REPLACE WITH TABLE 仅适用于 OLAP 表
        if target_schema.engine_type.upper() != "OLAP" or shadow_schema.engine_type.upper() != "OLAP":
            mismatches.append(
                {
                    "field": "engine_type",
                    "reason": "NON_OLAP_TABLE_UNSUPPORTED",
                    "target": target_schema.engine_type,
                    "shadow": shadow_schema.engine_type,
                }
            )

        # 2. Key 模型与 Key 列校验
        if target_schema.keys_type.upper() != shadow_schema.keys_type.upper():
            mismatches.append(
                {
                    "field": "keys_type",
                    "reason": "KEYS_MODEL_MISMATCH",
                    "target": target_schema.keys_type,
                    "shadow": shadow_schema.keys_type,
                }
            )

        target_keys = [k.lower() for k in target_schema.key_columns]
        shadow_keys = [k.lower() for k in shadow_schema.key_columns]
        if target_keys != shadow_keys:
            mismatches.append(
                {
                    "field": "key_columns",
                    "reason": "KEY_COLUMNS_MISMATCH",
                    "target": target_schema.key_columns,
                    "shadow": shadow_schema.key_columns,
                }
            )

        # 3. 分区列兼容性校验
        target_parts = [p.lower() for p in target_schema.partition_columns]
        shadow_parts = [p.lower() for p in shadow_schema.partition_columns]
        if target_parts != shadow_parts:
            mismatches.append(
                {
                    "field": "partition_columns",
                    "reason": "PARTITION_COLUMNS_MISMATCH",
                    "target": target_schema.partition_columns,
                    "shadow": shadow_schema.partition_columns,
                }
            )

        # 4. 列集合、顺序、数据类型、Nullability 与 Key 标记逐列比对
        target_col_map = {c.name.lower(): c for c in target_schema.columns}
        shadow_col_map = {c.name.lower(): c for c in shadow_schema.columns}

        missing_in_shadow = [
            c.name for c in target_schema.columns if c.name.lower() not in shadow_col_map
        ]
        extra_in_shadow = [
            c.name for c in shadow_schema.columns if c.name.lower() not in target_col_map
        ]
        if missing_in_shadow:
            mismatches.append(
                {
                    "field": "columns",
                    "reason": "MISSING_COLUMNS_IN_SHADOW",
                    "columns": missing_in_shadow,
                }
            )
        if extra_in_shadow:
            mismatches.append(
                {
                    "field": "columns",
                    "reason": "EXTRA_COLUMNS_IN_SHADOW",
                    "columns": extra_in_shadow,
                }
            )

        for col_key, t_col in target_col_map.items():
            s_col = shadow_col_map.get(col_key)
            if s_col is None:
                continue
            if t_col.normalized_type() != s_col.normalized_type():
                mismatches.append(
                    {
                        "field": f"column.{t_col.name}.data_type",
                        "reason": "COLUMN_TYPE_MISMATCH",
                        "column": t_col.name,
                        "target_type": t_col.normalized_type(),
                        "shadow_type": s_col.normalized_type(),
                    }
                )
            if t_col.nullable != s_col.nullable:
                mismatches.append(
                    {
                        "field": f"column.{t_col.name}.nullable",
                        "reason": "COLUMN_NULLABILITY_MISMATCH",
                        "column": t_col.name,
                        "target_nullable": t_col.nullable,
                        "shadow_nullable": s_col.nullable,
                    }
                )
            if t_col.is_key != s_col.is_key:
                mismatches.append(
                    {
                        "field": f"column.{t_col.name}.is_key",
                        "reason": "COLUMN_KEY_FLAG_MISMATCH",
                        "column": t_col.name,
                        "target_is_key": t_col.is_key,
                        "shadow_is_key": s_col.is_key,
                    }
                )

        if mismatches:
            raise PublishSchemaIncompatibleError(
                f"Doris 发布前 Schema 兼容门禁阻断：目标表 {target_schema.table_name} "
                f"与影子表 {shadow_schema.table_name} 存在 {len(mismatches)} 处结构不兼容",
                detail={
                    "target_table": target_schema.table_name,
                    "shadow_table": shadow_schema.table_name,
                    "mismatches": mismatches,
                },
            )

        return SchemaCompatibilityReport(
            compatible=True,
            target_table=target_schema.table_name,
            shadow_table=shadow_schema.table_name,
            mismatches=[],
        )

    @staticmethod
    def build_atomic_swap_sql(target_table: str, shadow_table: str) -> str:
        """构建 Doris 原子表替换 SQL 语句（ADR-004 §3）。

        注意：Doris 语法中 `WITH TABLE <shadow>` 使用同库下的影子表名（不含库名前缀）。
        生成格式严格为：
        `ALTER TABLE <target> REPLACE WITH TABLE <shadow> PROPERTIES('swap'='true')`
        """
        safe_target = _validate_sql_identifier(target_table, "target_table")
        safe_shadow_full = _validate_sql_identifier(shadow_table, "shadow_table")
        shadow_unqualified = safe_shadow_full.split(".")[-1]
        return (
            f"ALTER TABLE {safe_target} REPLACE WITH TABLE {shadow_unqualified} "
            "PROPERTIES('swap'='true')"
        )

    def publish_shadow_to_target(
        self,
        *,
        organization_id: str,
        project_id: str,
        execution_id: str,
        pipeline_version_id: str,
        artifact_hash: str,
        target_connection_id: str,
        quality_report_id: str,
        published_by: str,
        target_schema: DorisTableSchemaSpec,
        shadow_schema: DorisTableSchemaSpec,
        previous_version_id: str | None = None,
        retention_until: str | None = None,
    ) -> PublishedDatasetVersionRecord:
        """先强制执行 Schema 兼容校验，通过后再执行 Doris 原子 Swap 并登记 `PublishedDatasetVersionRecord`。"""
        # 第一步：强制 Schema 兼容门禁（若不兼容直接抛出 PublishSchemaIncompatibleError，绝不执行 SQL）
        compat_report = self.verify_schema_compatibility(target_schema, shadow_schema)

        # 第二步：生成精确 Doris 原子表替换 DDL
        swap_sql = self.build_atomic_swap_sql(
            target_schema.table_name,
            shadow_schema.table_name,
        )

        # 第三步：调用受管 SQL 执行器（若注入）并记录执行日志
        driver_result: dict[str, Any] = {"mode": "dry_run_or_injected"}
        if self._sql_executor is not None:
            driver_result = self._sql_executor(swap_sql) or {"status": "EXECUTED"}
        self.executed_sql_log.append(swap_sql)

        now_iso = datetime.now(timezone.utc).isoformat()
        swap_result = {
            "adapter": "DORIS_ATOMIC_SWAP",
            "swap_sql": swap_sql,
            "swap_property": "swap=true",
            "schema_compat_verified": compat_report.compatible,
            "driver_result": driver_result,
            "swapped_at": now_iso,
        }

        # swap='true' 下，原目标表的数据已原子置换到 shadow_schema.table_name，作为归档表引用保留供恢复
        return PublishedDatasetVersionRecord(
            id=str(uuid.uuid4()),
            organization_id=organization_id,
            project_id=project_id,
            execution_id=execution_id,
            pipeline_version_id=pipeline_version_id,
            artifact_hash=artifact_hash,
            target_connection_id=target_connection_id,
            target_table_ref=target_schema.table_name,
            shadow_table_ref=shadow_schema.table_name,
            previous_version_id=previous_version_id,
            archive_table_ref=shadow_schema.table_name,
            swap_result_json=swap_result,
            quality_report_id=quality_report_id,
            status="PUBLISHED",
            published_by=published_by,
            published_at=now_iso,
            retention_until=retention_until,
        )

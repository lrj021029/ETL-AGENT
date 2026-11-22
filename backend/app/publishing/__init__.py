"""目标端数据发布与 Doris 原子表替换模块（Publishing & Doris Atomic Swap Adapter）。

严格遵循 `ADR-004` §3、TECHNICAL_SPEC §10.4 / §13.1 与 `contracts/db/erd.md` §2.5：
1. Doris 原生 `REPLACE WITH TABLE` 不会校验两张表 Schema 是否一致；因此 `DorisPublishAdapter` 在生成或执行任何
   `ALTER TABLE` 语句前，必须先通过确定性 Schema 兼容门禁（校验引擎类型 OLAP、列名、数据类型、Nullability、Key 模型与分区键）。
2. 若 Schema 不兼容，立即抛出 `PublishSchemaIncompatibleError`（`PUBLISH_SCHEMA_INCOMPATIBLE`），绝不执行 DDL。
3. 验证通过后执行 `ALTER TABLE <target> REPLACE WITH TABLE <shadow> PROPERTIES('swap'='true')`，
   将原表数据保留在 `<shadow>`（记为 `archive_table_ref`），并创建绑定 `previous_version_id` 的 `PublishedDatasetVersionRecord`。
"""

from backend.app.publishing.doris_adapter import (
    DorisColumnSpec,
    DorisPublishAdapter,
    DorisTableSchemaSpec,
    PublishAdapter,
    PublishedDatasetVersionRecord,
    PublishSchemaIncompatibleError,
    SchemaCompatibilityReport,
    build_shadow_table_name,
)

__all__ = [
    "DorisColumnSpec",
    "DorisPublishAdapter",
    "DorisTableSchemaSpec",
    "PublishAdapter",
    "PublishSchemaIncompatibleError",
    "PublishedDatasetVersionRecord",
    "SchemaCompatibilityReport",
    "build_shadow_table_name",
]

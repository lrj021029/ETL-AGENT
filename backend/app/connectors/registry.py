"""版本化连接器注册中心实现（ConnectorRegistry）。

预置连接器状态严格遵循 `DECISION_REGISTER.md`（DEC-010, PRD-TBD-01）与 `connector_registry.schema.json`：
- 已完成配置适配但尚未在合并后执行真实 L2 容器端到端测试的连接器，一律初始化为 `CONFIGURED`。
- 严禁将未执行真实 L2 验证的连接器伪造为 `END_TO_END_VERIFIED`。
- Oracle、ClickHouse、REST API 明确标记为 `PLANNED`。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.app.connectors.models import (
    ConnectorCapabilities,
    ConnectorCapabilityEntry,
    ConnectorCapabilityUnsupportedError,
    ConnectorRole,
    PublishRollbackLimits,
    SupportStatus,
)

# 定位冻结的 JSON Schema 契约文件路径
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONNECTOR_SCHEMA_PATH = _REPO_ROOT / "contracts" / "schemas" / "connector_registry.schema.json"
_ISO8601_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def validate_entry_against_contract(entry_dict: dict[str, Any]) -> None:
    """对照 `contracts/schemas/connector_registry.schema.json` 对连接器条目执行严格结构与类型校验。

    不依赖可选第三方 jsonschema 库，直接解析契约文件并强制校验 required、additionalProperties、枚举及嵌套对象。
    """
    schema = json.loads(_CONNECTOR_SCHEMA_PATH.read_text(encoding="utf-8"))
    required_top = set(schema["required"])
    allowed_top = set(schema["properties"].keys())

    missing = required_top - set(entry_dict.keys())
    if missing:
        raise ValueError(f"连接器条目缺少必填字段: {sorted(missing)}")

    extra = set(entry_dict.keys()) - allowed_top
    if extra and schema.get("additionalProperties") is False:
        raise ValueError(f"连接器条目包含契约未允许的额外字段: {sorted(extra)}")

    if entry_dict.get("schema_version") != "1.0.0":
        raise ValueError("schema_version 必须恒为 '1.0.0'")

    for str_field in ("connector_id", "connector_name", "connector_version", "seatunnel_version"):
        val = entry_dict.get(str_field)
        if not isinstance(val, str) or len(val.strip()) < 1:
            raise ValueError(f"字段 {str_field} 必须是非空字符串")

    roles = entry_dict.get("roles")
    allowed_roles = set(schema["properties"]["roles"]["items"]["enum"])
    if not isinstance(roles, list) or len(roles) < 1 or any(r not in allowed_roles for r in roles):
        raise ValueError(f"字段 roles 非法: {roles}")

    auth_modes = entry_dict.get("auth_modes")
    if not isinstance(auth_modes, list) or any(not isinstance(a, str) for a in auth_modes):
        raise ValueError("字段 auth_modes 必须是字符串数组")

    allowed_status = set(schema["properties"]["support_status"]["enum"])
    if entry_dict.get("support_status") not in allowed_status:
        raise ValueError(f"字段 support_status 非法: {entry_dict.get('support_status')}")

    # 校验 capabilities 嵌套对象
    caps = entry_dict.get("capabilities")
    caps_schema = schema["properties"]["capabilities"]
    if not isinstance(caps, dict):
        raise ValueError("capabilities 必须是对象")
    req_caps = set(caps_schema["required"])
    if set(caps.keys()) != req_caps:
        raise ValueError(f"capabilities 字段集合不匹配契约: {set(caps.keys()) ^ req_caps}")
    for k, v in caps.items():
        if not isinstance(v, bool):
            raise ValueError(f"capabilities.{k} 必须是布尔值")

    # 校验 publish_rollback_limits 嵌套对象
    limits = entry_dict.get("publish_rollback_limits")
    limits_schema = schema["properties"]["publish_rollback_limits"]
    if not isinstance(limits, dict):
        raise ValueError("publish_rollback_limits 必须是对象")
    req_limits = set(limits_schema["required"])
    if set(limits.keys()) != req_limits:
        raise ValueError(
            f"publish_rollback_limits 字段集合不匹配契约: {set(limits.keys()) ^ req_limits}"
        )
    if not isinstance(limits["atomic_table_replace"], bool) or not isinstance(
        limits["shadow_table_supported"], bool
    ):
        raise ValueError("publish_rollback_limits 布尔字段类型非法")
    if not isinstance(limits["notes"], str):
        raise ValueError("publish_rollback_limits.notes 必须是字符串")

    # 校验可选证据与时间戳
    evidence_ref = entry_dict.get("test_evidence_ref")
    if evidence_ref is not None and not isinstance(evidence_ref, str):
        raise ValueError("test_evidence_ref 必须是字符串或 null")
    verified_at = entry_dict.get("verified_at")
    if verified_at is not None:
        if not isinstance(verified_at, str) or not _ISO8601_PATTERN.match(verified_at):
            raise ValueError(f"verified_at 必须符合 ISO-8601 date-time 格式: {verified_at}")


def _build_seed_entries() -> list[ConnectorCapabilityEntry]:
    """构建平台初始种子连接器能力矩阵。

    注意：所有已配置连接器初始状态均为 `CONFIGURED`，绝不虚标 `END_TO_END_VERIFIED`。
    Oracle、ClickHouse、REST API 按 PRD-TBD-01 标记为 `PLANNED`。
    """
    st_ver = "2.3.9"
    return [
        # 1. MySQL 8.0 JDBC 批处理/增量连接器
        ConnectorCapabilityEntry(
            connector_id="mysql-jdbc",
            connector_name="MySQL JDBC Connector (Batch & Incremental)",
            connector_version="2.3.9",
            seatunnel_version=st_ver,
            roles=[ConnectorRole.SOURCE, ConnectorRole.SINK],
            auth_modes=["USERNAME_PASSWORD_SECRET_REF", "TLS_CLIENT_CERT_SECRET_REF"],
            support_status=SupportStatus.CONFIGURED,
            capabilities=ConnectorCapabilities(
                metadata_probe=True,
                full_load=True,
                incremental=True,
                cdc=False,
                delete_events=False,
                upsert=True,
                checkpoint_restore=True,
                schema_evolution=False,
                quality_split_compatible=True,
            ),
            publish_rollback_limits=PublishRollbackLimits(
                atomic_table_replace=False,
                shadow_table_supported=True,
                notes="MySQL 支持写入影子表，但不作为默认 Doris 原子 REPLACE 表交换目标端。",
            ),
        ),
        # 2. MySQL 8.0 CDC 连接器（Binlog ROW + GTID）
        ConnectorCapabilityEntry(
            connector_id="mysql-cdc",
            connector_name="MySQL CDC Connector (Binlog ROW / GTID)",
            connector_version="2.3.9",
            seatunnel_version=st_ver,
            roles=[ConnectorRole.SOURCE],
            auth_modes=["USERNAME_PASSWORD_SECRET_REF"],
            support_status=SupportStatus.CONFIGURED,
            capabilities=ConnectorCapabilities(
                metadata_probe=True,
                full_load=True,
                incremental=True,
                cdc=True,
                delete_events=True,
                upsert=False,
                checkpoint_restore=True,
                schema_evolution=True,
                quality_split_compatible=True,
            ),
            publish_rollback_limits=PublishRollbackLimits(
                atomic_table_replace=False,
                shadow_table_supported=False,
                notes="CDC 源端连接器；位点失效恢复严格遵循 C+D 策略（先恢复有效历史检查点，丢失或过期需 Operator 确认）。",
            ),
        ),
        # 3. PostgreSQL 16 JDBC 批处理/增量连接器
        ConnectorCapabilityEntry(
            connector_id="postgres-jdbc",
            connector_name="PostgreSQL JDBC Connector (Batch & Incremental)",
            connector_version="2.3.9",
            seatunnel_version=st_ver,
            roles=[ConnectorRole.SOURCE, ConnectorRole.SINK],
            auth_modes=["USERNAME_PASSWORD_SECRET_REF", "TLS_CLIENT_CERT_SECRET_REF"],
            support_status=SupportStatus.CONFIGURED,
            capabilities=ConnectorCapabilities(
                metadata_probe=True,
                full_load=True,
                incremental=True,
                cdc=False,
                delete_events=False,
                upsert=True,
                checkpoint_restore=True,
                schema_evolution=False,
                quality_split_compatible=True,
            ),
            publish_rollback_limits=PublishRollbackLimits(
                atomic_table_replace=False,
                shadow_table_supported=True,
                notes="PostgreSQL 支持暂存表与只读元数据探查。",
            ),
        ),
        # 4. PostgreSQL 16 CDC 连接器（pgoutput / Logical Replication Slot）
        ConnectorCapabilityEntry(
            connector_id="postgres-cdc",
            connector_name="PostgreSQL CDC Connector (Logical WAL / pgoutput)",
            connector_version="2.3.9",
            seatunnel_version=st_ver,
            roles=[ConnectorRole.SOURCE],
            auth_modes=["USERNAME_PASSWORD_SECRET_REF"],
            support_status=SupportStatus.CONFIGURED,
            capabilities=ConnectorCapabilities(
                metadata_probe=True,
                full_load=True,
                incremental=True,
                cdc=True,
                delete_events=True,
                upsert=False,
                checkpoint_restore=True,
                schema_evolution=True,
                quality_split_compatible=True,
            ),
            publish_rollback_limits=PublishRollbackLimits(
                atomic_table_replace=False,
                shadow_table_supported=False,
                notes="依赖 wal_level=logical 与复制槽；Replication Slot 丢失或 WAL 过期时强制阻断并要求 Operator 显式确认。",
            ),
        ),
        # 5. Apache Doris 2.1.7 连接器（支持影子表 + 错误表 + 原子表替换发布）
        ConnectorCapabilityEntry(
            connector_id="doris",
            connector_name="Apache Doris Connector (FE StreamLoad & OLAP Swap)",
            connector_version="2.3.9-doris-2.1.7",
            seatunnel_version=st_ver,
            roles=[ConnectorRole.SOURCE, ConnectorRole.SINK],
            auth_modes=["USERNAME_PASSWORD_SECRET_REF"],
            support_status=SupportStatus.CONFIGURED,
            capabilities=ConnectorCapabilities(
                metadata_probe=True,
                full_load=True,
                incremental=True,
                cdc=False,
                delete_events=True,
                upsert=True,
                checkpoint_restore=True,
                schema_evolution=True,
                quality_split_compatible=True,
            ),
            publish_rollback_limits=PublishRollbackLimits(
                atomic_table_replace=True,
                shadow_table_supported=True,
                notes="仅适用于 OLAP 表；Doris 原生 REPLACE WITH TABLE 不校验 Schema，必须由 DorisPublishAdapter 先执行 Schema 兼容门禁。",
            ),
        ),
        # 6. MinIO / S3 对象存储连接器
        ConnectorCapabilityEntry(
            connector_id="s3-minio",
            connector_name="MinIO / S3 Object Storage File Connector",
            connector_version="2.3.9",
            seatunnel_version=st_ver,
            roles=[ConnectorRole.SOURCE, ConnectorRole.SINK],
            auth_modes=["ACCESS_KEY_SECRET_REF", "IAM_ROLE"],
            support_status=SupportStatus.CONFIGURED,
            capabilities=ConnectorCapabilities(
                metadata_probe=True,
                full_load=True,
                incremental=False,
                cdc=False,
                delete_events=False,
                upsert=False,
                checkpoint_restore=True,
                schema_evolution=False,
                quality_split_compatible=True,
            ),
            publish_rollback_limits=PublishRollbackLimits(
                atomic_table_replace=False,
                shadow_table_supported=True,
                notes="对象存储绑定租户/项目专属前缀，支持暂存目录写入。",
            ),
        ),
        # 7. 受控本地文件连接器（CSV）
        ConnectorCapabilityEntry(
            connector_id="local-file-csv",
            connector_name="Controlled Local CSV File Connector",
            connector_version="2.3.9",
            seatunnel_version=st_ver,
            roles=[ConnectorRole.SOURCE, ConnectorRole.SINK],
            auth_modes=["CONTROLLED_MOUNT_POLICY"],
            support_status=SupportStatus.CONFIGURED,
            capabilities=ConnectorCapabilities(
                metadata_probe=True,
                full_load=True,
                incremental=False,
                cdc=False,
                delete_events=False,
                upsert=False,
                checkpoint_restore=True,
                schema_evolution=False,
                quality_split_compatible=True,
            ),
            publish_rollback_limits=PublishRollbackLimits(
                atomic_table_replace=False,
                shadow_table_supported=False,
                notes="仅允许访问受控挂载白名单根目录下的规范化路径，严防路径穿越与符号链接逃逸。",
            ),
        ),
        # 8. 受控本地文件连接器（JSON）
        ConnectorCapabilityEntry(
            connector_id="local-file-json",
            connector_name="Controlled Local JSON File Connector",
            connector_version="2.3.9",
            seatunnel_version=st_ver,
            roles=[ConnectorRole.SOURCE, ConnectorRole.SINK],
            auth_modes=["CONTROLLED_MOUNT_POLICY"],
            support_status=SupportStatus.CONFIGURED,
            capabilities=ConnectorCapabilities(
                metadata_probe=True,
                full_load=True,
                incremental=False,
                cdc=False,
                delete_events=False,
                upsert=False,
                checkpoint_restore=True,
                schema_evolution=False,
                quality_split_compatible=True,
            ),
            publish_rollback_limits=PublishRollbackLimits(
                atomic_table_replace=False,
                shadow_table_supported=False,
                notes="仅允许访问受控挂载白名单根目录下的规范化路径，适合日志清洗与异常分流场景。",
            ),
        ),
        # 9. 受控本地文件连接器（Parquet）
        ConnectorCapabilityEntry(
            connector_id="local-file-parquet",
            connector_name="Controlled Local Parquet File Connector",
            connector_version="2.3.9",
            seatunnel_version=st_ver,
            roles=[ConnectorRole.SOURCE, ConnectorRole.SINK],
            auth_modes=["CONTROLLED_MOUNT_POLICY"],
            support_status=SupportStatus.CONFIGURED,
            capabilities=ConnectorCapabilities(
                metadata_probe=True,
                full_load=True,
                incremental=False,
                cdc=False,
                delete_events=False,
                upsert=False,
                checkpoint_restore=True,
                schema_evolution=False,
                quality_split_compatible=True,
            ),
            publish_rollback_limits=PublishRollbackLimits(
                atomic_table_replace=False,
                shadow_table_supported=False,
                notes="仅允许访问受控挂载白名单根目录下的规范化路径。",
            ),
        ),
        # 10. Oracle JDBC（PRD-TBD-01 规划态）
        ConnectorCapabilityEntry(
            connector_id="oracle-jdbc",
            connector_name="Oracle JDBC Connector",
            connector_version="2.3.9",
            seatunnel_version=st_ver,
            roles=[ConnectorRole.SOURCE, ConnectorRole.SINK],
            auth_modes=["USERNAME_PASSWORD_SECRET_REF"],
            support_status=SupportStatus.PLANNED,
            capabilities=ConnectorCapabilities(
                metadata_probe=False,
                full_load=True,
                incremental=True,
                cdc=False,
                delete_events=False,
                upsert=True,
                checkpoint_restore=True,
                schema_evolution=False,
                quality_split_compatible=True,
            ),
            publish_rollback_limits=PublishRollbackLimits(
                atomic_table_replace=False,
                shadow_table_supported=False,
                notes="[PRD-TBD-01] 处于 PLANNED 阶段，默认 Compose 未启用重型 Oracle 容器。",
            ),
        ),
        # 11. ClickHouse JDBC（PRD-TBD-01 规划态）
        ConnectorCapabilityEntry(
            connector_id="clickhouse-jdbc",
            connector_name="ClickHouse JDBC Connector",
            connector_version="2.3.9",
            seatunnel_version=st_ver,
            roles=[ConnectorRole.SOURCE, ConnectorRole.SINK],
            auth_modes=["USERNAME_PASSWORD_SECRET_REF"],
            support_status=SupportStatus.PLANNED,
            capabilities=ConnectorCapabilities(
                metadata_probe=False,
                full_load=True,
                incremental=True,
                cdc=False,
                delete_events=False,
                upsert=False,
                checkpoint_restore=True,
                schema_evolution=False,
                quality_split_compatible=True,
            ),
            publish_rollback_limits=PublishRollbackLimits(
                atomic_table_replace=False,
                shadow_table_supported=True,
                notes="[PRD-TBD-01] 处于 PLANNED 阶段，归属可选 ext-clickhouse profile。",
            ),
        ),
        # 12. HTTP / REST API（PRD-TBD-01 规划态）
        ConnectorCapabilityEntry(
            connector_id="http-rest-api",
            connector_name="Generic HTTP / REST API Connector",
            connector_version="2.3.9",
            seatunnel_version=st_ver,
            roles=[ConnectorRole.SOURCE],
            auth_modes=["BEARER_TOKEN_SECRET_REF", "API_KEY_HEADER_SECRET_REF"],
            support_status=SupportStatus.PLANNED,
            capabilities=ConnectorCapabilities(
                metadata_probe=False,
                full_load=True,
                incremental=False,
                cdc=False,
                delete_events=False,
                upsert=False,
                checkpoint_restore=False,
                schema_evolution=False,
                quality_split_compatible=True,
            ),
            publish_rollback_limits=PublishRollbackLimits(
                atomic_table_replace=False,
                shadow_table_supported=False,
                notes="[PRD-TBD-01] 认证分页与限流策略待定（TBD），当前标记为 PLANNED。",
            ),
        ),
    ]


class ConnectorRegistry:
    """版本化连接器能力矩阵注册表服务。"""

    def __init__(self, entries: list[ConnectorCapabilityEntry] | None = None) -> None:
        self._entries: dict[str, ConnectorCapabilityEntry] = {}
        seed_list = entries if entries is not None else _build_seed_entries()
        for entry in seed_list:
            validate_entry_against_contract(entry.to_dict())
            self._entries[entry.connector_id] = entry

    def list_entries(
        self,
        *,
        role: ConnectorRole | None = None,
        support_status: SupportStatus | None = None,
    ) -> list[ConnectorCapabilityEntry]:
        """按可选角色与支持状态筛选连接器条目列表。"""
        results = list(self._entries.values())
        if role is not None:
            results = [e for e in results if role in e.roles]
        if support_status is not None:
            results = [e for e in results if e.support_status == support_status]
        return results

    def get_entry(self, connector_id: str) -> ConnectorCapabilityEntry:
        """按 connector_id 获取连接器能力条目；不存在时抛出 `ConnectorCapabilityUnsupportedError`。"""
        entry = self._entries.get(connector_id)
        if entry is None:
            raise ConnectorCapabilityUnsupportedError(
                f"未知或未注册的连接器: {connector_id}",
                detail={"connector_id": connector_id, "reason": "CONNECTOR_NOT_FOUND"},
            )
        return entry

    def validate_pipeline_requirement(
        self,
        *,
        connector_id: str,
        required_role: ConnectorRole,
        sync_mode: str,
        require_atomic_swap: bool = False,
        require_shadow_table: bool = False,
        require_quality_split: bool = False,
        require_delete_events: bool = False,
        require_upsert: bool = False,
    ) -> ConnectorCapabilityEntry:
        """确定性校验连接器是否支持指定的角色、同步模式与发布特性。

        若连接器处于 `PLANNED` 或 `UNSUPPORTED` 状态，或对应布尔能力为 False，
        立即抛出 `ConnectorCapabilityUnsupportedError`（错误码 `CONNECTOR_CAPABILITY_UNSUPPORTED`）。
        """
        entry = self.get_entry(connector_id)

        if entry.support_status in (SupportStatus.PLANNED, SupportStatus.UNSUPPORTED):
            raise ConnectorCapabilityUnsupportedError(
                f"连接器 {connector_id} 当前状态为 {entry.support_status.value}，不可用于可执行流水线制品",
                detail={
                    "connector_id": connector_id,
                    "support_status": entry.support_status.value,
                    "reason": "STATUS_NOT_EXECUTABLE",
                },
            )

        if required_role not in entry.roles:
            raise ConnectorCapabilityUnsupportedError(
                f"连接器 {connector_id} 不支持角色 {required_role.value}",
                detail={
                    "connector_id": connector_id,
                    "required_role": required_role.value,
                    "supported_roles": [r.value for r in entry.roles],
                },
            )

        mode_upper = sync_mode.upper()
        if mode_upper == "FULL_LOAD" and not entry.capabilities.full_load:
            raise ConnectorCapabilityUnsupportedError(
                f"连接器 {connector_id} 不支持 FULL_LOAD 全量同步模式",
                detail={"connector_id": connector_id, "sync_mode": mode_upper},
            )
        if mode_upper == "INCREMENTAL" and not entry.capabilities.incremental:
            raise ConnectorCapabilityUnsupportedError(
                f"连接器 {connector_id} 不支持 INCREMENTAL 增量同步模式",
                detail={"connector_id": connector_id, "sync_mode": mode_upper},
            )
        if mode_upper == "CDC" and required_role == ConnectorRole.SOURCE and not entry.capabilities.cdc:
            raise ConnectorCapabilityUnsupportedError(
                f"源端连接器 {connector_id} 不支持 CDC 同步模式",
                detail={"connector_id": connector_id, "sync_mode": mode_upper},
            )

        if require_atomic_swap and not entry.publish_rollback_limits.atomic_table_replace:
            raise ConnectorCapabilityUnsupportedError(
                f"目标连接器 {connector_id} 不支持原子表替换 (atomic_table_replace)",
                detail={"connector_id": connector_id, "requirement": "atomic_table_replace"},
            )
        if require_shadow_table and not entry.publish_rollback_limits.shadow_table_supported:
            raise ConnectorCapabilityUnsupportedError(
                f"目标连接器 {connector_id} 不支持影子表写入 (shadow_table_supported)",
                detail={"connector_id": connector_id, "requirement": "shadow_table_supported"},
            )
        if require_quality_split and not entry.capabilities.quality_split_compatible:
            raise ConnectorCapabilityUnsupportedError(
                f"连接器 {connector_id} 不支持流式质量分流 (quality_split_compatible)",
                detail={"connector_id": connector_id, "requirement": "quality_split_compatible"},
            )
        if require_delete_events and not entry.capabilities.delete_events:
            raise ConnectorCapabilityUnsupportedError(
                f"连接器 {connector_id} 不支持 Delete 事件同步 (delete_events)",
                detail={"connector_id": connector_id, "requirement": "delete_events"},
            )
        if require_upsert and not entry.capabilities.upsert:
            raise ConnectorCapabilityUnsupportedError(
                f"连接器 {connector_id} 不支持 Upsert 写入 (upsert)",
                detail={"connector_id": connector_id, "requirement": "upsert"},
            )

        return entry

    def record_verification_evidence(
        self,
        *,
        connector_id: str,
        new_status: SupportStatus,
        test_evidence_ref: str,
        verified_at: datetime | str,
    ) -> ConnectorCapabilityEntry:
        """在真实 L1/L2 集成测试通过后登记测试证据并晋升连接器状态。

        若缺少真实 `test_evidence_ref` 或时间戳格式非法，将拒绝晋升。
        """
        entry = self.get_entry(connector_id)
        if not test_evidence_ref or not test_evidence_ref.strip():
            raise ValueError("晋升连接器状态必须提供真实的 test_evidence_ref 证据引用")

        verified_str = (
            verified_at.isoformat() if isinstance(verified_at, datetime) else str(verified_at)
        )
        updated = ConnectorCapabilityEntry(
            schema_version=entry.schema_version,
            connector_id=entry.connector_id,
            connector_name=entry.connector_name,
            connector_version=entry.connector_version,
            seatunnel_version=entry.seatunnel_version,
            roles=list(entry.roles),
            auth_modes=list(entry.auth_modes),
            support_status=new_status,
            capabilities=entry.capabilities,
            publish_rollback_limits=entry.publish_rollback_limits,
            test_evidence_ref=test_evidence_ref.strip(),
            verified_at=verified_str,
        )
        validate_entry_against_contract(updated.to_dict())
        self._entries[connector_id] = updated
        return updated


def get_default_connector_registry() -> ConnectorRegistry:
    """获取默认初始化的版本化连接器注册中心实例。"""
    return ConnectorRegistry()

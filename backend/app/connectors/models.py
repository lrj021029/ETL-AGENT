"""连接器能力矩阵领域模型与异常定义。

本模块定义符合 `contracts/schemas/connector_registry.schema.json` 的数据结构，
以及连接器能力不支持（`CONNECTOR_CAPABILITY_UNSUPPORTED`）与文件路径越界（`FILE_PATH_TRAVERSAL_BLOCKED`）错误。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ConnectorRole(str, Enum):
    """连接器在数据流水线中承担的角色枚举。"""

    SOURCE = "SOURCE"
    SINK = "SINK"
    TRANSFORM = "TRANSFORM"


class SupportStatus(str, Enum):
    """连接器成熟度与验证状态枚举。

    严禁在无真实 L2 测试证据的情况下标记为 `END_TO_END_VERIFIED`。
    """

    PLANNED = "PLANNED"
    CONFIGURED = "CONFIGURED"
    INTEGRATION_TESTED = "INTEGRATION_TESTED"
    END_TO_END_VERIFIED = "END_TO_END_VERIFIED"
    UNSUPPORTED = "UNSUPPORTED"


class ConnectorCapabilityUnsupportedError(Exception):
    """当请求的同步模式、角色或发布特性超出连接器已声明能力时抛出。"""

    error_code: str = "CONNECTOR_CAPABILITY_UNSUPPORTED"

    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class FilePathTraversalBlockedError(Exception):
    """当本地文件连接器检测到路径穿越、符号链接逃逸或越出受控挂载根目录时抛出。"""

    error_code: str = "FILE_PATH_TRAVERSAL_BLOCKED"

    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


@dataclass(frozen=True)
class ConnectorCapabilities:
    """连接器细分技术能力布尔矩阵。"""

    metadata_probe: bool
    full_load: bool
    incremental: bool
    cdc: bool
    delete_events: bool
    upsert: bool
    checkpoint_restore: bool
    schema_evolution: bool
    quality_split_compatible: bool

    def to_dict(self) -> dict[str, bool]:
        """转换为符合 JSON Schema 契约的字典。"""
        return {
            "metadata_probe": self.metadata_probe,
            "full_load": self.full_load,
            "incremental": self.incremental,
            "cdc": self.cdc,
            "delete_events": self.delete_events,
            "upsert": self.upsert,
            "checkpoint_restore": self.checkpoint_restore,
            "schema_evolution": self.schema_evolution,
            "quality_split_compatible": self.quality_split_compatible,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConnectorCapabilities:
        """从字典反序列化连接器能力矩阵。"""
        return cls(
            metadata_probe=bool(data["metadata_probe"]),
            full_load=bool(data["full_load"]),
            incremental=bool(data["incremental"]),
            cdc=bool(data["cdc"]),
            delete_events=bool(data["delete_events"]),
            upsert=bool(data["upsert"]),
            checkpoint_restore=bool(data["checkpoint_restore"]),
            schema_evolution=bool(data["schema_evolution"]),
            quality_split_compatible=bool(data["quality_split_compatible"]),
        )


@dataclass(frozen=True)
class PublishRollbackLimits:
    """连接器在目标端影子表与原子替换发布方面的能力限制。"""

    atomic_table_replace: bool
    shadow_table_supported: bool
    notes: str

    def to_dict(self) -> dict[str, Any]:
        """转换为符合 JSON Schema 契约的字典。"""
        return {
            "atomic_table_replace": self.atomic_table_replace,
            "shadow_table_supported": self.shadow_table_supported,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PublishRollbackLimits:
        """从字典反序列化发布与回滚限制。"""
        return cls(
            atomic_table_replace=bool(data["atomic_table_replace"]),
            shadow_table_supported=bool(data["shadow_table_supported"]),
            notes=str(data["notes"]),
        )


@dataclass(frozen=True)
class ConnectorCapabilityEntry:
    """版本化连接器注册表条目（对应 `ConnectorCapabilityEntry` v1.0.0 契约）。"""

    connector_id: str
    connector_name: str
    connector_version: str
    seatunnel_version: str
    roles: list[ConnectorRole]
    auth_modes: list[str]
    support_status: SupportStatus
    capabilities: ConnectorCapabilities
    publish_rollback_limits: PublishRollbackLimits
    schema_version: str = "1.0.0"
    test_evidence_ref: str | None = None
    verified_at: str | None = None
    extra_check: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """强制校验契约不变量：未附带真实测试证据时禁止声明为 END_TO_END_VERIFIED。"""
        if self.schema_version != "1.0.0":
            raise ValueError(f"不支持的 schema_version: {self.schema_version}")
        if not self.roles:
            raise ValueError("roles 至少需要包含一个角色")
        if self.support_status in (
            SupportStatus.INTEGRATION_TESTED,
            SupportStatus.END_TO_END_VERIFIED,
        ):
            if not self.test_evidence_ref or not self.verified_at:
                raise ValueError(
                    f"连接器 {self.connector_id} 状态为 {self.support_status.value} 时，"
                    "必须提供非空的 test_evidence_ref 与 verified_at 证据！"
                )

    def to_dict(self) -> dict[str, Any]:
        """序列化为严格遵循 `connector_registry.schema.json` 的字典（无额外字段）。"""
        return {
            "schema_version": self.schema_version,
            "connector_id": self.connector_id,
            "connector_name": self.connector_name,
            "connector_version": self.connector_version,
            "seatunnel_version": self.seatunnel_version,
            "roles": [r.value for r in self.roles],
            "auth_modes": list(self.auth_modes),
            "support_status": self.support_status.value,
            "capabilities": self.capabilities.to_dict(),
            "publish_rollback_limits": self.publish_rollback_limits.to_dict(),
            "test_evidence_ref": self.test_evidence_ref,
            "verified_at": self.verified_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConnectorCapabilityEntry:
        """从契约字典构造连接器条目。"""
        return cls(
            schema_version=str(data.get("schema_version", "1.0.0")),
            connector_id=str(data["connector_id"]),
            connector_name=str(data["connector_name"]),
            connector_version=str(data["connector_version"]),
            seatunnel_version=str(data["seatunnel_version"]),
            roles=[ConnectorRole(r) for r in data["roles"]],
            auth_modes=[str(a) for a in data["auth_modes"]],
            support_status=SupportStatus(data["support_status"]),
            capabilities=ConnectorCapabilities.from_dict(data["capabilities"]),
            publish_rollback_limits=PublishRollbackLimits.from_dict(
                data["publish_rollback_limits"]
            ),
            test_evidence_ref=data.get("test_evidence_ref"),
            verified_at=data.get("verified_at"),
        )

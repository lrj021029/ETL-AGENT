"""连接器注册中心与能力矩阵模块（Connector Registry & Capability Matrix）。

严格对齐 `contracts/schemas/connector_registry.schema.json`（Contract v1.0.0）。
核心原则：在未完成真实 L2 容器端到端验收并附带真实测试证据前，绝不将任何连接器标记为 `END_TO_END_VERIFIED`。
"""

from backend.app.connectors.models import (
    ConnectorCapabilities,
    ConnectorCapabilityEntry,
    ConnectorCapabilityUnsupportedError,
    ConnectorRole,
    FilePathTraversalBlockedError,
    PublishRollbackLimits,
    SupportStatus,
)
from backend.app.connectors.probe import (
    ControlledFileBoundary,
    ReadOnlyMetadataProbeService,
    compute_schema_fingerprint,
)
from backend.app.connectors.registry import (
    ConnectorRegistry,
    get_default_connector_registry,
    validate_entry_against_contract,
)

__all__ = [
    "ConnectorCapabilities",
    "ConnectorCapabilityEntry",
    "ConnectorCapabilityUnsupportedError",
    "ConnectorRegistry",
    "ConnectorRole",
    "ControlledFileBoundary",
    "FilePathTraversalBlockedError",
    "PublishRollbackLimits",
    "ReadOnlyMetadataProbeService",
    "SupportStatus",
    "compute_schema_fingerprint",
    "get_default_connector_registry",
    "validate_entry_against_contract",
]

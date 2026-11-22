"""不可变元数据快照（MetadataSnapshot）、敏感字段识别、脱敏采样与 Schema Drift 差异计算模块。"""
from __future__ import annotations

from app.metadata.service import (
    MetadataProberProtocol,
    MetadataService,
    SqlAlchemyReadOnlyMetadataProber,
    auto_detect_sensitive_column,
    compute_schema_drift,
    compute_schema_fingerprint,
    mask_sample_rows,
)

__all__ = [
    "MetadataProberProtocol",
    "SqlAlchemyReadOnlyMetadataProber",
    "MetadataService",
    "auto_detect_sensitive_column",
    "compute_schema_fingerprint",
    "compute_schema_drift",
    "mask_sample_rows",
]

"""领域 2.2：连接、元数据快照与凭据引用模型（13 张表）。

严格对应 contracts/db/erd.md §2.2：
secret_references, data_connections, connection_versions, metadata_profiles, metadata_snapshots,
metadata_objects, metadata_columns, metadata_statistics, sensitive_classifications,
schema_fingerprints, schema_drift_events, uploaded_files, object_storage_refs
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.models.base import Base, UUIDPrimaryKeyMixin, utc_now


class SecretReference(UUIDPrimaryKeyMixin, Base):
    """受管凭据引用表（严禁存储密码、API Key、私钥明文，仅存引用路径与版本号）。"""

    __tablename__ = "secret_references"
    __table_args__ = (Index("ix_secret_references_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_path: Mapped[str] = mapped_column(String(255), nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    masked_preview: Mapped[str] = mapped_column(String(64), nullable=False, default="******")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class DataConnection(UUIDPrimaryKeyMixin, Base):
    """数据源与目标端连接配置主表（仅含非敏感配置 JSON 与 secret_ref_id）。"""

    __tablename__ = "data_connections"
    __table_args__ = (Index("ix_data_connections_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    connector_id: Mapped[str] = mapped_column(String(128), nullable=False)
    connection_type: Mapped[str] = mapped_column(String(64), nullable=False)
    config_non_secret_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    secret_ref_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("secret_references.id", ondelete="SET NULL"), nullable=True
    )
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    last_health_status: Mapped[str] = mapped_column(String(32), nullable=False, default="UNCHECKED")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ConnectionVersion(UUIDPrimaryKeyMixin, Base):
    """连接配置不可变版本历史表。"""

    __tablename__ = "connection_versions"
    __table_args__ = (
        UniqueConstraint("connection_id", "version_no", name="uq_connection_versions_conn_ver"),
        Index("ix_connection_versions_org_proj", "organization_id", "project_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("data_connections.id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    config_non_secret_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    secret_ref_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    secret_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    config_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class MetadataProfile(UUIDPrimaryKeyMixin, Base):
    """元数据采集与样本策略配置表。"""

    __tablename__ = "metadata_profiles"
    __table_args__ = (Index("ix_metadata_profiles_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("data_connections.id", ondelete="CASCADE"), nullable=False
    )
    refresh_cron: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sample_policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class MetadataSnapshot(UUIDPrimaryKeyMixin, Base):
    """不可变元数据快照表（每次刷新必须新建记录，严禁原地更新覆盖）。"""

    __tablename__ = "metadata_snapshots"
    __table_args__ = (Index("ix_metadata_snapshots_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("metadata_profiles.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("data_connections.id", ondelete="CASCADE"), nullable=False
    )
    connection_version: Mapped[int] = mapped_column(Integer, nullable=False)
    connector_version: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    classification_rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    stats_source: Mapped[str] = mapped_column(String(64), nullable=False, default="READ_ONLY_PROBE")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    captured_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class MetadataObject(UUIDPrimaryKeyMixin, Base):
    """元数据表/视图对象记录表。"""

    __tablename__ = "metadata_objects"
    __table_args__ = (Index("ix_metadata_objects_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("metadata_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    schema_name: Mapped[str] = mapped_column(String(128), nullable=False)
    object_name: Mapped[str] = mapped_column(String(128), nullable=False)
    object_type: Mapped[str] = mapped_column(String(32), nullable=False, default="TABLE")
    row_count_approx: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    bytes_approx: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    primary_key_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    indexes_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)


class MetadataColumn(UUIDPrimaryKeyMixin, Base):
    """元数据列定义记录表。"""

    __tablename__ = "metadata_columns"
    __table_args__ = (Index("ix_metadata_columns_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    object_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("metadata_objects.id", ondelete="CASCADE"), nullable=False
    )
    column_name: Mapped[str] = mapped_column(String(128), nullable=False)
    data_type: Mapped[str] = mapped_column(String(128), nullable=False)
    nullable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    ordinal_position: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    default_expr: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)


class MetadataStatistic(UUIDPrimaryKeyMixin, Base):
    """元数据统计与脱敏样本表。"""

    __tablename__ = "metadata_statistics"
    __table_args__ = (Index("ix_metadata_statistics_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("metadata_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    object_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("metadata_objects.id", ondelete="CASCADE"), nullable=False
    )
    stats_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    masked_sample_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    sample_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SensitiveClassification(UUIDPrimaryKeyMixin, Base):
    """敏感字段自动识别与人工分级标记表。"""

    __tablename__ = "sensitive_classifications"
    __table_args__ = (Index("ix_sensitive_classifications_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("metadata_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    object_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("metadata_objects.id", ondelete="CASCADE"), nullable=False
    )
    column_name: Mapped[str] = mapped_column(String(128), nullable=False)
    classification_level: Mapped[str] = mapped_column(String(32), nullable=False)
    pii_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="AUTO_DETECT")
    marked_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    marked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class SchemaFingerprint(UUIDPrimaryKeyMixin, Base):
    """Schema 指纹记录表。"""

    __tablename__ = "schema_fingerprints"
    __table_args__ = (Index("ix_schema_fingerprints_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("data_connections.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("metadata_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    fingerprint_sha256: Mapped[str] = mapped_column(String(128), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class SchemaDriftEvent(UUIDPrimaryKeyMixin, Base):
    """Schema 漂移事件对比记录表。"""

    __tablename__ = "schema_drift_events"
    __table_args__ = (Index("ix_schema_drift_events_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("data_connections.id", ondelete="CASCADE"), nullable=False
    )
    from_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("metadata_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    to_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("metadata_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    diff_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ObjectStorageRef(UUIDPrimaryKeyMixin, Base):
    """租户隔离对象存储（MinIO / S3）引用表。"""

    __tablename__ = "object_storage_refs"
    __table_args__ = (Index("ix_object_storage_refs_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    etag: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    sha256: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class UploadedFile(UUIDPrimaryKeyMixin, Base):
    """受控上传文件记录表。"""

    __tablename__ = "uploaded_files"
    __table_args__ = (Index("ix_uploaded_files_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(128), nullable=False)
    object_storage_ref_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("object_storage_refs.id", ondelete="CASCADE"), nullable=False
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

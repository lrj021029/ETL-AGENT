"""领域 2.3：Pipeline、LangGraph Agent 与不可变执行制品模型（13 张表）。

严格对应 contracts/db/erd.md §2.3：
agent_threads, workflow_runs, pipeline_definitions, pipeline_drafts, pipeline_versions,
artifact_manifests, artifact_blobs, etl_plan_modules, sql_artifacts, hocon_artifacts,
quality_contracts, runtime_contracts, connector_capability_versions
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.models.base import Base, UUIDPrimaryKeyMixin, utc_now


class AgentThread(UUIDPrimaryKeyMixin, Base):
    """Studio LangGraph 会话线程表（受 RLS 保护，Resume 前必须校验 user_id/org_id/project_id 归属）。"""

    __tablename__ = "agent_threads"
    __table_args__ = (Index("ix_agent_threads_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    langgraph_thread_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="New ETL Thread")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    pending_clarification_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class WorkflowRun(UUIDPrimaryKeyMixin, Base):
    """LangGraph 推理与自动修复轮次审计记录表。"""

    __tablename__ = "workflow_runs"
    __table_args__ = (Index("ix_workflow_runs_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    thread_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_threads.id", ondelete="CASCADE"), nullable=False
    )
    pipeline_draft_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RUNNING")
    repair_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_repair_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    model_provider_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    llm_audit_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PipelineDefinition(UUIDPrimaryKeyMixin, Base):
    """Pipeline 主定义表。"""

    __tablename__ = "pipeline_definitions"
    __table_args__ = (Index("ix_pipeline_definitions_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sync_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="FULL_LOAD")
    source_connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sink_connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    latest_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class PipelineDraft(UUIDPrimaryKeyMixin, Base):
    """Studio 未冻结草稿表（运行时绝不可直接从草稿读取执行配置）。"""

    __tablename__ = "pipeline_drafts"
    __table_args__ = (Index("ix_pipeline_drafts_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pipeline_definitions.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    etl_plan_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    sql_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    hocon_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    quality_contract_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    runtime_contract_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    gate_report_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class PipelineVersion(UUIDPrimaryKeyMixin, Base):
    """不可变 PipelineVersion 制品版本表。"""

    __tablename__ = "pipeline_versions"
    __table_args__ = (
        UniqueConstraint("pipeline_id", "revision", name="uq_pipeline_versions_pipe_rev"),
        Index("ix_pipeline_versions_org_proj", "organization_id", "project_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pipeline_definitions.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artifact_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    manifest_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="FROZEN")
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ArtifactManifest(UUIDPrimaryKeyMixin, Base):
    """不可变 ArtifactManifest 清单表（基于 JCS Canonical JSON 计算 SHA256）。"""

    __tablename__ = "artifact_manifests"
    __table_args__ = (Index("ix_artifact_manifests_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    pipeline_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pipeline_versions.id", ondelete="CASCADE"), nullable=False
    )
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    generator_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    compiler_version: Mapped[str] = mapped_column(String(64), nullable=False, default="1.0.0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ArtifactBlob(UUIDPrimaryKeyMixin, Base):
    """大型制品二进制/文本对象引用表。"""

    __tablename__ = "artifact_blobs"
    __table_args__ = (Index("ix_artifact_blobs_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sha256: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    storage_ref_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class EtlPlanModule(UUIDPrimaryKeyMixin, Base):
    """结构化 EtlPlan DSL 模块表。"""

    __tablename__ = "etl_plan_modules"
    __table_args__ = (Index("ix_etl_plan_modules_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    pipeline_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pipeline_versions.id", ondelete="CASCADE"), nullable=False
    )
    module_type: Mapped[str] = mapped_column(String(64), nullable=False)
    dsl_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)


class SqlArtifact(UUIDPrimaryKeyMixin, Base):
    """经过 AST 门禁与执行位置编译器的 SQL 制品表。"""

    __tablename__ = "sql_artifacts"
    __table_args__ = (Index("ix_sql_artifacts_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    pipeline_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pipeline_versions.id", ondelete="CASCADE"), nullable=False
    )
    sql_purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    dialect: Mapped[str] = mapped_column(String(32), nullable=False)
    sql_text: Mapped[str] = mapped_column(Text, nullable=False)
    execution_strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_reason_codes: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    ast_report_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)


class HoconArtifact(UUIDPrimaryKeyMixin, Base):
    """SeaTunnel HOCON 模板制品表（仅包含 ${SECRET:...} 占位符，零明文密码）。"""

    __tablename__ = "hocon_artifacts"
    __table_args__ = (Index("ix_hocon_artifacts_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    pipeline_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pipeline_versions.id", ondelete="CASCADE"), nullable=False
    )
    hocon_template_text: Mapped[str] = mapped_column(Text, nullable=False)
    secret_placeholders_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    validation_report_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)


class QualityContractModel(UUIDPrimaryKeyMixin, Base):
    """数据质量契约（QualityContract）持久化表。"""

    __tablename__ = "quality_contracts"
    __table_args__ = (Index("ix_quality_contracts_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    pipeline_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pipeline_versions.id", ondelete="CASCADE"), nullable=True
    )
    contract_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class RuntimeContractModel(UUIDPrimaryKeyMixin, Base):
    """运行监督契约（RuntimeSupervisionContract）持久化表。"""

    __tablename__ = "runtime_contracts"
    __table_args__ = (Index("ix_runtime_contracts_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    pipeline_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pipeline_versions.id", ondelete="CASCADE"), nullable=True
    )
    contract_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ConnectorCapabilityVersion(UUIDPrimaryKeyMixin, Base):
    """版本化 Connector 能力矩阵表（全局元数据表）。"""

    __tablename__ = "connector_capability_versions"
    __table_args__ = (
        UniqueConstraint(
            "connector_id",
            "connector_version",
            "seatunnel_version",
            name="uq_connector_cap_ver",
        ),
    )

    connector_id: Mapped[str] = mapped_column(String(128), nullable=False)
    connector_name: Mapped[str] = mapped_column(String(128), nullable=False)
    connector_version: Mapped[str] = mapped_column(String(64), nullable=False)
    seatunnel_version: Mapped[str] = mapped_column(String(64), nullable=False)
    capabilities_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    support_status: Mapped[str] = mapped_column(String(32), nullable=False, default="CONFIGURED")
    test_evidence_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

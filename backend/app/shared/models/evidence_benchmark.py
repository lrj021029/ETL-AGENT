"""领域 2.6：Evidence Ledger 哈希链与 Benchmark 评估模型（10 张表）。

严格对应 contracts/db/erd.md §2.6：
evidence_events, evidence_checkpoints, evidence_verification_runs,
benchmark_datasets, benchmark_cases, benchmark_runs, benchmark_case_results,
model_provider_versions, policy_versions, generator_versions
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.models.base import Base, UUIDPrimaryKeyMixin, utc_now


class EvidenceEvent(UUIDPrimaryKeyMixin, Base):
    """只追加（Append-only）审计证据哈希链事件表。"""

    __tablename__ = "evidence_events"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "project_id",
            "sequence_no",
            name="uq_evidence_events_org_proj_seq",
        ),
        Index("ix_evidence_events_org_proj", "organization_id", "project_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sequence_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    timestamp: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    artifact_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    previous_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(128), nullable=False)


class EvidenceCheckpoint(UUIDPrimaryKeyMixin, Base):
    """Ed25519 签名的证据链检查点表。"""

    __tablename__ = "evidence_checkpoints"
    __table_args__ = (Index("ix_evidence_checkpoints_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    from_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    to_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    head_event_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    signing_key_id: Mapped[str] = mapped_column(String(128), nullable=False)
    signature_b64url: Mapped[str] = mapped_column(Text, nullable=False)
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class EvidenceVerificationRun(UUIDPrimaryKeyMixin, Base):
    """证据链完整性与签名校验记录表。"""

    __tablename__ = "evidence_verification_runs"
    __table_args__ = (Index("ix_evidence_verification_runs_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    verified_up_to_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chain_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    signatures_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    broken_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    verified_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class BenchmarkDataset(UUIDPrimaryKeyMixin, Base):
    """版本化确定性 Benchmark 数据集（10K / 100K / 1M）定义表。"""

    __tablename__ = "benchmark_datasets"
    __table_args__ = (Index("ix_benchmark_datasets_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    row_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    anomaly_spec_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    manifest_sha256: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class BenchmarkCase(UUIDPrimaryKeyMixin, Base):
    """Benchmark 测试用例定义表。"""

    __tablename__ = "benchmark_cases"

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("benchmark_datasets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    case_code: Mapped[str] = mapped_column(String(64), nullable=False)
    layer: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    is_p0_gate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_p1_gate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    expected_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class BenchmarkRun(UUIDPrimaryKeyMixin, Base):
    """L0 / L1 / L2 Benchmark 运行结果表（P0/P1 安全门槛独立硬判定）。"""

    __tablename__ = "benchmark_runs"
    __table_args__ = (Index("ix_benchmark_runs_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("benchmark_datasets.id", ondelete="CASCADE"), nullable=False
    )
    layer: Mapped[str] = mapped_column(String(16), nullable=False)
    model_provider_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    policy_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    generator_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    is_mock_llm: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    p0_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    p1_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    overall_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    raw_metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    composite_scores_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    scoring_profile_version: Mapped[str] = mapped_column(String(64), nullable=False, default="v1.0.0-tbd")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BenchmarkCaseResult(UUIDPrimaryKeyMixin, Base):
    """Benchmark 单用例执行明细表。"""

    __tablename__ = "benchmark_case_results"

    benchmark_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("benchmark_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("benchmark_cases.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    actual_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ModelProviderVersion(UUIDPrimaryKeyMixin, Base):
    """LLM 模型 Provider 版本记录表。"""

    __tablename__ = "model_provider_versions"
    __table_args__ = (Index("ix_model_provider_versions_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class PolicyVersion(UUIDPrimaryKeyMixin, Base):
    """PDP 策略版本表。"""

    __tablename__ = "policy_versions"
    __table_args__ = (Index("ix_policy_versions_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version_tag: Mapped[str] = mapped_column(String(64), nullable=False)
    rules_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    rules_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    approved_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class GeneratorVersion(UUIDPrimaryKeyMixin, Base):
    """Agent Prompt 与确定性编译器版本记录表。"""

    __tablename__ = "generator_versions"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    compiler_version: Mapped[str] = mapped_column(String(64), nullable=False)
    git_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

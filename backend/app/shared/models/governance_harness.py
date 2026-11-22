"""领域 2.4：Governance 与 Harness 安全内核模型（10 张表）。

严格对应 contracts/db/erd.md §2.4：
preparations, preparation_fingerprints, approval_slots, approval_decisions,
capability_issuances, capability_consumptions, execution_commands, replay_records,
outbox_events, outbox_deliveries
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.models.base import Base, UUIDPrimaryKeyMixin, utc_now


class Preparation(UUIDPrimaryKeyMixin, Base):
    """Harness Prepare 冻结准备单表（preparation_type IN ('EXECUTION','ROLLBACK','STANDING_SCHEDULE')）。"""

    __tablename__ = "preparations"
    __table_args__ = (Index("ix_preparations_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    preparation_type: Mapped[str] = mapped_column(String(32), nullable=False, default="EXECUTION")
    pipeline_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    pipeline_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    artifact_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False, default="dev")
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING_APPROVAL")
    frozen_facts_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    rollback_plan_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    budget_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    fingerprint_sha256: Mapped[str] = mapped_column(String(128), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class PreparationFingerprint(UUIDPrimaryKeyMixin, Base):
    """Preparation 输入事实指纹构成表。"""

    __tablename__ = "preparation_fingerprints"
    __table_args__ = (Index("ix_preparation_fingerprints_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    preparation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("preparations.id", ondelete="CASCADE"), nullable=False
    )
    components_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    fingerprint_sha256: Mapped[str] = mapped_column(String(128), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ApprovalSlot(UUIDPrimaryKeyMixin, Base):
    """独立职责分离审批槽位表（slot_role IN ('DATA_CHECKER','SECURITY_CHECKER','SINGLE_CHECKER','AUTO_P3')）。"""

    __tablename__ = "approval_slots"
    __table_args__ = (
        UniqueConstraint("preparation_id", "slot_role", name="uq_approval_slots_prep_role"),
        Index("ix_approval_slots_org_proj", "organization_id", "project_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    preparation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("preparations.id", ondelete="CASCADE"), nullable=False
    )
    slot_role: Mapped[str] = mapped_column(String(32), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")


class ApprovalDecision(UUIDPrimaryKeyMixin, Base):
    """实名 Checker 审批决策记录表。"""

    __tablename__ = "approval_decisions"
    __table_args__ = (Index("ix_approval_decisions_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    preparation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("preparations.id", ondelete="CASCADE"), nullable=False
    )
    slot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("approval_slots.id", ondelete="CASCADE"), nullable=False
    )
    approver_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    fact_digest_sha256: Mapped[str] = mapped_column(String(128), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class CapabilityIssuance(UUIDPrimaryKeyMixin, Base):
    """Ed25519 单次短时（5分钟）Capability 签发记录表。"""

    __tablename__ = "capability_issuances"
    __table_args__ = (Index("ix_capability_issuances_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    jti: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), unique=True, nullable=False, index=True)
    preparation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    pipeline_version_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    approval_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    signing_key_id: Mapped[str] = mapped_column(String(128), nullable=False)
    nonce: Mapped[str] = mapped_column(String(128), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CapabilityConsumption(UUIDPrimaryKeyMixin, Base):
    """Capability 单次消费终极事实表（jti UNIQUE 唯一约束确保绝无二次消费）。"""

    __tablename__ = "capability_consumptions"
    __table_args__ = (Index("ix_capability_consumptions_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    jti: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), unique=True, nullable=False, index=True)
    consumed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    execution_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    command_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    consumed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ExecutionCommand(UUIDPrimaryKeyMixin, Base):
    """受管执行命令事实表（command_id UNIQUE 唯一约束确保 Worker 消费幂等）。"""

    __tablename__ = "execution_commands"
    __table_args__ = (Index("ix_execution_commands_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    command_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), unique=True, nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    execution_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    command_type: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_jti: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ReplayRecord(UUIDPrimaryKeyMixin, Base):
    """重放攻击拦截审计记录表。"""

    __tablename__ = "replay_records"
    __table_args__ = (Index("ix_replay_records_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    jti: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    nonce: Mapped[str] = mapped_column(String(128), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    source_component: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class OutboxEvent(UUIDPrimaryKeyMixin, Base):
    """事务性 Outbox 待投递事件表。"""

    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_events_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_ref_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class OutboxDelivery(UUIDPrimaryKeyMixin, Base):
    """Outbox 投递状态与租约（SELECT ... FOR UPDATE SKIP LOCKED）跟踪表。"""

    __tablename__ = "outbox_deliveries"

    outbox_event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("outbox_events.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

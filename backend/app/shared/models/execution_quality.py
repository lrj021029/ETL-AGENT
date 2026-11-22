"""领域 2.5：Execution、质量报告、受管发布、回滚与 Schedule 模型（12 张表）。

严格对应 contracts/db/erd.md §2.5：
execution_runs, execution_state_events, external_job_refs, execution_metrics,
execution_logs_refs, quality_reports, quality_rule_results, rejected_record_summaries,
published_dataset_versions, rollback_runs, schedules, schedule_triggers
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, JSON, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.models.base import Base, UUIDPrimaryKeyMixin, utc_now


class ExecutionRun(UUIDPrimaryKeyMixin, Base):
    """统一主执行记录表（BATCH 与 CDC 共用实体，分别遵循分模式状态机）。"""

    __tablename__ = "execution_runs"
    __table_args__ = (Index("ix_execution_runs_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    pipeline_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    artifact_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="BATCH")
    preparation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    capability_jti: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    command_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), unique=True, nullable=False, index=True)
    external_engine: Mapped[str] = mapped_column(String(64), nullable=False, default="SEATUNNEL_ZETA")
    external_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="CREATED", index=True)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_position_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    checkpoint_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metrics_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    published_dataset_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    diagnosis_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ExecutionStateEvent(UUIDPrimaryKeyMixin, Base):
    """执行状态跃迁历史事件表。"""

    __tablename__ = "execution_state_events"
    __table_args__ = (Index("ix_execution_state_events_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False, default="TRANSITION")
    detail_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ExternalJobRef(UUIDPrimaryKeyMixin, Base):
    """外部引擎（SeaTunnel Zeta）作业稳定提交标识与对账状态表。"""

    __tablename__ = "external_job_refs"
    __table_args__ = (Index("ix_external_job_refs_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    engine_type: Mapped[str] = mapped_column(String(64), nullable=False, default="SEATUNNEL_ZETA")
    stable_submit_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    external_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    submit_state: Mapped[str] = mapped_column(String(32), nullable=False, default="REQUEST_SUBMITTED")
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_status_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ExecutionMetric(UUIDPrimaryKeyMixin, Base):
    """执行时序采样指标表。"""

    __tablename__ = "execution_metrics"
    __table_args__ = (Index("ix_execution_metrics_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    read_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    read_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    validated_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    valid_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    write_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    write_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    error_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reject_rate_total: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reject_rate_window: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    throughput_rps: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    amplification_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    cdc_lag_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    checkpoint_position: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ExecutionLogsRef(UUIDPrimaryKeyMixin, Base):
    """执行日志与 Trace 查询关联引用表。"""

    __tablename__ = "execution_logs_refs"
    __table_args__ = (Index("ix_execution_logs_refs_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    loki_query_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class QualityReport(UUIDPrimaryKeyMixin, Base):
    """执行质量评估报告表。"""

    __tablename__ = "quality_reports"
    __table_args__ = (Index("ix_quality_reports_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("execution_runs.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    contract_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    validated_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    valid_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    error_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reject_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    failure_reasons_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class QualityRuleResult(UUIDPrimaryKeyMixin, Base):
    """单条质量规则检查明细表。"""

    __tablename__ = "quality_rule_results"
    __table_args__ = (Index("ix_quality_rule_results_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    quality_report_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("quality_reports.id", ondelete="CASCADE"), nullable=False
    )
    rule_id: Mapped[str] = mapped_column(String(128), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(64), nullable=False)
    checked_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    failed_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    detail_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class RejectedRecordSummary(UUIDPrimaryKeyMixin, Base):
    """错误分流表违规记录脱敏聚合摘要表。"""

    __tablename__ = "rejected_record_summaries"
    __table_args__ = (Index("ix_rejected_record_summaries_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    error_table_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(128), nullable=False)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False)
    count_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sample_redacted_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class PublishedDatasetVersion(UUIDPrimaryKeyMixin, Base):
    """目标表受管发布（Doris Swap 等）数据版本表。"""

    __tablename__ = "published_dataset_versions"
    __table_args__ = (Index("ix_published_dataset_versions_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    execution_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    pipeline_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    artifact_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    target_connection_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    target_table_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    shadow_table_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    previous_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    archive_table_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    swap_result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    quality_report_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PUBLISHED")
    published_by: Mapped[str] = mapped_column(String(128), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RollbackRun(UUIDPrimaryKeyMixin, Base):
    """受管回滚执行记录表（所有回滚必须经 Harness 授权）。"""

    __tablename__ = "rollback_runs"
    __table_args__ = (Index("ix_rollback_runs_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    rollback_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_execution_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    target_published_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    preparation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    capability_jti: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    command_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), unique=True, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="CREATED")
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    initiated_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Schedule(UUIDPrimaryKeyMixin, Base):
    """受管定时调度配置表（必须绑定已审批的 StandingSchedulePreparation）。"""

    __tablename__ = "schedules"
    __table_args__ = (Index("ix_schedules_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    pipeline_version_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    standing_preparation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("preparations.id", ondelete="RESTRICT"), nullable=False
    )
    cron_expression: Mapped[str] = mapped_column(String(64), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    concurrency_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="FORBID")
    misfire_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="SKIP")
    version_binding_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="PINNED")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ScheduleTrigger(UUIDPrimaryKeyMixin, Base):
    """定时调度触发历史记录表。"""

    __tablename__ = "schedule_triggers"
    __table_args__ = (Index("ix_schedule_triggers_org_proj", "organization_id", "project_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    schedule_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False
    )
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

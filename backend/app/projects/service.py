"""项目（Project）生命周期与总览工作台（Overview Dashboard）实时统计服务。"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.shared.errors import AppError, ErrorCode
from app.shared.models.connections_metadata import DataConnection
from app.shared.models.evidence_benchmark import EvidenceEvent
from app.shared.models.execution_quality import ExecutionRun, QualityReport
from app.shared.models.governance_harness import Preparation
from app.shared.models.iam import OrganizationMember, Project, ProjectMember
from app.shared.models.pipeline_artifacts import PipelineDefinition, PipelineVersion


class ProjectService:
    """提供项目列表、创建与总览工作台聚合查询服务。"""

    def list_projects_in_organization(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        """列出当前用户在活跃组织内可访问的项目列表（强制双重成员校验）。"""
        org_member = session.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == user_id,
                OrganizationMember.status == "ACTIVE",
            )
        ).scalar_one_or_none()
        if org_member is None:
            raise AppError(
                ErrorCode.TENANT_ACCESS_DENIED,
                "当前用户不具备该组织的活跃成员资格",
            )

        rows = (
            session.execute(
                select(Project, ProjectMember)
                .join(ProjectMember, ProjectMember.project_id == Project.id)
                .where(
                    Project.organization_id == organization_id,
                    ProjectMember.user_id == user_id,
                    ProjectMember.status == "ACTIVE",
                    Project.status == "ACTIVE",
                )
                .order_by(Project.created_at.asc())
            )
            .all()
        )
        return [
            {
                "id": str(proj.id),
                "organization_id": str(proj.organization_id),
                "slug": proj.slug,
                "name": proj.name,
                "environment": proj.environment,
                "llm_data_policy_json": proj.llm_data_policy_json,
                "status": proj.status,
                "created_at": proj.created_at.isoformat(),
            }
            for proj, _ in rows
        ]

    def create_project(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        slug: str,
        name: str,
        environment: str = "dev",
        llm_data_policy_json: dict[str, Any] | None = None,
        creator_user_id: uuid.UUID,
    ) -> dict[str, Any]:
        """在指定组织下创建新项目，并将创建者加入 ProjectMember。"""
        org_member = session.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == creator_user_id,
                OrganizationMember.status == "ACTIVE",
            )
        ).scalar_one_or_none()
        if org_member is None:
            raise AppError(
                ErrorCode.TENANT_ACCESS_DENIED,
                "用户不属于目标组织，禁止创建项目",
            )

        clean_slug = slug.strip().lower()
        existing = session.execute(
            select(Project).where(
                Project.organization_id == organization_id,
                Project.slug == clean_slug,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise AppError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                f"组织内已存在同 slug 项目 '{clean_slug}'",
            )

        proj = Project(
            organization_id=organization_id,
            slug=clean_slug,
            name=name.strip(),
            environment=environment,
            llm_data_policy_json=llm_data_policy_json
            or {
                "allow_schema_metadata": True,
                "allow_masked_samples": True,
                "allow_raw_sensitive_rows": False,
            },
            status="ACTIVE",
        )
        session.add(proj)
        session.flush()

        pm = ProjectMember(
            organization_id=organization_id,
            project_id=proj.id,
            user_id=creator_user_id,
            status="ACTIVE",
        )
        session.add(pm)
        session.flush()

        return {
            "id": str(proj.id),
            "organization_id": str(proj.organization_id),
            "slug": proj.slug,
            "name": proj.name,
            "environment": proj.environment,
            "llm_data_policy_json": proj.llm_data_policy_json,
            "status": proj.status,
            "created_at": proj.created_at.isoformat(),
        }

    def get_project_overview(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
    ) -> dict[str, Any]:
        """从数据库真实持久化事实聚合总览工作台（Overview Dashboard）指标（遵循 PRD §4.1）。"""
        proj = session.get(Project, project_id)
        if proj is None or proj.organization_id != organization_id:
            raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "目标项目不存在或跨组织越权")

        conn_rows = (
            session.execute(
                select(DataConnection).where(
                    DataConnection.organization_id == organization_id,
                    DataConnection.project_id == project_id,
                )
            )
            .scalars()
            .all()
        )
        healthy_conns = sum(1 for c in conn_rows if c.last_health_status == "HEALTHY")

        pipelines_count = (
            session.execute(
                select(func.count(PipelineDefinition.id)).where(
                    PipelineDefinition.organization_id == organization_id,
                    PipelineDefinition.project_id == project_id,
                )
            ).scalar()
            or 0
        )
        versions_count = (
            session.execute(
                select(func.count(PipelineVersion.id)).where(
                    PipelineVersion.organization_id == organization_id,
                    PipelineVersion.project_id == project_id,
                )
            ).scalar()
            or 0
        )

        preps = (
            session.execute(
                select(Preparation).where(
                    Preparation.organization_id == organization_id,
                    Preparation.project_id == project_id,
                )
            )
            .scalars()
            .all()
        )
        pending_approvals = sum(1 for p in preps if p.status == "PENDING_APPROVAL")
        ready_to_commit = sum(1 for p in preps if p.status == "APPROVED")

        runs = (
            session.execute(
                select(ExecutionRun).where(
                    ExecutionRun.organization_id == organization_id,
                    ExecutionRun.project_id == project_id,
                )
            )
            .scalars()
            .all()
        )
        batch_runs = sum(1 for r in runs if r.execution_mode == "BATCH")
        cdc_runs = sum(1 for r in runs if r.execution_mode == "CDC")
        succeeded_runs = sum(1 for r in runs if r.status in ("SUCCEEDED", "STREAMING"))
        success_rate = round(succeeded_runs / len(runs), 4) if runs else 1.0

        quality_reports = (
            session.execute(
                select(QualityReport).where(
                    QualityReport.organization_id == organization_id,
                    QualityReport.project_id == project_id,
                )
            )
            .scalars()
            .all()
        )
        avg_reject_rate = (
            round(sum(q.reject_rate for q in quality_reports) / len(quality_reports), 4)
            if quality_reports
            else 0.0
        )

        recent_evidence = (
            session.execute(
                select(EvidenceEvent)
                .where(
                    EvidenceEvent.organization_id == organization_id,
                    EvidenceEvent.project_id == project_id,
                )
                .order_by(EvidenceEvent.sequence_no.desc())
                .limit(5)
            )
            .scalars()
            .all()
        )

        evidence_items = [
            {
                "event_id": str(ev.id),
                "sequence_no": ev.sequence_no,
                "event_type": ev.event_type,
                "actor": ev.actor,
                "resource_ref": ev.resource_ref,
                "artifact_hash": ev.artifact_hash,
                "payload_digest": ev.payload_digest,
                "previous_hash": ev.previous_hash,
                "event_hash": ev.event_hash,
                "timestamp": ev.timestamp.isoformat() if hasattr(ev.timestamp, "isoformat") else str(ev.timestamp),
            }
            for ev in recent_evidence
        ]
        pending_preparations = sum(1 for p in preps if p.status in ("DRAFT", "PREPARED"))
        return {
            "organization_id": str(organization_id),
            "project_id": str(project_id),
            "project_name": proj.name,
            "environment": proj.environment,
            # 前端 OverviewView.vue 扁平字段
            "connections_total": len(conn_rows),
            "connections_healthy": healthy_conns,
            "pipelines_total": int(pipelines_count),
            "latest_versions_count": int(versions_count),
            "pending_preparations": pending_preparations,
            "pending_approvals": pending_approvals,
            "pending_commits": ready_to_commit,
            "active_batch_runs": batch_runs,
            "active_cdc_runs": cdc_runs,
            "success_rate": success_rate if runs else None,
            "reject_rate": avg_reject_rate if quality_reports else None,
            "budget_utilization_pct": 0.0 if runs else None,
            "recent_audit_events": evidence_items,
            # 后端兼容嵌套字段
            "connections": {
                "total": len(conn_rows),
                "healthy": healthy_conns,
            },
            "pipelines": {
                "total_definitions": int(pipelines_count),
                "frozen_versions": int(versions_count),
            },
            "governance_gates": {
                "total_preparations": len(preps),
                "pending_approvals": pending_approvals,
                "ready_for_operator_commit": ready_to_commit,
            },
            "executions": {
                "total_runs": len(runs),
                "batch_runs": batch_runs,
                "cdc_runs": cdc_runs,
                "success_rate": success_rate,
                "average_reject_rate": avg_reject_rate,
            },
            "recent_evidence_events": evidence_items,
        }

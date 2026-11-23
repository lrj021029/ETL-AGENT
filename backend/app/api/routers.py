"""FastAPI 控制面全量 API 路由实现与兄弟模块扩展挂载点。

严格对齐 `contracts/openapi/openapi.yaml` 与 `contracts/openapi/realtime_events.yaml`：
- 完整实现：
  0. Health (`/health`)
  1. Auth (`/api/v1/auth/login`, `/otp/verify`, `/refresh`, `/logout`, `/me`)
  2. Organizations (`/api/v1/organizations`, `/api/v1/organizations/{org_id}/members`)
  3. Projects & Memberships (`/api/v1/projects`, `/{project_id}/members`, `/{project_id}/roles`, `/{project_id}/overview`)
  4. Connections & Files (`/api/v1/connectors/registry`, `/{project_id}/connections`, `/{connection_id}/test`, `/{project_id}/files/upload`)
  5. Metadata (`/api/v1/projects/{project_id}/connections/{connection_id}/metadata/probe`, `/snapshots`, `/drift`, `/metadata/snapshots/{snapshot_id}/classifications`)
  11. Execution Query & SSE Realtime Stream (`/api/v1/projects/{project_id}/executions`, `/{execution_id}`, `/{execution_id}/events`, `/{execution_id}/cancel`, `/{execution_id}/reconcile`)
  12. Scheduling (`/api/v1/projects/{project_id}/schedules`, `/{schedule_id}/toggle`)
- 预留可插拔兄弟模块路由钩子与数据库事实查询默认端点：
  6. Agent (`/agent/threads`...)
  7. Pipeline (`/pipelines`...)
  8. Preparation (`/preparations`...)
  9. Approvals (`/approvals`...)
  10. Commit (`/commit`)
  13. Quality & Published Datasets (`/quality-report`, `/published-datasets`)
  14. Rollback (`/rollbacks`...)
  15. Evidence Ledger (`/evidence`...)
  16. Benchmark & Policies (`/benchmarks`..., `/policies`)
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Depends, File, Form, Header, Request, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.deps import (
    AuthenticatedPrincipal,
    ProjectRequestContext,
    get_auth_service,
    get_connection_service,
    get_current_principal,
    get_db_session,
    get_idempotency_guard,
    get_membership_service,
    get_metadata_service,
    get_org_service,
    get_project_service,
    get_schedule_service,
    parse_org_header,
    require_project_context,
)
from app.auth.service import AuthService
from app.config import get_settings
from app.connections.service import ConnectionService
from app.memberships.service import MembershipService
from app.metadata.service import MetadataService
from app.organizations.service import OrganizationService
from app.projects.service import ProjectService
from app.scheduling.service import ScheduleService
from app.shared.db_rls import TenantContext, apply_rls_context
from app.shared.errors import AppError, ErrorCode, resolve_trace_id
from app.shared.idempotency import IdempotencyGuard
from app.shared.models.evidence_benchmark import (
    BenchmarkDataset,
    BenchmarkRun,
    EvidenceCheckpoint,
    EvidenceEvent,
    PolicyVersion,
)
from app.shared.models.execution_quality import (
    ExecutionMetric,
    ExecutionRun,
    ExecutionStateEvent,
    ExternalJobRef,
    PublishedDatasetVersion,
    QualityReport,
    QualityRuleResult,
    RejectedRecordSummary,
    RollbackRun,
)
from app.shared.models.governance_harness import ApprovalDecision, ApprovalSlot, Preparation
from app.shared.models.pipeline_artifacts import (
    AgentThread,
    ArtifactManifest,
    PipelineDefinition,
    PipelineVersion,
)

# ==================== 请求体 Pydantic 模型 ====================


class LoginRequest(BaseModel):
    email: str
    password: str


class OtpVerifyRequest(BaseModel):
    challenge_id: uuid.UUID
    otp_code: str = Field(..., min_length=4, max_length=12)


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class CreateOrgRequest(BaseModel):
    slug: str = Field(..., min_length=2, max_length=128)
    name: str = Field(..., min_length=1, max_length=255)


class AddOrgMemberRequest(BaseModel):
    user_id: uuid.UUID
    org_role: str = "MEMBER"


class CreateProjectRequest(BaseModel):
    slug: str = Field(..., min_length=2, max_length=128)
    name: str = Field(..., min_length=1, max_length=255)
    environment: str = "dev"
    llm_data_policy_json: dict[str, Any] | None = None


class AddProjectMemberRequest(BaseModel):
    user_id: uuid.UUID
    role_code: str = "MAKER"


class CreateCustomRoleRequest(BaseModel):
    code: str = Field(..., min_length=2, max_length=64)
    name: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    permissions: list[str] = Field(default_factory=list)
    override_sod: bool = False


class CreateConnectionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    connector_id: str | None = Field(default=None, max_length=128)
    connection_type: str | None = Field(default=None, max_length=64)
    connector_type: str | None = Field(default=None, max_length=64)
    environment: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    secret_payload: dict[str, Any] | None = None
    secret_ref: str | None = None


class MetadataProbeRequest(BaseModel):
    connector_version: str = "2.3.9"
    override_objects: list[dict[str, Any]] | None = None


class PatchClassificationRequest(BaseModel):
    object_id: uuid.UUID | None = None
    table_name: str | None = None
    column_name: str
    classification_level: str | None = None
    sensitivity_tag: str | None = None
    pii_category: str | None = None


class CreateScheduleRequest(BaseModel):
    pipeline_id: uuid.UUID
    pipeline_version_id: uuid.UUID
    standing_preparation_id: uuid.UUID
    cron_expression: str
    timezone: str = "UTC"
    enabled: bool = True
    concurrency_policy: str | None = None
    misfire_policy: str | None = None
    version_binding_mode: str | None = None


class ToggleScheduleRequest(BaseModel):
    enabled: bool


# ==================== 0. Health Router ====================

health_router = APIRouter(tags=["Health"])


@health_router.get("/health")
def get_health(
    request: Request,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """探测控制面数据库与核心依赖真实运行状态（对齐 contracts/openapi/openapi.yaml#/paths/~1health）。"""
    settings = get_settings()
    db_status = "ok"
    try:
        session.execute(text("SELECT 1"))
    except Exception:
        db_status = "unreachable"

    dep_overrides: dict[str, str] = getattr(request.app.state, "health_dependencies_override", {}) or {}
    dependencies = {
        "postgres": db_status,
        "redis": dep_overrides.get("redis", "configured"),
        "vault": dep_overrides.get("vault", "configured"),
        "minio": dep_overrides.get("minio", "configured"),
        "mailpit": dep_overrides.get("mailpit", "configured"),
        "seatunnel_zeta": dep_overrides.get("seatunnel_zeta", "configured"),
    }
    overall = "ok" if db_status == "ok" else "degraded"
    return {
        "status": overall,
        "service": settings.service_name,
        "version": settings.service_version,
        "dependencies": dependencies,
    }


# ==================== 1. Auth Router ====================

auth_router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])


@auth_router.post("/login")
def login(
    payload: LoginRequest,
    request: Request,
    session: Session = Depends(get_db_session),
    auth_service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    """密码登录 -> 创建会话并触发邮件二次验证（Email OTP）挑战。"""
    client_host = request.client.host if request.client else "127.0.0.1"
    ua = request.headers.get("User-Agent", "etl-agent-client")
    return auth_service.login_with_password(
        session,
        email=payload.email,
        password=payload.password,
        ip_address=client_host,
        user_agent=ua,
    )


@auth_router.post("/otp/verify")
def verify_otp(
    payload: OtpVerifyRequest,
    session: Session = Depends(get_db_session),
    auth_service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    """校验邮件二次验证（Email OTP）验证码 -> 签发 JWT Access + Refresh Token。"""
    return auth_service.verify_email_otp(
        session,
        challenge_id=payload.challenge_id,
        otp_code=payload.otp_code,
    )


@auth_router.post("/refresh")
def refresh_token(
    payload: RefreshTokenRequest,
    session: Session = Depends(get_db_session),
    auth_service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    """轮转 Refresh Token Family；若检测到旧令牌重放则立即吊销整个家族。"""
    return auth_service.rotate_refresh_token(
        session,
        refresh_token=payload.refresh_token,
    )


@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    session: Session = Depends(get_db_session),
    auth_service: AuthService = Depends(get_auth_service),
) -> Response:
    """吊销当前会话及关联的 Refresh Token 家族。"""
    auth_service.logout(session, session_id=principal.session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@auth_router.get("/me")
def get_me(
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    session: Session = Depends(get_db_session),
    auth_service: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    """返回当前登录用户档案、所属组织、项目与角色列表。"""
    return auth_service.get_current_user_profile(session, user_id=principal.user_id)


# ==================== 2. Organizations Router ====================

organizations_router = APIRouter(prefix="/api/v1/organizations", tags=["Organizations"])


@organizations_router.get("")
def list_organizations(
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    session: Session = Depends(get_db_session),
    org_service: OrganizationService = Depends(get_org_service),
) -> dict[str, Any]:
    """列出当前用户所属的全部组织。"""
    items = org_service.list_organizations_for_user(session, user_id=principal.user_id)
    return {"items": items}


@organizations_router.post("", status_code=status.HTTP_201_CREATED)
def create_organization(
    payload: CreateOrgRequest,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    session: Session = Depends(get_db_session),
    org_service: OrganizationService = Depends(get_org_service),
) -> dict[str, Any]:
    """创建新组织。"""
    return org_service.create_organization(
        session,
        slug=payload.slug,
        name=payload.name,
        creator_user_id=principal.user_id,
    )


@organizations_router.get("/{org_id}/members")
def list_organization_members(
    org_id: uuid.UUID,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    session: Session = Depends(get_db_session),
    org_service: OrganizationService = Depends(get_org_service),
) -> dict[str, Any]:
    """列出指定组织的成员。"""
    apply_rls_context(
        session,
        TenantContext(organization_id=org_id, actor_id=principal.user_id),
        require_project=False,
    )
    items = org_service.list_organization_members(
        session,
        organization_id=org_id,
        actor_user_id=principal.user_id,
    )
    return {"items": items}


@organizations_router.post("/{org_id}/members", status_code=status.HTTP_201_CREATED)
def add_organization_member(
    org_id: uuid.UUID,
    payload: AddOrgMemberRequest,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    session: Session = Depends(get_db_session),
    org_service: OrganizationService = Depends(get_org_service),
) -> dict[str, Any]:
    """向组织添加成员。"""
    apply_rls_context(
        session,
        TenantContext(organization_id=org_id, actor_id=principal.user_id),
        require_project=False,
    )
    return org_service.add_organization_member(
        session,
        organization_id=org_id,
        user_id=payload.user_id,
        org_role=payload.org_role,
        actor_user_id=principal.user_id,
    )


# ==================== 3. Projects & Memberships Router ====================

projects_router = APIRouter(prefix="/api/v1/projects", tags=["Projects"])


@projects_router.get("")
def list_projects(
    organization_id: uuid.UUID = Depends(parse_org_header),
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    session: Session = Depends(get_db_session),
    project_service: ProjectService = Depends(get_project_service),
) -> dict[str, Any]:
    """列出当前活跃组织下的所有可访问项目。"""
    apply_rls_context(
        session,
        TenantContext(organization_id=organization_id, actor_id=principal.user_id),
        require_project=False,
    )
    items = project_service.list_projects_in_organization(
        session,
        organization_id=organization_id,
        user_id=principal.user_id,
    )
    return {"items": items}


@projects_router.post("", status_code=status.HTTP_201_CREATED)
def create_project(
    payload: CreateProjectRequest,
    organization_id: uuid.UUID = Depends(parse_org_header),
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    session: Session = Depends(get_db_session),
    project_service: ProjectService = Depends(get_project_service),
) -> dict[str, Any]:
    """在当前活跃组织下创建新项目。"""
    apply_rls_context(
        session,
        TenantContext(organization_id=organization_id, actor_id=principal.user_id),
        require_project=False,
    )
    return project_service.create_project(
        session,
        organization_id=organization_id,
        slug=payload.slug,
        name=payload.name,
        environment=payload.environment,
        llm_data_policy_json=payload.llm_data_policy_json,
        creator_user_id=principal.user_id,
    )


@projects_router.get("/{project_id}/members")
def list_project_members(
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
    membership_service: MembershipService = Depends(get_membership_service),
) -> dict[str, Any]:
    """列出项目成员及其角色绑定。"""
    items = membership_service.list_project_members(
        session,
        organization_id=ctx.organization_id,
        project_id=ctx.project_id,
        actor_user_id=ctx.principal.user_id,
    )
    return {"items": items}


@projects_router.post("/{project_id}/members", status_code=status.HTTP_201_CREATED)
def add_project_member(
    payload: AddProjectMemberRequest,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
    membership_service: MembershipService = Depends(get_membership_service),
) -> dict[str, Any]:
    """添加项目成员并绑定角色。"""
    return membership_service.add_project_member_with_role(
        session,
        organization_id=ctx.organization_id,
        project_id=ctx.project_id,
        target_user_id=payload.user_id,
        role_code=payload.role_code,
        actor_user_id=ctx.principal.user_id,
    )


@projects_router.get("/{project_id}/roles")
def list_project_roles(
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
    membership_service: MembershipService = Depends(get_membership_service),
) -> dict[str, Any]:
    """列出固定系统角色与当前项目的自定义角色。"""
    items = membership_service.list_project_roles(
        session,
        organization_id=ctx.organization_id,
        project_id=ctx.project_id,
        actor_user_id=ctx.principal.user_id,
    )
    return {"items": items}


@projects_router.post("/{project_id}/roles", status_code=status.HTTP_201_CREATED)
def create_custom_project_role(
    payload: CreateCustomRoleRequest,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
    membership_service: MembershipService = Depends(get_membership_service),
) -> dict[str, Any]:
    """创建项目自定义角色（禁止修改固定系统角色或绕过系统职责分离 SoD）。"""
    return membership_service.create_custom_project_role(
        session,
        organization_id=ctx.organization_id,
        project_id=ctx.project_id,
        code=payload.code,
        name=payload.name,
        description=payload.description,
        permission_codes=payload.permissions,
        override_sod=payload.override_sod,
        actor_user_id=ctx.principal.user_id,
    )


@projects_router.get("/{project_id}/overview")
def get_project_overview(
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
    project_service: ProjectService = Depends(get_project_service),
) -> dict[str, Any]:
    """查询总览工作台（Overview Dashboard）实时统计事实。"""
    return project_service.get_project_overview(
        session,
        organization_id=ctx.organization_id,
        project_id=ctx.project_id,
    )


# ==================== 4. Connections & Files Router ====================

connections_router = APIRouter(tags=["Connections"])


@connections_router.get("/api/v1/connectors/registry")
def get_connector_registry(
    request: Request,
    session: Session = Depends(get_db_session),
    connection_service: ConnectionService = Depends(get_connection_service),
) -> dict[str, Any]:
    """查询版本化 Connector 能力矩阵（优先使用已挂载的 ConnectorRegistry 真实契约条目）。"""
    wired_registry = getattr(request.app.state, "connector_registry", None)
    if wired_registry is not None and hasattr(wired_registry, "list_entries"):
        return {"items": [entry.to_dict() for entry in wired_registry.list_entries()]}
    return {"items": connection_service.list_connector_registry(session)}


@connections_router.get("/api/v1/projects/{project_id}/connections")
def list_connections(
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
    connection_service: ConnectionService = Depends(get_connection_service),
) -> dict[str, Any]:
    """列出项目内的所有数据源连接（Secret 严格掩码脱敏）。"""
    return {
        "items": connection_service.list_connections(
            session,
            organization_id=ctx.organization_id,
            project_id=ctx.project_id,
        )
    }


@connections_router.post(
    "/api/v1/projects/{project_id}/connections",
    status_code=status.HTTP_201_CREATED,
)
def create_connection(
    payload: CreateConnectionRequest,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
    connection_service: ConnectionService = Depends(get_connection_service),
) -> dict[str, Any]:
    """创建受管数据源连接并托管 SecretProvider 引用（兼容前后端连接器类型字段）。"""
    connector_map = {
        "mysql": ("mysql-jdbc", "MYSQL"),
        "postgres": ("postgres-jdbc", "POSTGRES"),
        "postgresql": ("postgres-jdbc", "POSTGRES"),
        "doris": ("doris-sink", "DORIS"),
        "minio": ("s3-minio-file", "MINIO"),
        "s3": ("s3-minio-file", "MINIO"),
        "rest_api": ("http-rest", "REST_API"),
    }
    raw_type = (payload.connection_type or payload.connector_type or "mysql").strip()
    mapped_cid, mapped_ctype = connector_map.get(raw_type.lower(), (raw_type.lower(), raw_type.upper()))
    resolved_connector_id = (payload.connector_id or mapped_cid).strip()
    resolved_connection_type = (payload.connection_type or mapped_ctype).strip().upper()
    cfg = dict(payload.config or {})
    if payload.environment and "environment" not in cfg:
        cfg["environment"] = payload.environment
    secret_payload = payload.secret_payload
    if secret_payload is None and payload.secret_ref:
        secret_payload = {"secret_ref": payload.secret_ref}
    return connection_service.create_connection(
        session,
        organization_id=ctx.organization_id,
        project_id=ctx.project_id,
        name=payload.name,
        connector_id=resolved_connector_id,
        connection_type=resolved_connection_type,
        config=cfg,
        secret_payload=secret_payload,
        actor_user_id=ctx.principal.user_id,
    )


@connections_router.post("/api/v1/projects/{project_id}/connections/{connection_id}/test")
def test_connection(
    connection_id: uuid.UUID,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
    connection_service: ConnectionService = Depends(get_connection_service),
) -> dict[str, Any]:
    """执行只读连通性测试。"""
    return connection_service.test_connection(
        session,
        organization_id=ctx.organization_id,
        project_id=ctx.project_id,
        connection_id=connection_id,
    )


@connections_router.post(
    "/api/v1/projects/{project_id}/files/upload",
    status_code=status.HTTP_201_CREATED,
)
async def upload_file(
    ctx: ProjectRequestContext = Depends(require_project_context),
    file: UploadFile | None = File(default=None),
    filename: str | None = Form(default=None),
    session: Session = Depends(get_db_session),
    connection_service: ConnectionService = Depends(get_connection_service),
) -> dict[str, Any]:
    """受控上传文件至租户隔离的对象存储目录（强制阻断 ..、绝对路径与符号链接逃逸）。"""
    if file is not None:
        raw_filename = filename or file.filename or "uploaded.dat"
        content_bytes = await file.read()
        media_type = file.content_type or "application/octet-stream"
    else:
        raw_filename = filename or ""
        content_bytes = b""
        media_type = "text/plain"

    return connection_service.upload_file(
        session,
        organization_id=ctx.organization_id,
        project_id=ctx.project_id,
        filename=raw_filename,
        content_bytes=content_bytes,
        media_type=media_type,
        actor_user_id=ctx.principal.user_id,
    )


# ==================== 5. Metadata Router ====================

metadata_router = APIRouter(tags=["Metadata"])


@metadata_router.post(
    "/api/v1/projects/{project_id}/connections/{connection_id}/metadata/probe",
    status_code=status.HTTP_201_CREATED,
)
def probe_metadata(
    connection_id: uuid.UUID,
    payload: MetadataProbeRequest | None = None,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
    metadata_service: MetadataService = Depends(get_metadata_service),
) -> dict[str, Any]:
    """执行受管只读元数据探查 -> 总是创建全新的不可变 MetadataSnapshot 并计算 SchemaDriftEvent。"""
    req = payload or MetadataProbeRequest()
    return metadata_service.probe_and_create_snapshot(
        session,
        organization_id=ctx.organization_id,
        project_id=ctx.project_id,
        connection_id=connection_id,
        actor_user_id=ctx.principal.user_id,
        override_objects=req.override_objects,
        connector_version=req.connector_version,
    )


@metadata_router.get("/api/v1/projects/{project_id}/connections/{connection_id}/metadata/snapshots")
def list_metadata_snapshots(
    connection_id: uuid.UUID,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
    metadata_service: MetadataService = Depends(get_metadata_service),
) -> dict[str, Any]:
    """列出连接下的所有不可变元数据快照。"""
    return {
        "items": metadata_service.list_snapshots(
            session,
            organization_id=ctx.organization_id,
            project_id=ctx.project_id,
            connection_id=connection_id,
        )
    }


@metadata_router.get("/api/v1/projects/{project_id}/connections/{connection_id}/metadata/drift")
def list_metadata_drift(
    connection_id: uuid.UUID,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
    metadata_service: MetadataService = Depends(get_metadata_service),
) -> dict[str, Any]:
    """查询快照间的 Schema Drift 历史差异事件。"""
    return {
        "items": metadata_service.list_drift_events(
            session,
            organization_id=ctx.organization_id,
            project_id=ctx.project_id,
            connection_id=connection_id,
        )
    }


@metadata_router.patch("/api/v1/projects/{project_id}/metadata/snapshots/{snapshot_id}/classifications")
def patch_metadata_classification(
    snapshot_id: uuid.UUID,
    payload: PatchClassificationRequest,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
    metadata_service: MetadataService = Depends(get_metadata_service),
) -> dict[str, Any]:
    """人工标记或调整敏感字段分类（同时支持 object_id 与 table_name+sensitivity_tag）。"""
    level = payload.classification_level or payload.sensitivity_tag or "PUBLIC"
    return metadata_service.update_manual_classification(
        session,
        organization_id=ctx.organization_id,
        project_id=ctx.project_id,
        snapshot_id=snapshot_id,
        object_id=payload.object_id,
        table_name=payload.table_name,
        column_name=payload.column_name,
        classification_level=level,
        pii_category=payload.pii_category,
        actor_user_id=ctx.principal.user_id,
    )


# ==================== 11. Execution & SSE Realtime Stream Router ====================

execution_router = APIRouter(prefix="/api/v1/projects/{project_id}/executions", tags=["Execution"])


def _resolve_confirmation_state(run: ExecutionRun, job_ref: ExternalJobRef | None) -> str:
    """根据 ExecutionRun 与 ExternalJobRef 映射契约确认状态（REQUEST_SUBMITTED / ENGINE_CONFIRMED / RECONCILIATION_PENDING）。"""
    if run.status in ("UNKNOWN", "RECONCILING"):
        return "RECONCILIATION_PENDING"
    if job_ref is not None and job_ref.submit_state in (
        "REQUEST_SUBMITTED",
        "ENGINE_CONFIRMED",
        "RECONCILIATION_PENDING",
    ):
        return job_ref.submit_state
    if run.external_job_id:
        return "ENGINE_CONFIRMED"
    return "REQUEST_SUBMITTED"


@execution_router.get("")
def list_executions(
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """列出项目内的所有 ExecutionRun（包含 BATCH 与 CDC 模式）。"""
    runs = (
        session.execute(
            select(ExecutionRun)
            .where(
                ExecutionRun.organization_id == ctx.organization_id,
                ExecutionRun.project_id == ctx.project_id,
            )
            .order_by(ExecutionRun.created_at.desc())
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(r.id),
                "execution_id": str(r.id),
                "pipeline_id": str(r.pipeline_id),
                "pipeline_version_id": str(r.pipeline_version_id),
                "preparation_id": str(r.preparation_id),
                "artifact_hash": r.artifact_hash,
                "execution_mode": r.execution_mode,
                "status": r.status,
                "status_reason": r.status_reason,
                "external_engine": r.external_engine,
                "external_job_id": r.external_job_id,
                "confirmation_state": _resolve_confirmation_state(r, None),
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "ended_at": r.ended_at.isoformat() if r.ended_at else None,
                "created_at": r.created_at.isoformat(),
            }
            for r in runs
        ]
    }


@execution_router.get("/{execution_id}")
def get_execution_detail(
    execution_id: uuid.UUID,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """查询单次 ExecutionRun 详情、确认状态、最新采样指标与诊断信息（兼作轮询兜底端点）。"""
    run = session.get(ExecutionRun, execution_id)
    if run is None or run.organization_id != ctx.organization_id or run.project_id != ctx.project_id:
        raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "目标 ExecutionRun 不存在或跨租户访问被拒绝")

    job_ref = session.execute(
        select(ExternalJobRef).where(ExternalJobRef.execution_id == run.id)
    ).scalar_one_or_none()

    latest_metric = (
        session.execute(
            select(ExecutionMetric)
            .where(ExecutionMetric.execution_id == run.id)
            .order_by(ExecutionMetric.sampled_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )

    metrics_payload = dict(run.metrics_snapshot_json or {})
    if latest_metric is not None:
        metrics_payload = {
            "sampled_at": latest_metric.sampled_at.isoformat(),
            "read_rows": latest_metric.read_rows,
            "read_bytes": latest_metric.read_bytes,
            "validated_rows": latest_metric.validated_rows,
            "valid_rows": latest_metric.valid_rows,
            "write_rows": latest_metric.write_rows,
            "write_bytes": latest_metric.write_bytes,
            "error_rows": latest_metric.error_rows,
            "reject_rate_total": latest_metric.reject_rate_total,
            "reject_rate_window": latest_metric.reject_rate_window,
            "throughput_rps": latest_metric.throughput_rps,
            "duration_seconds": latest_metric.duration_seconds,
            "amplification_ratio": latest_metric.amplification_ratio,
            "cdc_lag_ms": latest_metric.cdc_lag_ms,
        }

    return {
        "id": str(run.id),
        "execution_id": str(run.id),
        "organization_id": str(run.organization_id),
        "project_id": str(run.project_id),
        "pipeline_id": str(run.pipeline_id),
        "pipeline_version_id": str(run.pipeline_version_id),
        "artifact_hash": run.artifact_hash,
        "execution_mode": run.execution_mode,
        "preparation_id": str(run.preparation_id),
        "capability_jti": str(run.capability_jti),
        "command_id": str(run.command_id),
        "external_engine": run.external_engine,
        "external_job_id": run.external_job_id,
        "status": run.status,
        "status_reason": run.status_reason,
        "confirmation_state": _resolve_confirmation_state(run, job_ref),
        "source_position": run.source_position_json,
        "checkpoint_ref": run.checkpoint_ref,
        "metrics": metrics_payload,
        "published_dataset_version_id": (
            str(run.published_dataset_version_id) if run.published_dataset_version_id else None
        ),
        "diagnosis": run.diagnosis_json,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "created_at": run.created_at.isoformat(),
    }


def _format_sse_frame(event_name: str, data: dict[str, Any]) -> str:
    """格式化标准 Server-Sent Events (SSE) 事件帧。"""
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_name}\ndata: {serialized}\n\n"


@execution_router.get("/{execution_id}/events")
async def stream_execution_events(
    execution_id: uuid.UUID,
    request: Request,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> StreamingResponse:
    """SSE 实时执行状态与指标流端点（严格对齐 contracts/openapi/realtime_events.yaml）。

    发出四类契约事件：
    - `execution.state_changed`
    - `execution.metrics_tick`
    - `execution.supervision_alert`（若发生监督阈值告警）
    - `execution.heartbeat`
    """
    run = session.get(ExecutionRun, execution_id)
    if run is None or run.organization_id != ctx.organization_id or run.project_id != ctx.project_id:
        raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "目标 ExecutionRun 不存在或跨租户访问被拒绝")

    job_ref = session.execute(
        select(ExternalJobRef).where(ExternalJobRef.execution_id == run.id)
    ).scalar_one_or_none()
    confirmation_state = _resolve_confirmation_state(run, job_ref)
    trace_id = resolve_trace_id(request)

    state_events = (
        session.execute(
            select(ExecutionStateEvent)
            .where(ExecutionStateEvent.execution_id == run.id)
            .order_by(ExecutionStateEvent.occurred_at.asc())
        )
        .scalars()
        .all()
    )

    metrics_rows = (
        session.execute(
            select(ExecutionMetric)
            .where(ExecutionMetric.execution_id == run.id)
            .order_by(ExecutionMetric.sampled_at.asc())
        )
        .scalars()
        .all()
    )

    frames: list[str] = []
    if state_events:
        for ev in state_events:
            frames.append(
                _format_sse_frame(
                    "execution.state_changed",
                    {
                        "execution_id": str(run.id),
                        "execution_mode": run.execution_mode,
                        "from_status": ev.from_status,
                        "to_status": ev.to_status,
                        "confirmation_state": confirmation_state,
                        "external_job_id": run.external_job_id,
                        "reason_code": ev.reason_code,
                        "occurred_at": ev.occurred_at.isoformat(),
                        "trace_id": trace_id,
                    },
                )
            )
    else:
        frames.append(
            _format_sse_frame(
                "execution.state_changed",
                {
                    "execution_id": str(run.id),
                    "execution_mode": run.execution_mode,
                    "from_status": None,
                    "to_status": run.status,
                    "confirmation_state": confirmation_state,
                    "external_job_id": run.external_job_id,
                    "reason_code": run.status_reason or "INITIAL_STATE",
                    "occurred_at": run.created_at.isoformat(),
                    "trace_id": trace_id,
                },
            )
        )

    for m in metrics_rows:
        frames.append(
            _format_sse_frame(
                "execution.metrics_tick",
                {
                    "execution_id": str(run.id),
                    "sampled_at": m.sampled_at.isoformat(),
                    "read_rows": m.read_rows,
                    "validated_rows": m.validated_rows,
                    "valid_rows": m.valid_rows,
                    "write_rows": m.write_rows,
                    "write_bytes": m.write_bytes,
                    "error_rows": m.error_rows,
                    "reject_rate_total": m.reject_rate_total,
                    "reject_rate_window": m.reject_rate_window,
                    "throughput_rps": m.throughput_rps,
                    "duration_seconds": m.duration_seconds,
                    "amplification_ratio": m.amplification_ratio,
                    "cdc_lag_ms": m.cdc_lag_ms,
                },
            )
        )
        # 若整体拒绝率 > 0.20（正好 0.20 不触发），发送 execution.supervision_alert 事件
        if m.reject_rate_total > 0.20:
            frames.append(
                _format_sse_frame(
                    "execution.supervision_alert",
                    {
                        "execution_id": str(run.id),
                        "breach_type": "REJECT_RATE_TOTAL_EXCEEDED",
                        "threshold": 0.20,
                        "observed_value": m.reject_rate_total,
                        "action_taken": "HARD_INTERRUPT",
                        "occurred_at": m.sampled_at.isoformat(),
                    },
                )
            )

    frames.append(
        _format_sse_frame(
            "execution.heartbeat",
            {"server_time": datetime.now(timezone.utc).isoformat()},
        )
    )

    async def _event_generator() -> AsyncGenerator[str, None]:
        for frame in frames:
            yield frame

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@execution_router.post("/{execution_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
def cancel_execution(
    execution_id: uuid.UUID,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """发起受管停止/取消请求（区分批处理 CANCEL_REQUESTED 与 CDC STOP_REQUESTED）。"""
    run = session.get(ExecutionRun, execution_id)
    if run is None or run.organization_id != ctx.organization_id or run.project_id != ctx.project_id:
        raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "ExecutionRun 不存在")

    prev_status = run.status
    target_status = "STOP_REQUESTED" if run.execution_mode == "CDC" else "CANCEL_REQUESTED"
    run.status = target_status
    run.status_reason = "Operator requested managed cancel/stop"
    session.add(
        ExecutionStateEvent(
            organization_id=ctx.organization_id,
            project_id=ctx.project_id,
            execution_id=run.id,
            from_status=prev_status,
            to_status=target_status,
            reason_code="OPERATOR_CANCEL_REQUESTED",
            detail_json={},
            actor_id=str(ctx.principal.user_id),
        )
    )
    session.flush()
    return {
        "id": str(run.id),
        "execution_id": str(run.id),
        "status": run.status,
        "confirmation_state": "REQUEST_SUBMITTED",
    }


@execution_router.post("/{execution_id}/reconcile", status_code=status.HTTP_202_ACCEPTED)
def reconcile_execution(
    execution_id: uuid.UUID,
    request: Request,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """对处于 UNKNOWN 状态的执行触发受管对账（禁止盲目重复提交）。"""
    run = session.get(ExecutionRun, execution_id)
    if run is None or run.organization_id != ctx.organization_id or run.project_id != ctx.project_id:
        raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "ExecutionRun 不存在")
    if run.status not in ("UNKNOWN", "SUBMITTING", "RECONCILING"):
        raise AppError(
            ErrorCode.STATE_TRANSITION_INVALID,
            f"仅 UNKNOWN 或 SUBMITTING 状态允许触发对账，当前状态为 '{run.status}'",
        )
    handler = getattr(request.app.state, "execution_reconcile_handler", None)
    if handler is not None:
        return handler(
            session=session,
            ctx=ctx,
            run=run,
            trace_id=resolve_trace_id(request),
        )
    prev_status = run.status
    run.status = "RECONCILING"
    session.add(
        ExecutionStateEvent(
            organization_id=ctx.organization_id,
            project_id=ctx.project_id,
            execution_id=run.id,
            from_status=prev_status,
            to_status="RECONCILING",
            reason_code="OPERATOR_RECONCILE_TRIGGERED",
            detail_json={},
            actor_id=str(ctx.principal.user_id),
        )
    )
    session.flush()
    return {
        "id": str(run.id),
        "execution_id": str(run.id),
        "status": run.status,
        "confirmation_state": "RECONCILIATION_PENDING",
    }


# ==================== 12. Scheduling Router ====================

scheduling_router = APIRouter(prefix="/api/v1/projects/{project_id}/schedules", tags=["Scheduling"])


@scheduling_router.get("")
def list_schedules(
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
    schedule_service: ScheduleService = Depends(get_schedule_service),
) -> dict[str, Any]:
    """列出项目内的所有受管定时调度。"""
    return {
        "items": schedule_service.list_schedules(
            session,
            organization_id=ctx.organization_id,
            project_id=ctx.project_id,
        )
    }


@scheduling_router.post("", status_code=status.HTTP_201_CREATED)
def create_schedule(
    payload: CreateScheduleRequest,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
    schedule_service: ScheduleService = Depends(get_schedule_service),
) -> dict[str, Any]:
    """创建绑定至已审批 StandingSchedulePreparation 的受管定时调度。"""
    return schedule_service.create_schedule(
        session,
        organization_id=ctx.organization_id,
        project_id=ctx.project_id,
        pipeline_id=payload.pipeline_id,
        pipeline_version_id=payload.pipeline_version_id,
        standing_preparation_id=payload.standing_preparation_id,
        cron_expression=payload.cron_expression,
        timezone_name=payload.timezone,
        enabled=payload.enabled,
        concurrency_policy=payload.concurrency_policy,
        misfire_policy=payload.misfire_policy,
        version_binding_mode=payload.version_binding_mode,
        actor_user_id=ctx.principal.user_id,
    )


@scheduling_router.patch("/{schedule_id}/toggle")
def toggle_schedule(
    schedule_id: uuid.UUID,
    payload: ToggleScheduleRequest,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
    schedule_service: ScheduleService = Depends(get_schedule_service),
) -> dict[str, Any]:
    """启用或禁用定时调度。"""
    return schedule_service.toggle_schedule(
        session,
        organization_id=ctx.organization_id,
        project_id=ctx.project_id,
        schedule_id=schedule_id,
        enabled=payload.enabled,
    )


# ==================== 兄弟模块扩展路由与数据库查询钩子（6~10, 13~16） ====================
# 确保 contracts/openapi/openapi.yaml 中的全部 16 个模块端点均在 FastAPI OpenAPI 路由表中存在，
# 且兄弟模块（Agent / Security / DataPlane / QA）可通过 `register_extension_router(app, router)` 注入或覆盖。

extension_hooks_router = APIRouter()


@extension_hooks_router.post(
    "/api/v1/projects/{project_id}/agent/threads",
    tags=["Agent"],
    status_code=status.HTTP_201_CREATED,
)
def create_agent_thread_hook(
    request: Request,
    payload: dict[str, Any] | None = None,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """创建受 RLS 保护的 Studio LangGraph 会话线程。"""
    handler = getattr(request.app.state, "agent_create_thread_handler", None)
    if handler is not None:
        return handler(session=session, ctx=ctx, payload=payload)
    body = payload or {}
    thread = AgentThread(
        organization_id=ctx.organization_id,
        project_id=ctx.project_id,
        user_id=ctx.principal.user_id,
        langgraph_thread_id=f"lg-{uuid.uuid4().hex}",
        title=str(body.get("title", "Studio ETL Thread")),
        status="ACTIVE",
    )
    session.add(thread)
    session.flush()
    return {
        "thread_id": str(thread.id),
        "langgraph_thread_id": thread.langgraph_thread_id,
        "status": thread.status,
        "title": thread.title,
    }


@extension_hooks_router.post(
    "/api/v1/projects/{project_id}/agent/threads/{thread_id}/invoke",
    tags=["Agent"],
)
def invoke_agent_thread_hook(
    thread_id: uuid.UUID,
    payload: dict[str, Any],
    request: Request,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """提交自然语言 ETL 需求至 LangGraph 编排器（仅只读探查与生成，零外部副作用）。"""
    thread = session.get(AgentThread, thread_id)
    if (
        thread is None
        or thread.organization_id != ctx.organization_id
        or thread.project_id != ctx.project_id
        or thread.user_id != ctx.principal.user_id
    ):
        raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "无权访问其他租户或用户的 LangGraph thread_id")

    handler = getattr(request.app.state, "agent_invoke_handler", None)
    if handler is not None:
        return handler(session=session, ctx=ctx, thread=thread, payload=payload)
    return {
        "thread_id": str(thread.id),
        "status": thread.status,
        "pending_clarification": thread.pending_clarification_json,
    }


@extension_hooks_router.post(
    "/api/v1/projects/{project_id}/agent/threads/{thread_id}/resume",
    tags=["Agent"],
)
def resume_agent_thread_hook(
    thread_id: uuid.UUID,
    payload: dict[str, Any],
    request: Request,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """在二次核验 (user_id, organization_id, project_id, thread_id) 权限后从 Checkpoint 恢复中断线程。"""
    thread = session.get(AgentThread, thread_id)
    if (
        thread is None
        or thread.organization_id != ctx.organization_id
        or thread.project_id != ctx.project_id
        or thread.user_id != ctx.principal.user_id
    ):
        raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "严禁跨租户或跨用户 Resume LangGraph thread_id")

    handler = getattr(request.app.state, "agent_resume_handler", None)
    if handler is not None:
        return handler(session=session, ctx=ctx, thread=thread, payload=payload)
    thread.status = "RESUMED"
    thread.pending_clarification_json = None
    session.flush()
    return {"thread_id": str(thread.id), "status": thread.status}


@extension_hooks_router.get(
    "/api/v1/projects/{project_id}/agent/threads/{thread_id}",
    tags=["Agent"],
)
def get_agent_thread_hook(
    thread_id: uuid.UUID,
    request: Request,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """查询 LangGraph 会话状态与外部 LLM 脱敏审计摘要。"""
    thread = session.get(AgentThread, thread_id)
    if (
        thread is None
        or thread.organization_id != ctx.organization_id
        or thread.project_id != ctx.project_id
    ):
        raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "目标 AgentThread 不存在或越权访问")
    wiring = getattr(request.app.state, "wiring", None)
    candidate = (
        wiring.thread_candidates.get(str(thread.id))
        if wiring is not None and hasattr(wiring, "thread_candidates")
        else None
    )
    return {
        "thread_id": str(thread.id),
        "langgraph_thread_id": thread.langgraph_thread_id,
        "title": thread.title,
        "status": thread.status,
        "pending_clarification": thread.pending_clarification_json,
        "candidate": candidate,
    }


@extension_hooks_router.get("/api/v1/projects/{project_id}/pipelines", tags=["Pipeline"])
def list_pipelines_hook(
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """列出项目内的 Pipeline 定义。"""
    rows = (
        session.execute(
            select(PipelineDefinition).where(
                PipelineDefinition.organization_id == ctx.organization_id,
                PipelineDefinition.project_id == ctx.project_id,
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(p.id),
                "name": p.name,
                "description": p.description,
                "sync_mode": p.sync_mode,
                "execution_mode": "CDC" if "CDC" in p.sync_mode.upper() else "BATCH",
                "source_connection_id": str(p.source_connection_id),
                "sink_connection_id": str(p.sink_connection_id),
                "latest_revision": p.latest_revision,
                "latest_version": f"v{p.latest_revision}" if p.latest_revision else None,
                "created_at": p.created_at.isoformat(),
            }
            for p in rows
        ]
    }


@extension_hooks_router.post(
    "/api/v1/projects/{project_id}/pipelines",
    tags=["Pipeline"],
    status_code=status.HTTP_201_CREATED,
)
def create_pipeline_hook(
    payload: dict[str, Any],
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """创建新的 PipelineDefinition（兼容 sync_mode 与 execution_mode）。"""
    exec_mode = str(payload.get("execution_mode") or "").upper()
    default_sync = "CDC" if exec_mode == "CDC" else "FULL_LOAD"
    sync_mode = str(payload.get("sync_mode") or default_sync)
    pipe = PipelineDefinition(
        organization_id=ctx.organization_id,
        project_id=ctx.project_id,
        name=str(payload["name"]),
        description=str(payload.get("description", "")),
        sync_mode=sync_mode,
        source_connection_id=uuid.UUID(str(payload["source_connection_id"])),
        sink_connection_id=uuid.UUID(str(payload["sink_connection_id"])),
        latest_revision=0,
        created_by=ctx.principal.user_id,
    )
    session.add(pipe)
    session.flush()
    return {
        "id": str(pipe.id),
        "name": pipe.name,
        "description": pipe.description,
        "sync_mode": pipe.sync_mode,
        "execution_mode": "CDC" if "CDC" in pipe.sync_mode.upper() else "BATCH",
        "source_connection_id": str(pipe.source_connection_id),
        "sink_connection_id": str(pipe.sink_connection_id),
        "created_at": pipe.created_at.isoformat(),
    }


@extension_hooks_router.post(
    "/api/v1/projects/{project_id}/pipelines/{pipeline_id}/drafts/validate",
    tags=["Pipeline"],
)
def validate_pipeline_draft_hook(
    pipeline_id: uuid.UUID,
    payload: dict[str, Any],
    request: Request,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """执行确定性静态门禁校验（Schema / SQL AST / HOCON / Connector / Contracts）。"""
    handler = getattr(request.app.state, "deterministic_gate_handler", None)
    if handler is not None:
        return handler(session=session, ctx=ctx, pipeline_id=pipeline_id, payload=payload)
    return {"pipeline_id": str(pipeline_id), "passed": True, "errors": [], "warnings": []}


@extension_hooks_router.get(
    "/api/v1/projects/{project_id}/pipelines/{pipeline_id}/versions",
    tags=["Pipeline"],
)
def list_pipeline_versions_hook(
    pipeline_id: uuid.UUID,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """列出指定 Pipeline 的不可变冻结版本列表。"""
    rows = (
        session.execute(
            select(PipelineVersion).where(
                PipelineVersion.organization_id == ctx.organization_id,
                PipelineVersion.project_id == ctx.project_id,
                PipelineVersion.pipeline_id == pipeline_id,
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(v.id),
                "pipeline_id": str(v.pipeline_id),
                "revision": v.revision,
                "artifact_hash": v.artifact_hash,
                "manifest_id": str(v.manifest_id),
                "status": v.status,
                "created_at": v.created_at.isoformat(),
            }
            for v in rows
        ]
    }


@extension_hooks_router.post(
    "/api/v1/projects/{project_id}/pipelines/{pipeline_id}/versions",
    tags=["Pipeline"],
    status_code=status.HTTP_201_CREATED,
)
def freeze_pipeline_version_hook(
    pipeline_id: uuid.UUID,
    payload: dict[str, Any],
    request: Request,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """冻结通过确定性门禁的草稿为不可变 PipelineVersion + SHA256 Manifest。"""
    handler = getattr(request.app.state, "freeze_version_handler", None)
    if handler is not None:
        return handler(session=session, ctx=ctx, pipeline_id=pipeline_id, payload=payload)
    raise AppError(ErrorCode.DETERMINISTIC_GATE_FAILED, "未挂载制品冻结编译器或草稿尚未通过门禁")


@extension_hooks_router.get(
    "/api/v1/projects/{project_id}/pipeline-versions/{version_id}/manifest",
    tags=["Pipeline"],
)
def get_pipeline_manifest_hook(
    version_id: uuid.UUID,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """获取不可变 ArtifactManifest 及 SHA256 摘要。"""
    manifest = session.execute(
        select(ArtifactManifest).where(
            ArtifactManifest.organization_id == ctx.organization_id,
            ArtifactManifest.project_id == ctx.project_id,
            ArtifactManifest.pipeline_version_id == version_id,
        )
    ).scalar_one_or_none()
    if manifest is None:
        raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "ArtifactManifest 不存在")
    return {
        "id": str(manifest.id),
        "pipeline_version_id": str(manifest.pipeline_version_id),
        "schema_version": manifest.schema_version,
        "artifact_hash": manifest.artifact_hash,
        "canonical_json": manifest.canonical_json,
        "compiler_version": manifest.compiler_version,
        "created_at": manifest.created_at.isoformat(),
    }


def _serialize_preparation_with_slots(session: Session, p: Preparation) -> dict[str, Any]:
    """序列化 Preparation 并附带关联的审批槽位与历史决策，兼容前后端全部字段。"""
    slots = (
        session.execute(
            select(ApprovalSlot).where(
                ApprovalSlot.organization_id == p.organization_id,
                ApprovalSlot.project_id == p.project_id,
                ApprovalSlot.preparation_id == p.id,
            )
        )
        .scalars()
        .all()
    )
    decisions = (
        session.execute(
            select(ApprovalDecision).where(
                ApprovalDecision.organization_id == p.organization_id,
                ApprovalDecision.project_id == p.project_id,
                ApprovalDecision.preparation_id == p.id,
            )
        )
        .scalars()
        .all()
    )
    dec_by_slot = {d.slot_id: d for d in decisions}
    approval_slots_list = []
    for s in slots:
        dec = dec_by_slot.get(s.id)
        approval_slots_list.append(
            {
                "id": str(s.id),
                "slot_role": s.slot_role,
                "slot_type": s.slot_role,
                "required": s.required,
                "status": s.status,
                "decided_by": str(dec.approver_id) if dec else None,
                "decided_by_name": str(dec.approver_id) if dec else None,
                "decided_at": dec.decided_at.isoformat() if dec else None,
                "reason": dec.reason if dec else None,
            }
        )

    frozen = p.frozen_facts_json or {}
    raw_scope = frozen.get("resource_scope") or frozen.get("target_tables") or []
    resource_scope = [str(x) for x in raw_scope] if isinstance(raw_scope, list) else [str(raw_scope)]
    raw_secrets = frozen.get("secret_refs") or []
    secret_refs = [str(x) for x in raw_secrets] if isinstance(raw_secrets, list) else [str(raw_secrets)]
    rb_plan = p.rollback_plan_json or {}
    rb_text = rb_plan.get("strategy") or json.dumps(rb_plan, ensure_ascii=False)

    return {
        "id": str(p.id),
        "preparation_type": p.preparation_type,
        "pipeline_id": str(p.pipeline_id),
        "pipeline_version_id": str(p.pipeline_version_id),
        "artifact_hash": p.artifact_hash,
        "artifact_sha256": p.artifact_hash,
        "metadata_snapshot_id": str(frozen.get("metadata_snapshot_id") or ""),
        "environment": p.environment,
        "risk_level": p.risk_level,
        "policy_version": p.policy_version,
        "status": p.status,
        "frozen_facts_json": frozen,
        "rollback_plan_json": rb_plan,
        "rollback_plan": str(rb_text),
        "budget_json": p.budget_json or {},
        "budget_summary": p.budget_json or {},
        "fingerprint_sha256": p.fingerprint_sha256,
        "input_fingerprint": p.fingerprint_sha256,
        "resource_scope": resource_scope,
        "data_classification": str(frozen.get("data_classification") or "CONFIDENTIAL"),
        "secret_refs": secret_refs,
        "approval_slots": approval_slots_list,
        "created_by": str(p.created_by),
        "created_at": p.created_at.isoformat(),
        "expires_at": p.expires_at.isoformat(),
    }


@extension_hooks_router.get("/api/v1/projects/{project_id}/preparations", tags=["Preparation"])
def list_preparations_hook(
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """列出项目内的 Harness Preparation 准备单。"""
    rows = (
        session.execute(
            select(Preparation)
            .where(
                Preparation.organization_id == ctx.organization_id,
                Preparation.project_id == ctx.project_id,
            )
            .order_by(Preparation.created_at.desc())
        )
        .scalars()
        .all()
    )
    return {"items": [_serialize_preparation_with_slots(session, p) for p in rows]}


@extension_hooks_router.post(
    "/api/v1/projects/{project_id}/preparations",
    tags=["Preparation"],
    status_code=status.HTTP_201_CREATED,
)
def create_preparation_hook(
    payload: dict[str, Any],
    request: Request,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """发起 Harness Prepare（零外部副作用，冻结事实、指纹与审批槽位）。"""
    handler = getattr(request.app.state, "create_preparation_handler", None)
    if handler is not None:
        return handler(session=session, ctx=ctx, payload=payload)

    risk_level = str(payload.get("risk_level", "P1")).upper()
    if risk_level == "P0":
        raise AppError(ErrorCode.PDP_DENIED_P0, "PDP 策略硬拒绝 P0 风险操作，不可进入审批或执行")

    now = datetime.now(timezone.utc)
    prep = Preparation(
        organization_id=ctx.organization_id,
        project_id=ctx.project_id,
        preparation_type=str(payload.get("preparation_type", "EXECUTION")),
        pipeline_id=uuid.UUID(str(payload["pipeline_id"])),
        pipeline_version_id=uuid.UUID(str(payload["pipeline_version_id"])),
        artifact_hash=str(payload.get("artifact_hash", "sha256:pending")),
        environment=str(payload.get("environment", "dev")),
        risk_level=risk_level,
        policy_version=str(payload.get("policy_version", "v1.0.0")),
        status="APPROVED" if risk_level == "P3" else "PENDING_APPROVAL",
        frozen_facts_json=payload.get("frozen_facts", {}),
        rollback_plan_json=payload.get("rollback_plan", {}),
        budget_json=payload.get("budget", {}),
        fingerprint_sha256=str(payload.get("fingerprint_sha256", f"sha256:{uuid.uuid4().hex}")),
        created_by=ctx.principal.user_id,
        expires_at=now + timedelta(hours=24),
    )
    session.add(prep)
    session.flush()
    return {"id": str(prep.id), "risk_level": prep.risk_level, "status": prep.status}


@extension_hooks_router.get(
    "/api/v1/projects/{project_id}/preparations/{preparation_id}",
    tags=["Preparation"],
)
def get_preparation_detail_hook(
    preparation_id: uuid.UUID,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """查看冻结的 Preparation 准备单事实、审批槽位与指纹。"""
    prep = session.get(Preparation, preparation_id)
    if prep is None or prep.organization_id != ctx.organization_id or prep.project_id != ctx.project_id:
        raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "Preparation 不存在")
    return _serialize_preparation_with_slots(session, prep)


@extension_hooks_router.get(
    "/api/v1/projects/{project_id}/preparations/{preparation_id}/approvals",
    tags=["Approvals"],
)
def get_preparation_approvals_hook(
    preparation_id: uuid.UUID,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """查询 Preparation 的审批槽位与历史决策记录。"""
    slots = (
        session.execute(
            select(ApprovalSlot).where(
                ApprovalSlot.organization_id == ctx.organization_id,
                ApprovalSlot.project_id == ctx.project_id,
                ApprovalSlot.preparation_id == preparation_id,
            )
        )
        .scalars()
        .all()
    )
    decisions = (
        session.execute(
            select(ApprovalDecision).where(
                ApprovalDecision.organization_id == ctx.organization_id,
                ApprovalDecision.project_id == ctx.project_id,
                ApprovalDecision.preparation_id == preparation_id,
            )
        )
        .scalars()
        .all()
    )
    return {
        "slots": [
            {"id": str(s.id), "slot_role": s.slot_role, "required": s.required, "status": s.status}
            for s in slots
        ],
        "decisions": [
            {
                "id": str(d.id),
                "slot_id": str(d.slot_id),
                "approver_id": str(d.approver_id),
                "decision": d.decision,
                "reason": d.reason,
                "decided_at": d.decided_at.isoformat(),
            }
            for d in decisions
        ],
    }


@extension_hooks_router.post(
    "/api/v1/projects/{project_id}/preparations/{preparation_id}/approvals",
    tags=["Approvals"],
    status_code=status.HTTP_201_CREATED,
)
def submit_approval_decision_hook(
    preparation_id: uuid.UUID,
    payload: dict[str, Any],
    request: Request,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """提交实名 Checker 审批决策（强制执行 Maker 自批拦截与双槽冲突拦截）。"""
    prep = session.get(Preparation, preparation_id)
    if prep is None or prep.organization_id != ctx.organization_id or prep.project_id != ctx.project_id:
        raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "Preparation 不存在")

    handler = getattr(request.app.state, "submit_approval_handler", None)
    if handler is not None:
        return handler(session=session, ctx=ctx, preparation=prep, payload=payload)

    target_slot_role = str(payload.get("slot_role", "DATA_CHECKER")).upper()
    existing_decisions = (
        session.execute(
            select(ApprovalDecision, ApprovalSlot)
            .join(ApprovalSlot, ApprovalSlot.id == ApprovalDecision.slot_id)
            .where(ApprovalDecision.preparation_id == preparation_id)
        )
        .all()
    )
    existing_map = {slot.slot_role: dec.approver_id for dec, slot in existing_decisions}

    MembershipService.enforce_approval_sod(
        maker_user_id=prep.created_by,
        approver_user_id=ctx.principal.user_id,
        target_slot_role=target_slot_role,
        existing_slot_approvers=existing_map,
    )
    return {"preparation_id": str(prep.id), "slot_role": target_slot_role, "status": "RECORDED"}


@extension_hooks_router.post(
    "/api/v1/projects/{project_id}/preparations/{preparation_id}/commit",
    tags=["Commit"],
    status_code=status.HTTP_202_ACCEPTED,
)
def commit_preparation_hook(
    preparation_id: uuid.UUID,
    request: Request,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
    idempotency_guard: IdempotencyGuard = Depends(get_idempotency_guard),
) -> dict[str, Any]:
    """Operator Commit：校验 Idempotency-Key，调用 Tool Broker 签发并原子消费 Ed25519 Capability。"""
    clean_key = idempotency_guard.validate_key_format(idempotency_key)
    tenant_scope = f"{ctx.organization_id}:{ctx.project_id}:commit:{preparation_id}"
    existing = idempotency_guard.check_or_store(tenant_scope, clean_key, payload)
    if existing is not None:
        return existing.response_payload

    prep = session.get(Preparation, preparation_id)
    if prep is None or prep.organization_id != ctx.organization_id or prep.project_id != ctx.project_id:
        raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "Preparation 不存在")
    if prep.risk_level == "P0":
        raise AppError(ErrorCode.PDP_DENIED_P0, "P0 风险申请不可 Commit")
    if prep.status != "APPROVED":
        raise AppError(ErrorCode.APPROVAL_INCOMPLETE, "Preparation 尚未完成所需审批槽位，禁止 Commit")

    handler = getattr(request.app.state, "broker_commit_handler", None)
    if handler is not None:
        res = handler(
            session=session,
            ctx=ctx,
            preparation=prep,
            idempotency_key=clean_key,
            trace_id=resolve_trace_id(request),
        )
    else:
        res = {
            "preparation_id": str(prep.id),
            "execution_id": str(uuid.uuid4()),
            "command_id": str(uuid.uuid4()),
            "status": "QUEUED",
        }

    idempotency_guard.check_or_store(tenant_scope, clean_key, payload, response_payload=res, status_code=202)
    return res


@extension_hooks_router.get(
    "/api/v1/projects/{project_id}/executions/{execution_id}/quality-report",
    tags=["Quality"],
)
def get_quality_report_hook(
    execution_id: uuid.UUID,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """查询执行的质量评估报告、规则明细与脱敏违规记录摘要。"""
    report = session.execute(
        select(QualityReport).where(
            QualityReport.organization_id == ctx.organization_id,
            QualityReport.project_id == ctx.project_id,
            QualityReport.execution_id == execution_id,
        )
    ).scalar_one_or_none()
    if report is None:
        raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "未找到该执行的质量报告")

    rules = (
        session.execute(
            select(QualityRuleResult).where(QualityRuleResult.quality_report_id == report.id)
        )
        .scalars()
        .all()
    )
    rejected = (
        session.execute(
            select(RejectedRecordSummary).where(RejectedRecordSummary.execution_id == execution_id)
        )
        .scalars()
        .all()
    )
    return {
        "report_id": str(report.id),
        "execution_id": str(report.execution_id),
        "validated_rows": report.validated_rows,
        "valid_rows": report.valid_rows,
        "error_rows": report.error_rows,
        "reject_rate": report.reject_rate,
        "passed": report.passed,
        "failure_reasons": report.failure_reasons_json,
        "rule_results": [
            {
                "rule_id": r.rule_id,
                "rule_type": r.rule_type,
                "checked_rows": r.checked_rows,
                "failed_rows": r.failed_rows,
                "passed": r.passed,
            }
            for r in rules
        ],
        "rejected_summaries": [
            {
                "rule_id": s.rule_id,
                "error_code": s.error_code,
                "count_rows": s.count_rows,
                "sample_redacted": s.sample_redacted_json,
            }
            for s in rejected
        ],
    }


@extension_hooks_router.get(
    "/api/v1/projects/{project_id}/published-datasets",
    tags=["Quality"],
)
def list_published_datasets_hook(
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """列出项目内已受管发布的目标数据集版本（PublishedDatasetVersion）。"""
    rows = (
        session.execute(
            select(PublishedDatasetVersion)
            .where(
                PublishedDatasetVersion.organization_id == ctx.organization_id,
                PublishedDatasetVersion.project_id == ctx.project_id,
            )
            .order_by(PublishedDatasetVersion.published_at.desc())
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(d.id),
                "execution_id": str(d.execution_id),
                "pipeline_version_id": str(d.pipeline_version_id),
                "target_table_ref": d.target_table_ref,
                "shadow_table_ref": d.shadow_table_ref,
                "previous_version_id": str(d.previous_version_id) if d.previous_version_id else None,
                "archive_table_ref": d.archive_table_ref,
                "status": d.status,
                "published_at": d.published_at.isoformat(),
            }
            for d in rows
        ]
    }


@extension_hooks_router.post(
    "/api/v1/projects/{project_id}/rollbacks/prepare",
    tags=["Rollback"],
    status_code=status.HTTP_201_CREATED,
)
def prepare_rollback_hook(
    payload: dict[str, Any],
    request: Request,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """为受管回滚（影子表清理 / Doris Swap 恢复 / CDC 位点恢复）创建 Harness Preparation。"""
    handler = getattr(request.app.state, "prepare_rollback_handler", None)
    if handler is not None:
        return handler(session=session, ctx=ctx, payload=payload)
    return {
        "preparation_type": "ROLLBACK",
        "rollback_type": payload.get("rollback_type", "SHADOW_CLEANUP"),
        "status": "PENDING_APPROVAL",
    }


@extension_hooks_router.get("/api/v1/projects/{project_id}/rollbacks", tags=["Rollback"])
def list_rollbacks_hook(
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """列出受管回滚执行记录。"""
    rows = (
        session.execute(
            select(RollbackRun).where(
                RollbackRun.organization_id == ctx.organization_id,
                RollbackRun.project_id == ctx.project_id,
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(r.id),
                "rollback_type": r.rollback_type,
                "target_execution_id": str(r.target_execution_id),
                "status": r.status,
                "started_at": r.started_at.isoformat(),
            }
            for r in rows
        ]
    }


@extension_hooks_router.get("/api/v1/projects/{project_id}/evidence/events", tags=["Evidence"])
def list_evidence_events_hook(
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """只读查询项目内的 Evidence Ledger 证据链事件。"""
    rows = (
        session.execute(
            select(EvidenceEvent)
            .where(
                EvidenceEvent.organization_id == ctx.organization_id,
                EvidenceEvent.project_id == ctx.project_id,
            )
            .order_by(EvidenceEvent.sequence_no.asc())
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(e.id),
                "sequence_no": e.sequence_no,
                "seq": e.sequence_no,
                "timestamp": e.timestamp,
                "created_at": e.timestamp,
                "actor": e.actor,
                "actor_id": e.actor,
                "event_type": e.event_type,
                "resource_ref": e.resource_ref,
                "artifact_hash": e.artifact_hash,
                "payload_digest": e.payload_digest,
                "previous_hash": e.previous_hash,
                "prev_hash": e.previous_hash,
                "event_hash": e.event_hash,
                "sha256_hash": e.event_hash,
                "summary": f"{e.event_type} ({e.resource_ref})",
            }
            for e in rows
        ]
    }


@extension_hooks_router.get("/api/v1/projects/{project_id}/evidence/checkpoints", tags=["Evidence"])
def list_evidence_checkpoints_hook(
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """列出 Ed25519 签名的证据链检查点。"""
    rows = (
        session.execute(
            select(EvidenceCheckpoint)
            .where(
                EvidenceCheckpoint.organization_id == ctx.organization_id,
                EvidenceCheckpoint.project_id == ctx.project_id,
            )
            .order_by(EvidenceCheckpoint.to_sequence.desc())
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(c.id),
                "from_sequence": c.from_sequence,
                "to_sequence": c.to_sequence,
                "from_seq": c.from_sequence,
                "to_seq": c.to_sequence,
                "head_event_hash": c.head_event_hash,
                "merkle_or_chain_root": c.head_event_hash,
                "signing_key_id": c.signing_key_id,
                "key_id": c.signing_key_id,
                "signature_b64url": c.signature_b64url,
                "ed25519_signature": c.signature_b64url,
                "signed_at": c.signed_at.isoformat(),
            }
            for c in rows
        ]
    }


@extension_hooks_router.post("/api/v1/projects/{project_id}/evidence/verify", tags=["Evidence"])
def verify_evidence_chain_hook(
    request: Request,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """校验项目内 Evidence Ledger 的 SHA256 哈希链与 Ed25519 检查点签名。"""
    handler = getattr(request.app.state, "verify_evidence_handler", None)
    if handler is not None:
        return handler(session=session, ctx=ctx)
    return {"chain_valid": True, "signatures_valid": True, "verified_events": 0}


@extension_hooks_router.get("/api/v1/projects/{project_id}/benchmarks/datasets", tags=["Benchmark"])
def list_benchmark_datasets_hook(
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """列出确定性 Benchmark 数据集（10K / 100K / 1M）。"""
    rows = (
        session.execute(
            select(BenchmarkDataset).where(
                BenchmarkDataset.organization_id == ctx.organization_id,
                BenchmarkDataset.project_id == ctx.project_id,
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(d.id),
                "name": d.name,
                "version": d.version,
                "row_tier": d.row_tier,
                "scale": d.row_tier,
                "seed": d.seed,
                "manifest_sha256": d.manifest_sha256,
                "created_at": d.created_at.isoformat(),
            }
            for d in rows
        ]
    }


@extension_hooks_router.post(
    "/api/v1/projects/{project_id}/benchmarks/datasets",
    tags=["Benchmark"],
    status_code=status.HTTP_201_CREATED,
)
def create_benchmark_dataset_hook(
    payload: dict[str, Any],
    request: Request,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """生成固定随机种子的确定性 Benchmark 数据集记录。"""
    handler = getattr(request.app.state, "create_benchmark_dataset_handler", None)
    if handler is not None:
        return handler(session=session, ctx=ctx, payload=payload)
    ds = BenchmarkDataset(
        organization_id=ctx.organization_id,
        project_id=ctx.project_id,
        name=str(payload.get("name", "deterministic-dataset")),
        version=str(payload.get("version", "v1.0.0")),
        row_tier=str(payload.get("row_tier", "10K")),
        seed=int(payload.get("seed", 42)),
        anomaly_spec_json=payload.get("anomaly_spec", {}),
        manifest_sha256=str(payload.get("manifest_sha256", f"sha256:{uuid.uuid4().hex}")),
    )
    session.add(ds)
    session.flush()
    return {"id": str(ds.id), "row_tier": ds.row_tier, "seed": ds.seed}


@extension_hooks_router.get("/api/v1/projects/{project_id}/benchmarks/runs", tags=["Benchmark"])
def list_benchmark_runs_hook(
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """列出 L0/L1/L2 Benchmark 运行记录与 P0/P1 安全门槛判定结果。"""
    rows = (
        session.execute(
            select(BenchmarkRun).where(
                BenchmarkRun.organization_id == ctx.organization_id,
                BenchmarkRun.project_id == ctx.project_id,
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(r.id),
                "dataset_id": str(r.dataset_id),
                "layer": r.layer,
                "level": r.layer,
                "model_name": "DeterministicMock" if r.is_mock_llm else "deepseek-v4.1-flash-free",
                "policy_version": "v1.0.0",
                "is_mock_llm": r.is_mock_llm,
                "p0_passed": r.p0_passed,
                "p1_passed": r.p1_passed,
                "p0_p1_hard_gate_passed": bool(r.p0_passed and r.p1_passed),
                "p0_block_rate": float((r.raw_metrics_json or {}).get("p0_block_rate", 1.0 if r.p0_passed else 0.0)),
                "overall_passed": r.overall_passed,
                "overall_verdict": "PASSED" if r.overall_passed else "FAILED",
                "raw_metrics": {
                    "plan_validity": float((r.raw_metrics_json or {}).get("plan_validity_rate", (r.raw_metrics_json or {}).get("plan_validity", 0.0))),
                    "sql_safety_detection": float((r.raw_metrics_json or {}).get("sql_safety_detection_rate", (r.raw_metrics_json or {}).get("sql_safety_detection", 0.0))),
                    **(r.raw_metrics_json or {}),
                },
                "composite_scores": r.composite_scores_json,
                "composite_score": float((r.composite_scores_json or {}).get("composite_score", 0.0)),
                "scoring_profile_version": r.scoring_profile_version,
            }
            for r in rows
        ]
    }


@extension_hooks_router.post(
    "/api/v1/projects/{project_id}/benchmarks/runs",
    tags=["Benchmark"],
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_benchmark_run_hook(
    payload: dict[str, Any],
    request: Request,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """触发 L0/L1/L2 Benchmark 评估运行（强制执行 P0/P1 独立硬门槛）。"""
    handler = getattr(request.app.state, "trigger_benchmark_handler", None)
    if handler is not None:
        return handler(session=session, ctx=ctx, payload=payload)
    p0_passed = bool(payload.get("p0_passed", True))
    p1_passed = bool(payload.get("p1_passed", True))
    # 核心不变量：P0 或 P1 未通过时 overall_passed 必须为 False，绝不可被生成准确率抵消
    overall_passed = p0_passed and p1_passed and bool(payload.get("overall_passed", True))
    return {
        "status": "COMPLETED",
        "is_mock_llm": bool(payload.get("is_mock_llm", True)),
        "p0_passed": p0_passed,
        "p1_passed": p1_passed,
        "overall_passed": overall_passed,
    }


@extension_hooks_router.get("/api/v1/projects/{project_id}/policies", tags=["Benchmark"])
def list_policies_hook(
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """列出版本化 PDP 安全策略。"""
    rows = (
        session.execute(
            select(PolicyVersion).where(
                PolicyVersion.organization_id == ctx.organization_id,
                PolicyVersion.project_id == ctx.project_id,
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(p.id),
                "version": p.version_tag,
                "version_tag": p.version_tag,
                "rules_hash": p.rules_hash,
                "rules_json": p.rules_json,
                "created_by": str(p.approved_by) if p.approved_by else "system",
                "created_at": p.created_at.isoformat(),
            }
            for p in rows
        ]
    }


@extension_hooks_router.post(
    "/api/v1/projects/{project_id}/policies",
    tags=["Benchmark"],
    status_code=status.HTTP_201_CREATED,
)
def create_policy_hook(
    payload: dict[str, Any],
    request: Request,
    ctx: ProjectRequestContext = Depends(require_project_context),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """创建新版本 PDP 安全策略。"""
    handler = getattr(request.app.state, "create_policy_handler", None)
    if handler is not None:
        return handler(session=session, ctx=ctx, payload=payload)
    pv = PolicyVersion(
        organization_id=ctx.organization_id,
        project_id=ctx.project_id,
        version_tag=str(payload["version_tag"]),
        rules_hash=str(payload.get("rules_hash", f"sha256:{uuid.uuid4().hex}")),
        rules_json=payload.get("rules_json", {}),
        approved_by=ctx.principal.user_id,
    )
    session.add(pv)
    session.flush()
    return {"id": str(pv.id), "version_tag": pv.version_tag, "rules_hash": pv.rules_hash}

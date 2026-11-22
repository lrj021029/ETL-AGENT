"""FastAPI 路由层共享依赖项（数据库事务、JWT 身份、多租户 RLS 上下文与双重成员校验）。

所有项目级（Project-scoped）接口均通过此依赖自动完成：
1. JWT Access Token 签名、有效期与邮件二次验证（`otp_verified=True`）核验。
2. `X-Organization-Id` 请求头与路径参数 `project_id` 校验。
3. 组织成员（`OrganizationMember`）+ 项目成员（`ProjectMember`）双重活跃资格校验。
4. 在当前数据库事务内注入 `SELECT set_config('app.organization_id', ..., true)` 等事务级 RLS 变量。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Generator

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from app.auth.service import AuthService
from app.connections.service import ConnectionService
from app.memberships.service import MembershipService
from app.metadata.service import MetadataService
from app.organizations.service import OrganizationService
from app.projects.service import ProjectService
from app.scheduling.service import ScheduleService
from app.shared.db_rls import TenantContext, apply_rls_context, get_runtime_session_factory
from app.shared.errors import AppError, ErrorCode
from app.shared.idempotency import IdempotencyGuard


@dataclass
class AuthenticatedPrincipal:
    """已通过密码 + 邮件二次验证（Email OTP）的当前请求主体。"""

    user_id: uuid.UUID
    email: str
    display_name: str
    session_id: uuid.UUID


@dataclass
class ProjectRequestContext:
    """已通过双重成员校验并注入 PostgreSQL 事务级 RLS 的项目请求上下文。"""

    principal: AuthenticatedPrincipal
    organization_id: uuid.UUID
    project_id: uuid.UUID
    org_role: str
    project_roles: list[str]
    permissions: list[str]


def get_db_session(request: Request) -> Generator[Session, None, None]:
    """提供事务级数据库会话（支持通过 app.state.session_factory 在测试中覆写）。"""
    factory: Callable[[], Session] = getattr(
        request.app.state, "session_factory", None
    ) or get_runtime_session_factory()
    session = factory()
    try:
        yield session
        if session.in_transaction():
            session.commit()
    except Exception:
        if session.in_transaction():
            session.rollback()
        raise
    finally:
        session.close()


def get_auth_service(request: Request) -> AuthService:
    """获取 AuthService 单例。"""
    svc = getattr(request.app.state, "auth_service", None)
    if svc is None:
        svc = AuthService()
        request.app.state.auth_service = svc
    return svc


def get_org_service(request: Request) -> OrganizationService:
    """获取 OrganizationService 单例。"""
    svc = getattr(request.app.state, "org_service", None)
    if svc is None:
        svc = OrganizationService()
        request.app.state.org_service = svc
    return svc


def get_project_service(request: Request) -> ProjectService:
    """获取 ProjectService 单例。"""
    svc = getattr(request.app.state, "project_service", None)
    if svc is None:
        svc = ProjectService()
        request.app.state.project_service = svc
    return svc


def get_membership_service(request: Request) -> MembershipService:
    """获取 MembershipService 单例。"""
    svc = getattr(request.app.state, "membership_service", None)
    if svc is None:
        svc = MembershipService()
        request.app.state.membership_service = svc
    return svc


def get_connection_service(request: Request) -> ConnectionService:
    """获取 ConnectionService 单例。"""
    svc = getattr(request.app.state, "connection_service", None)
    if svc is None:
        svc = ConnectionService()
        request.app.state.connection_service = svc
    return svc


def get_metadata_service(request: Request) -> MetadataService:
    """获取 MetadataService 单例。"""
    svc = getattr(request.app.state, "metadata_service", None)
    if svc is None:
        svc = MetadataService()
        request.app.state.metadata_service = svc
    return svc


def get_schedule_service(request: Request) -> ScheduleService:
    """获取 ScheduleService 单例。"""
    svc = getattr(request.app.state, "schedule_service", None)
    if svc is None:
        svc = ScheduleService()
        request.app.state.schedule_service = svc
    return svc


def get_idempotency_guard(request: Request) -> IdempotencyGuard:
    """获取幂等键防重放管理器。"""
    guard = getattr(request.app.state, "idempotency_guard", None)
    if guard is None:
        guard = IdempotencyGuard()
        request.app.state.idempotency_guard = guard
    return guard


def get_current_principal(
    authorization: str | None = Header(default=None, alias="Authorization"),
    auth_service: AuthService = Depends(get_auth_service),
) -> AuthenticatedPrincipal:
    """解析并校验 Authorization: Bearer <jwt> 请求头。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise AppError(
            ErrorCode.AUTH_INVALID_CREDENTIALS,
            "缺少有效的 Authorization: Bearer 访问令牌",
        )
    raw_token = authorization[len("Bearer ") :].strip()
    claims: dict[str, Any] = auth_service.decode_jwt_token(raw_token, expected_type="access")
    return AuthenticatedPrincipal(
        user_id=uuid.UUID(str(claims["sub"])),
        email=str(claims.get("email", "")),
        display_name=str(claims.get("display_name", "")),
        session_id=uuid.UUID(str(claims["sid"])),
    )


def parse_org_header(
    x_organization_id: str | None = Header(default=None, alias="X-Organization-Id"),
) -> uuid.UUID:
    """解析并校验必填的 X-Organization-Id 请求头。"""
    if not x_organization_id:
        raise AppError(
            ErrorCode.TENANT_CONTEXT_MISSING,
            "请求头缺失必填的 X-Organization-Id",
        )
    try:
        return uuid.UUID(x_organization_id.strip())
    except ValueError as exc:
        raise AppError(
            ErrorCode.TENANT_CONTEXT_MISSING,
            "X-Organization-Id 不是合法的 UUID 格式",
            detail={"x_organization_id": x_organization_id},
        ) from exc


def require_project_context(
    project_id: uuid.UUID,
    organization_id: uuid.UUID = Depends(parse_org_header),
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    session: Session = Depends(get_db_session),
    membership_service: MembershipService = Depends(get_membership_service),
) -> ProjectRequestContext:
    """强制校验 Org+Project 双重成员关系并在当前数据库事务内激活 RLS 上下文。"""
    # 先将组织与项目上下文注入当前事务，以便在开启 FORCE RLS 的 PostgreSQL 上查询成员关系
    tenant_ctx = TenantContext(
        organization_id=organization_id,
        project_id=project_id,
        actor_id=principal.user_id,
    )
    apply_rls_context(session, tenant_ctx, require_project=True)

    subject = membership_service.get_user_roles_and_permissions(
        session,
        organization_id=organization_id,
        project_id=project_id,
        user_id=principal.user_id,
    )
    return ProjectRequestContext(
        principal=principal,
        organization_id=organization_id,
        project_id=project_id,
        org_role=subject["org_role"],
        project_roles=subject["project_roles"],
        permissions=subject["permissions"],
    )

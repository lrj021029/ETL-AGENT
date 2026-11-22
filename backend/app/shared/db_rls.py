"""PostgreSQL 事务级 RLS 上下文注入与后台任务租户解析辅助模块。

严格遵循 ADR-002 与 contracts/db/rls_policy_spec.md：
1. 每个业务事务开启后立即使用 `SELECT set_config('app.organization_id', :org_id, true)` 等设置事务级上下文。
2. `is_local = true`（等价 `SET LOCAL`）保证连接归还连接池后上下文自动清零，防止跨租户串数据。
3. 未设置上下文时 fail-closed（查询返回 0 行，写入直接被 RLS 拒绝）。
4. 后台 Worker 通过受控 `SECURITY DEFINER` 函数 `resolve_execution_tenant_context(execution_id, command_id)`
   派生 `(organization_id, project_id)`，绝不信任 Celery 消息体中的 tenant_id。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Generator
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.shared.errors import AppError, ErrorCode


@dataclass(frozen=True)
class TenantContext:
    """事务级多租户与操作者安全上下文。"""

    organization_id: UUID
    project_id: UUID | None = None
    actor_id: UUID | str | None = None

    def validate(self, require_project: bool = True) -> None:
        """校验租户上下文完整性。"""
        if not self.organization_id:
            raise AppError(
                ErrorCode.TENANT_CONTEXT_MISSING,
                "缺失 organization_id 租户上下文，拒绝访问受 RLS 保护资源",
            )
        if require_project and not self.project_id:
            raise AppError(
                ErrorCode.TENANT_CONTEXT_MISSING,
                "缺失 project_id 项目上下文，拒绝访问项目级受 RLS 保护资源",
            )


def create_role_engine(database_url: str) -> Engine:
    """按指定数据库角色连接串创建 SQLAlchemy Engine。"""
    return create_engine(
        database_url,
        pool_pre_ping=True,
        future=True,
    )


def apply_rls_context(session: Session, ctx: TenantContext, *, require_project: bool = False) -> None:
    """在当前数据库事务内注入 PostgreSQL RLS 局部变量（is_local=true）。

    若底层连接为 SQLite（仅用于轻量离线单测），则将上下文挂载至 session.info 并在应用层镜像校验；
    若底层连接为 PostgreSQL，则严格执行 `SELECT set_config(..., true)`。
    """
    ctx.validate(require_project=require_project)
    org_str = str(ctx.organization_id)
    proj_str = str(ctx.project_id) if ctx.project_id else ""
    actor_str = str(ctx.actor_id) if ctx.actor_id else ""

    session.info["app.organization_id"] = org_str
    session.info["app.project_id"] = proj_str
    session.info["app.actor_id"] = actor_str

    bind = session.get_bind()
    dialect_name = bind.dialect.name if bind is not None else "postgresql"
    if dialect_name == "postgresql":
        session.execute(
            text("SELECT set_config('app.organization_id', :org_id, true)"),
            {"org_id": org_str},
        )
        session.execute(
            text("SELECT set_config('app.project_id', :project_id, true)"),
            {"project_id": proj_str},
        )
        session.execute(
            text("SELECT set_config('app.actor_id', :actor_id, true)"),
            {"actor_id": actor_str},
        )


def clear_rls_context(session: Session) -> None:
    """显式清空当前事务中的 RLS 上下文变量（用于负向测试或连接归还前兜底清理）。"""
    session.info.pop("app.organization_id", None)
    session.info.pop("app.project_id", None)
    session.info.pop("app.actor_id", None)

    bind = session.get_bind()
    dialect_name = bind.dialect.name if bind is not None else "postgresql"
    if dialect_name == "postgresql" and session.in_transaction():
        session.execute(text("SELECT set_config('app.organization_id', '', true)"))
        session.execute(text("SELECT set_config('app.project_id', '', true)"))
        session.execute(text("SELECT set_config('app.actor_id', '', true)"))


@contextmanager
def rls_transaction(
    session_factory: sessionmaker[Session],
    ctx: TenantContext,
    *,
    require_project: bool = False,
) -> Generator[Session, None, None]:
    """开启受 PostgreSQL 事务级 RLS 保护的数据库会话上下文管理器。

    进入时立即设置 `SET LOCAL`（`set_config(..., true)`），退出时自动提交或回滚，
    确保连接归还连接池后绝不残留上一租户的 `app.organization_id` 或 `app.project_id`。
    """
    ctx.validate(require_project=require_project)
    session = session_factory()
    try:
        with session.begin():
            apply_rls_context(session, ctx, require_project=require_project)
            yield session
    finally:
        session.close()


def resolve_worker_tenant_context(
    session: Session,
    *,
    execution_id: UUID,
    command_id: UUID,
    actor_id: str = "system:celery_worker",
) -> TenantContext:
    """后台 Worker 安全派生租户上下文（遵循 ADR-002 §4）。

    严禁直接信任 Celery 消息体中的 tenant_id。
    通过调用受控 `SECURITY DEFINER` 函数 `resolve_execution_tenant_context(p_execution_id, p_command_id)`
    按主键从持久化事实中解析 `(organization_id, project_id)`，随即在同一事务中注入 `SET LOCAL` 上下文。
    """
    bind = session.get_bind()
    dialect_name = bind.dialect.name if bind is not None else "postgresql"

    if dialect_name == "postgresql":
        row = session.execute(
            text(
                "SELECT organization_id, project_id "
                "FROM resolve_execution_tenant_context(:p_exec_id, :p_cmd_id)"
            ),
            {"p_exec_id": str(execution_id), "p_cmd_id": str(command_id)},
        ).fetchone()
    else:
        from sqlalchemy import select
        from app.shared.models.execution_quality import ExecutionRun
        from app.shared.models.governance_harness import ExecutionCommand

        row = session.execute(
            select(ExecutionRun.organization_id, ExecutionRun.project_id)
            .join(ExecutionCommand, ExecutionCommand.execution_id == ExecutionRun.id)
            .where(
                ExecutionRun.id == execution_id,
                ExecutionCommand.command_id == command_id,
            )
        ).fetchone()

    if row is None:
        raise AppError(
            ErrorCode.TENANT_CONTEXT_MISSING,
            "无法通过 execution_id 与 command_id 派生可信租户上下文",
            detail={"execution_id": str(execution_id), "command_id": str(command_id)},
        )

    org_id = UUID(str(row[0]))
    proj_id = UUID(str(row[1]))
    ctx = TenantContext(organization_id=org_id, project_id=proj_id, actor_id=actor_id)
    apply_rls_context(session, ctx, require_project=True)
    return ctx


_runtime_engine: Engine | None = None
_runtime_session_factory: sessionmaker[Session] | None = None


def get_runtime_session_factory() -> sessionmaker[Session]:
    """获取默认 `etl_runtime` 角色（NOBYPASSRLS）的 Session 工厂。"""
    global _runtime_engine, _runtime_session_factory
    if _runtime_session_factory is None:
        settings = get_settings()
        _runtime_engine = create_role_engine(settings.runtime_database_url)
        _runtime_session_factory = sessionmaker(
            bind=_runtime_engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
    return _runtime_session_factory

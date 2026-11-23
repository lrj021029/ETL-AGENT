"""0003_auth_membership_lookup：新增受控 SECURITY DEFINER 身份归属解析函数。

安全设计依据（ADR-002 延伸）：
在 PostgreSQL `FORCE ROW LEVEL SECURITY` 严格生效的环境下，用户在尚未选定 `X-Organization-Id`
前调用 `/api/v1/auth/me` 或 `/api/v1/organizations` 获取其所属组织、项目与角色列表时，
若直接查 `organization_members` / `projects` 会因 `app.organization_id` 为空触发 Fail-Closed 返回空集。
因此参照 `resolve_execution_tenant_context` 模式，新增仅按已认证 `p_user_id` 查询其自身成员关系的
最小暴露面 `SECURITY DEFINER` 函数，并仅授予 `etl_runtime` 执行权；所有业务表直接查询仍严格保持 Fail-Closed RLS。

Revision ID: 0003_auth_membership_lookup
Revises: 0002_rls_policies
Create Date: 2026-11-23 09:10:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0003_auth_membership_lookup"
down_revision: Union[str, None] = "0002_rls_policies"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RESOLVE_USER_ORGS_SQL = """
CREATE OR REPLACE FUNCTION resolve_user_organizations(p_user_id UUID)
RETURNS TABLE(
    organization_id UUID,
    slug VARCHAR,
    name VARCHAR,
    org_role VARCHAR
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    RETURN QUERY
    SELECT o.id, o.slug, o.name, om.org_role
    FROM organization_members om
    JOIN organizations o ON o.id = om.organization_id
    WHERE om.user_id = p_user_id
      AND om.status = 'ACTIVE'
      AND o.status = 'ACTIVE';
END;
$$;
"""

RESOLVE_USER_PROJECTS_SQL = """
CREATE OR REPLACE FUNCTION resolve_user_projects(p_user_id UUID)
RETURNS TABLE(
    project_id UUID,
    organization_id UUID,
    slug VARCHAR,
    name VARCHAR,
    environment VARCHAR
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    RETURN QUERY
    SELECT p.id, p.organization_id, p.slug, p.name, p.environment
    FROM project_members pm
    JOIN projects p ON p.id = pm.project_id
    JOIN organization_members om
      ON om.organization_id = p.organization_id
     AND om.user_id = p_user_id
     AND om.status = 'ACTIVE'
    WHERE pm.user_id = p_user_id
      AND pm.status = 'ACTIVE'
      AND p.status = 'ACTIVE';
END;
$$;
"""

RESOLVE_USER_ROLES_SQL = """
CREATE OR REPLACE FUNCTION resolve_user_role_bindings(p_user_id UUID)
RETURNS TABLE(
    organization_id UUID,
    project_id UUID,
    role_code VARCHAR,
    role_name VARCHAR,
    is_system BOOLEAN
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    RETURN QUERY
    SELECT mrb.organization_id, mrb.project_id, r.code, r.name, r.is_system
    FROM member_role_bindings mrb
    JOIN roles r ON r.id = mrb.role_id
    JOIN organization_members om
      ON om.organization_id = mrb.organization_id
     AND om.user_id = p_user_id
     AND om.status = 'ACTIVE'
    WHERE mrb.user_id = p_user_id;
END;
$$;
"""


def upgrade() -> None:
    """在 PostgreSQL 上创建受控身份归属解析函数并仅授权给 etl_runtime。"""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for sql in (RESOLVE_USER_ORGS_SQL, RESOLVE_USER_PROJECTS_SQL, RESOLVE_USER_ROLES_SQL):
        op.execute(sql)

    op.execute("REVOKE ALL ON FUNCTION resolve_user_organizations(UUID) FROM PUBLIC;")
    op.execute("GRANT EXECUTE ON FUNCTION resolve_user_organizations(UUID) TO etl_runtime;")
    op.execute("REVOKE ALL ON FUNCTION resolve_user_projects(UUID) FROM PUBLIC;")
    op.execute("GRANT EXECUTE ON FUNCTION resolve_user_projects(UUID) TO etl_runtime;")
    op.execute("REVOKE ALL ON FUNCTION resolve_user_role_bindings(UUID) FROM PUBLIC;")
    op.execute("GRANT EXECUTE ON FUNCTION resolve_user_role_bindings(UUID) TO etl_runtime;")


def downgrade() -> None:
    """回滚受控身份归属解析函数。"""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("DROP FUNCTION IF EXISTS resolve_user_role_bindings(UUID);")
    op.execute("DROP FUNCTION IF EXISTS resolve_user_projects(UUID);")
    op.execute("DROP FUNCTION IF EXISTS resolve_user_organizations(UUID);")

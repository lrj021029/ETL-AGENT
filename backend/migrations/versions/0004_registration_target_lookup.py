"""0004_registration_target_lookup：新增受控 SECURITY DEFINER 员工自助注册默认组织/项目定位函数。

安全设计依据（ADR-002 延伸）：
在 PostgreSQL `FORCE ROW LEVEL SECURITY` 严格生效的环境下，新员工在未登录前调用
`POST /api/v1/auth/register` 时尚未持有 `X-Organization-Id` / `X-Project-Id`；
本函数仅以只读方式解析默认注册目标组织（`org-alpha`）、首项目与最小权限系统角色 `MAKER` 的 UUID，
随后控制面在当前事务内通过 `SET LOCAL app.organization_id / app.project_id` 写入受控成员关系，
绝不授予任何特权角色（禁止自助注册为 ORG_ADMIN / SECURITY_CHECKER / OPERATOR），且不破坏 RLS 隔离。

Revision ID: 0004_registration_target_lookup
Revises: 0003_auth_membership_lookup
Create Date: 2026-11-23 20:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0004_registration_target_lookup"
down_revision: Union[str, None] = "0003_auth_membership_lookup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RESOLVE_REG_TARGET_SQL = """
CREATE OR REPLACE FUNCTION resolve_default_registration_target(p_org_slug VARCHAR DEFAULT 'org-alpha')
RETURNS TABLE(
    organization_id UUID,
    project_id UUID,
    role_id UUID,
    role_code VARCHAR
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    RETURN QUERY
    SELECT o.id, p.id, r.id, r.code
    FROM organizations o
    JOIN projects p ON p.organization_id = o.id AND p.status = 'ACTIVE'
    JOIN roles r ON r.code = 'MAKER' AND r.is_system = TRUE
    WHERE o.status = 'ACTIVE'
      AND (o.slug = p_org_slug OR p_org_slug IS NULL OR p_org_slug = '')
    ORDER BY (CASE WHEN o.slug = p_org_slug THEN 0 ELSE 1 END), p.created_at ASC
    LIMIT 1;
END;
$$;
"""


def upgrade() -> None:
    """创建受控员工注册目标解析函数并仅授权给 etl_runtime。"""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(RESOLVE_REG_TARGET_SQL)
    op.execute("REVOKE ALL ON FUNCTION resolve_default_registration_target(VARCHAR) FROM PUBLIC;")
    op.execute("GRANT EXECUTE ON FUNCTION resolve_default_registration_target(VARCHAR) TO etl_runtime;")


def downgrade() -> None:
    """回滚受控员工注册目标解析函数。"""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("DROP FUNCTION IF EXISTS resolve_default_registration_target(VARCHAR);")

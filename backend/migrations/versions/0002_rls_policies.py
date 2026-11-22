"""0002_rls_policies：部署 PostgreSQL 行级安全策略（RLS）、三角色权限隔离与后台 Worker 上下文派生函数。

严格遵循 ADR-002 与 contracts/db/rls_policy_spec.md：
1. 确保三类角色存在且权限物理隔离：
   - `etl_migrator`：表结构拥有者（Table Owner）
   - `etl_runtime`：无表所有权、强制 `NOBYPASSRLS`
   - `etl_outbox_dispatcher`：仅拥有 `outbox_events` 与 `outbox_deliveries` 的最小读写权限，无任何其他业务表权限
2. 对所有组织级与项目级租户表开启 `ENABLE ROW LEVEL SECURITY` 与 `FORCE ROW LEVEL SECURITY`。
3. 策略采用 Fail-Closed 谓词：当 `current_setting('app.organization_id', true)` 或 `app.project_id` 为空时返回 FALSE。
4. 创建 `resolve_execution_tenant_context(UUID, UUID)` `SECURITY DEFINER` 函数并仅授权给 `etl_runtime`。

Revision ID: 0002_rls_policies
Revises: 0001_initial_schema
Create Date: 2026-11-22 22:26:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.shared.models import ORG_SCOPED_RLS_TABLES, PROJECT_SCOPED_RLS_TABLES

revision: str = "0002_rls_policies"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def build_org_rls_sql(table_name: str) -> list[str]:
    """生成组织级租户表的 ENABLE/FORCE RLS 与 Fail-Closed 隔离策略 SQL。"""
    policy_name = f"tenant_org_isolation_{table_name}"
    predicate = (
        "current_setting('app.organization_id', true) IS NOT NULL "
        "AND current_setting('app.organization_id', true) <> '' "
        "AND organization_id = current_setting('app.organization_id', true)::uuid"
    )
    return [
        f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY;",
        f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY;",
        f"DROP POLICY IF EXISTS {policy_name} ON {table_name};",
        (
            f"CREATE POLICY {policy_name} ON {table_name} "
            f"USING ({predicate}) WITH CHECK ({predicate});"
        ),
    ]


def build_project_rls_sql(table_name: str) -> list[str]:
    """生成项目级租户表的 ENABLE/FORCE RLS 与 Fail-Closed 隔离策略 SQL。"""
    policy_name = f"tenant_project_isolation_{table_name}"
    predicate = (
        "current_setting('app.organization_id', true) IS NOT NULL "
        "AND current_setting('app.organization_id', true) <> '' "
        "AND current_setting('app.project_id', true) IS NOT NULL "
        "AND current_setting('app.project_id', true) <> '' "
        "AND organization_id = current_setting('app.organization_id', true)::uuid "
        "AND project_id = current_setting('app.project_id', true)::uuid"
    )
    return [
        f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY;",
        f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY;",
        f"DROP POLICY IF EXISTS {policy_name} ON {table_name};",
        (
            f"CREATE POLICY {policy_name} ON {table_name} "
            f"USING ({predicate}) WITH CHECK ({predicate});"
        ),
    ]


RESOLVE_EXECUTION_TENANT_CONTEXT_SQL = """
CREATE OR REPLACE FUNCTION resolve_execution_tenant_context(
    p_execution_id UUID,
    p_command_id UUID
)
RETURNS TABLE(organization_id UUID, project_id UUID)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    RETURN QUERY
    SELECT er.organization_id, er.project_id
    FROM execution_runs er
    JOIN execution_commands ec ON ec.execution_id = er.id
    WHERE er.id = p_execution_id
      AND ec.command_id = p_command_id;
END;
$$;
"""


ROLE_SETUP_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'etl_migrator') THEN
        CREATE ROLE etl_migrator LOGIN PASSWORD 'etl_migrator_local_dev' NOSUPERUSER CREATEDB CREATEROLE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'etl_runtime') THEN
        CREATE ROLE etl_runtime LOGIN PASSWORD 'etl_runtime_local_dev' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    ELSE
        ALTER ROLE etl_runtime NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'etl_outbox_dispatcher') THEN
        CREATE ROLE etl_outbox_dispatcher LOGIN PASSWORD 'etl_outbox_local_dev' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    ELSE
        ALTER ROLE etl_outbox_dispatcher NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
END
$$;
"""


def upgrade() -> None:
    """在 PostgreSQL 上启用全量租户表 RLS、创建受控上下文派生函数并下发最小权限。"""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # 1. 创建或加固三类数据库角色
    op.execute(ROLE_SETUP_SQL)

    # 2. 为所有组织级表启用 ENABLE + FORCE RLS
    for table in ORG_SCOPED_RLS_TABLES:
        for stmt in build_org_rls_sql(table):
            op.execute(stmt)

    # 3. 为所有项目级表启用 ENABLE + FORCE RLS
    for table in PROJECT_SCOPED_RLS_TABLES:
        for stmt in build_project_rls_sql(table):
            op.execute(stmt)

    # 4. 为 etl_outbox_dispatcher 在 outbox_events 上授予只读策略，使其无需 BYPASSRLS 即可扫描待投递事件
    op.execute("DROP POLICY IF EXISTS outbox_dispatcher_read_events ON outbox_events;")
    op.execute(
        "CREATE POLICY outbox_dispatcher_read_events ON outbox_events "
        "FOR SELECT TO etl_outbox_dispatcher USING (true);"
    )

    # 5. 创建后台 Worker 租户上下文派生 SECURITY DEFINER 函数并锁定执行权限
    op.execute(RESOLVE_EXECUTION_TENANT_CONTEXT_SQL)
    op.execute("REVOKE ALL ON FUNCTION resolve_execution_tenant_context(UUID, UUID) FROM PUBLIC;")
    op.execute("GRANT EXECUTE ON FUNCTION resolve_execution_tenant_context(UUID, UUID) TO etl_runtime;")

    # 6. 授予 etl_runtime 与 etl_outbox_dispatcher 最小表级权限
    op.execute("GRANT USAGE ON SCHEMA public TO etl_runtime, etl_outbox_dispatcher;")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO etl_runtime;")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM etl_outbox_dispatcher;")
    op.execute("GRANT SELECT ON TABLE outbox_events TO etl_outbox_dispatcher;")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE outbox_deliveries TO etl_outbox_dispatcher;")


def downgrade() -> None:
    """卸载 RLS 策略与上下文派生函数。"""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("DROP FUNCTION IF EXISTS resolve_execution_tenant_context(UUID, UUID);")
    op.execute("DROP POLICY IF EXISTS outbox_dispatcher_read_events ON outbox_events;")

    for table in PROJECT_SCOPED_RLS_TABLES:
        policy_name = f"tenant_project_isolation_{table}"
        op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    for table in ORG_SCOPED_RLS_TABLES:
        policy_name = f"tenant_org_isolation_{table}"
        op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

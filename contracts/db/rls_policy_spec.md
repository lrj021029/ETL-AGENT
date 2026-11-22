# Layer 0 契约：PostgreSQL RLS 策略与连接池上下文规范

- **Contract Version**：`1.0.0`
- **Owner**：Security / Harness + Backend / IAM（Lead 审核冻结）

## 1. 数据库角色与权限矩阵

```sql
-- 1. Migration & Table Owner Role (仅 Alembic 使用)
CREATE ROLE etl_migrator LOGIN PASSWORD '...' NOSUPERUSER CREATEDB CREATEROLE;

-- 2. Application & Worker Runtime Role (绝不拥有表，绝无 BYPASSRLS)
CREATE ROLE etl_runtime LOGIN PASSWORD '...' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

-- 3. Outbox Dispatcher Role (仅能操作 outbox_events & outbox_deliveries)
CREATE ROLE etl_outbox_dispatcher LOGIN PASSWORD '...' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
```

## 2. 事务级上下文注入（Fail-Closed）

所有 FastAPI 请求与 Celery Worker 业务事务开启后必须立即执行：

```sql
SELECT set_config('app.organization_id', :org_id, true);
SELECT set_config('app.project_id', :project_id, true);
SELECT set_config('app.actor_id', :actor_id, true);
```

- `is_local = true` 确保连接归还池后配置自动清除。
- 未设置上下文时，`current_setting('app.organization_id', true)` 返回 `NULL` 或 `''`，RLS 谓词求值为 `FALSE`，表查询返回 0 行、写入被 PostgreSQL 直接拒绝。

## 3. 标准 RLS 策略模板

### 组织级表（如 `projects`, `organization_members`）
```sql
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE projects FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_org_isolation ON projects
  USING (
    current_setting('app.organization_id', true) IS NOT NULL
    AND current_setting('app.organization_id', true) <> ''
    AND organization_id = current_setting('app.organization_id', true)::uuid
  )
  WITH CHECK (
    current_setting('app.organization_id', true) IS NOT NULL
    AND current_setting('app.organization_id', true) <> ''
    AND organization_id = current_setting('app.organization_id', true)::uuid
  );
```

### 项目级表（如 `data_connections`, `pipeline_versions`, `preparations`, `execution_runs`）
```sql
ALTER TABLE data_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_connections FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_project_isolation ON data_connections
  USING (
    current_setting('app.organization_id', true) <> ''
    AND current_setting('app.project_id', true) <> ''
    AND organization_id = current_setting('app.organization_id', true)::uuid
    AND project_id = current_setting('app.project_id', true)::uuid
  )
  WITH CHECK (
    current_setting('app.organization_id', true) <> ''
    AND current_setting('app.project_id', true) <> ''
    AND organization_id = current_setting('app.organization_id', true)::uuid
    AND project_id = current_setting('app.project_id', true)::uuid
  );
```

## 4. 后台 Worker 安全上下文派生函数

```sql
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

REVOKE ALL ON FUNCTION resolve_execution_tenant_context(UUID, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_execution_tenant_context(UUID, UUID) TO etl_runtime;
```

## 5. 强制负向测试清单（必须使用真实 PostgreSQL 容器）
1. 缺失 `app.organization_id` 或 `app.project_id` 时 `SELECT` 返回 0 行、`INSERT` 报错。
2. 跨组织读取其他 Org 的 `projects` 返回 0 行。
3. 同组织但跨 `project_id` 读取其他 Project 资源返回 0 行。
4. 同一 SQLAlchemy 连接池连接先服务租户 A 提交后，再无上下文或切换租户 B 查询，严禁出现租户 A 数据。
5. `SELECT rolbypassrls FROM pg_roles WHERE rolname = 'etl_runtime'` 必须为 `false`，且 `etl_runtime` 不拥有任何 `public` 业务表。

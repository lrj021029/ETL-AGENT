# ADR-002: PostgreSQL RLS 双重多租户隔离与后台任务上下文模型

- **状态**：APPROVED（2026-11-22）
- **决策者**：用户 + Principal Architect + Security

## 背景
平台采用 `User ↔ OrganizationMember ↔ Organization → Project ↔ ProjectMember` 多租户模型，并要求应用层 + PostgreSQL RLS 双重保护。开启 `FORCE ROW LEVEL SECURITY` 后，跨租户的 `Outbox Dispatcher` 与 `Celery Worker` 必须在严禁赋予普通运行角色 `BYPASSRLS` 的前提下安全获取租户上下文。

## 决策
1. **三类数据库角色物理分离**：
   - `etl_migrator`：仅用于 Alembic 迁移和表结构拥有者（Table Owner），应用运行时严禁使用。
   - `etl_runtime`：FastAPI 与 Celery Worker 常规查询使用。**不拥有任何业务表，明确 `NOBYPASSRLS`**。
   - `etl_outbox_dispatcher`：最小权限角色，**仅授予** `outbox_events` 与 `outbox_deliveries` 的 `SELECT/INSERT/UPDATE` 权限，无任何业务表读取权限。
2. **事务级租户上下文**：
   - 每个请求/任务在打开数据库事务后立即执行：
     - `SELECT set_config('app.organization_id', :org_id, true);`
     - `SELECT set_config('app.project_id', :project_id, true);`
     - `SELECT set_config('app.actor_id', :actor_id, true);`
   - 第三参数 `true`（等价 `SET LOCAL`）保证变量严格限定在当前事务内，连接归还连接池后自动清零，杜绝串租户。
3. **RLS 策略规则**：
   - 全部租户业务表启用 `ENABLE ROW LEVEL SECURITY` 与 `FORCE ROW LEVEL SECURITY`。
   - 若 `current_setting('app.organization_id', true)` 为空或无法匹配，策略返回 `FALSE`（默认拒绝，fail-closed）。
4. **后台 Worker 租户上下文派生（禁止信任消息体 `tenant_id`）**：
   - 定义受限 `SECURITY DEFINER` 函数 `resolve_execution_tenant_context(p_execution_id UUID, p_command_id UUID)`（仅暴露最小 `(organization_id, project_id)` 元组，归属 `etl_migrator`，仅授予 `etl_runtime` 执行权）。
   - Celery Worker 收到消息后，调用该函数解析已持久化的 `(organization_id, project_id)`，随即在业务事务内 `SET LOCAL` 设置上下文，并再次通过常规 RLS 查验 `execution_runs`、`execution_commands` 与 `capability_consumptions`。
5. **LangGraph Checkpoint 隔离**：
   - 每次创建/读取/Resume `thread_id` 前，必须先经受 RLS 保护的 `agent_threads` 表验证 `(user_id, organization_id, project_id, thread_id)` 归属，禁止直接按外部传入 `thread_id` 读取 checkpoint 表。

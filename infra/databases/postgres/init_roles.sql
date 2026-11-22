-- =============================================================================
-- 控制面 PostgreSQL 16 角色与权限初始化脚本 (infra/databases/postgres/init_roles.sql)
-- 遵循 ADR-002 与 contracts/db/rls_policy_spec.md 三角色物理隔离规范：
--   1. etl_migrator          : 仅用于 Alembic 迁移与表拥有者 (Table Owner)
--   2. etl_runtime           : FastAPI 与 Celery Worker 运行时角色 (强制 NOBYPASSRLS)
--   3. etl_outbox_dispatcher : Outbox 投递器最小权限角色 (强制 NOBYPASSRLS)
-- =============================================================================

-- 启用 UUID 与密码学扩展
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

DO $$
BEGIN
    -- 1. 创建迁移与表结构拥有者角色（仅 Alembic 迁移使用，应用运行时严禁使用）
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'etl_migrator') THEN
        CREATE ROLE etl_migrator LOGIN PASSWORD 'dev_only_migrator_placeholder_not_for_prod'
            NOSUPERUSER CREATEDB CREATEROLE NOBYPASSRLS;
    END IF;

    -- 2. 创建应用与 Worker 运行时角色（绝不拥有业务表，严格强制 NOBYPASSRLS）
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'etl_runtime') THEN
        CREATE ROLE etl_runtime LOGIN PASSWORD 'dev_only_runtime_placeholder_not_for_prod'
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    ELSE
        ALTER ROLE etl_runtime NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;

    -- 3. 创建 Outbox Dispatcher 最小权限角色（仅允许操作 outbox_events 与 outbox_deliveries）
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'etl_outbox_dispatcher') THEN
        CREATE ROLE etl_outbox_dispatcher LOGIN PASSWORD 'dev_only_outbox_placeholder_not_for_prod'
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    ELSE
        ALTER ROLE etl_outbox_dispatcher NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
END
$$;

-- 授予数据库与 public Schema 基础权限
GRANT CONNECT ON DATABASE etl_control_plane TO etl_migrator, etl_runtime, etl_outbox_dispatcher;
GRANT USAGE, CREATE ON SCHEMA public TO etl_migrator;
GRANT USAGE ON SCHEMA public TO etl_runtime, etl_outbox_dispatcher;

-- 确保 public Schema 下由 etl_migrator 创建的表默认授予 etl_runtime 基础 CRUD 权限（受 RLS 策略严格约束）
ALTER DEFAULT PRIVILEGES FOR ROLE etl_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO etl_runtime;

ALTER DEFAULT PRIVILEGES FOR ROLE etl_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO etl_runtime, etl_outbox_dispatcher;

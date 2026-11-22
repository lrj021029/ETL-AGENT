"""Alembic 迁移运行环境配置。

仅使用 `etl_migrator` 角色执行表结构变更与 RLS 策略部署，
应用运行时（`etl_runtime`）严禁执行 DDL。
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.shared.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_migrator_url() -> str:
    """优先从全局配置读取 etl_migrator 数据库连接串。"""
    settings = get_settings()
    return settings.migrator_database_url or config.get_main_option("sqlalchemy.url", "")


def run_migrations_offline() -> None:
    """以离线模式（生成 SQL 脚本）运行迁移。"""
    url = get_migrator_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """以在线模式连接数据库运行迁移。"""
    cfg_section = config.get_section(config.config_ini_section, {})
    cfg_section["sqlalchemy.url"] = get_migrator_url()
    connectable = engine_from_config(
        cfg_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

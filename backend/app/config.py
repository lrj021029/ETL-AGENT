"""控制面应用全局配置模块。

依据 docs/implementation/DECISION_REGISTER.md 将所有 [CONFIGURABLE/TBD] 参数显式配置化，
禁止在业务代码中硬编码伪称已锁定的业务阈值。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """FastAPI 控制面、Celery Worker 与 Outbox Dispatcher 的集中配置类。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    service_name: str = "etl-agent-control-plane"
    service_version: str = "1.0.0"
    environment: Literal["dev", "staging", "prod", "test"] = "dev"

    # 数据库三角色物理分离连接串（遵循 ADR-002）
    # etl_migrator：仅用于 Alembic 迁移和表结构拥有者（Table Owner），运行时严禁使用
    migrator_database_url: str = Field(
        default="postgresql+psycopg://etl_migrator:etl_migrator_local_dev@127.0.0.1:5432/etl_agent"
    )
    # etl_runtime：FastAPI 与 Celery Worker 常规查询角色（无表所有权，强制 NOBYPASSRLS）
    runtime_database_url: str = Field(
        default="postgresql+psycopg://etl_runtime:etl_runtime_local_dev@127.0.0.1:5432/etl_agent"
    )
    # etl_outbox_dispatcher：Outbox 投递器最小权限角色（仅允许访问 outbox_events 与 outbox_deliveries）
    outbox_database_url: str = Field(
        default="postgresql+psycopg://etl_outbox_dispatcher:etl_outbox_local_dev@127.0.0.1:5432/etl_agent"
    )

    # Redis 与 Celery 消息队列配置
    redis_url: str = Field(default="redis://127.0.0.1:6379/0")
    celery_broker_url: str = Field(default="redis://127.0.0.1:6379/1")
    celery_result_backend: str = Field(default="redis://127.0.0.1:6379/2")

    # Vault 与 SecretProvider 配置（遵循 DEC-003）
    vault_addr: str = Field(default="http://127.0.0.1:8200")
    vault_token: str = Field(default="")
    secret_provider_type: Literal["VAULT_KV2", "ENCRYPTED_PG", "MEMORY"] = "ENCRYPTED_PG"
    secret_encryption_key: str = Field(
        default="local-dev-fernet-fallback-key-32bytes-seed!!"
    )

    # JWT 与 Email OTP（遵循 DEC-009：配本地 Mailpit SMTP 捕获，界面与文档明确标注为“邮件二次验证”）
    jwt_secret_key: str = Field(
        default="etl-agent-local-dev-jwt-secret-change-in-production-32b"
    )
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 900  # Access Token 有效期 15 分钟
    refresh_token_ttl_seconds: int = 604800  # Refresh Token 有效期 7 天
    otp_ttl_seconds: int = 300  # 邮件二次验证 OTP 有效期 5 分钟
    otp_max_attempts: int = 5
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    smtp_from_email: str = "no-reply@etl-agent.local"
    smtp_enabled: bool = False  # 当 Mailpit 容器就绪时开启真实 SMTP 发送

    # MinIO / S3 与本地文件受控沙箱目录
    minio_endpoint: str = "http://127.0.0.1:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "etl-agent-artifacts"
    allowed_upload_root: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[1] / "data" / "uploads"
    )
    max_upload_size_bytes: int = 50 * 1024 * 1024  # 50 MB 上限 [CONFIGURABLE/TBD]

    # SeaTunnel Zeta 引擎 REST 地址
    seatunnel_rest_url: str = "http://127.0.0.1:5801"

    # [CONFIGURABLE/TBD] 可配置策略参数（遵循 DECISION_REGISTER.md §2）
    # PRD-TBD-06 / 10：Metadata 样本行数上限、保留期与发布历史保留天数
    metadata_sample_max_rows: int = 20
    metadata_sample_retention_days: int = 7
    metadata_classification_rule_version: str = "v1.0.0"
    published_dataset_retention_days: int = 30

    # PRD-TBD-08：Reject Rate 与运行监督默认安全参数（>20% 触发已确认，恰好 20% 不触发）
    supervision_max_reject_rate_total: float = 0.20
    supervision_max_reject_rate_window: float = 0.25
    supervision_min_validated_rows: int = 50
    supervision_max_error_rows_absolute: int = 10000

    # PRD-TBD-11：Schedule 默认并发、错过策略与版本绑定策略
    schedule_default_concurrency_policy: Literal["FORBID", "ALLOW", "REPLACE"] = "FORBID"
    schedule_default_misfire_policy: Literal["SKIP", "FIRE_ONCE", "CATCH_UP"] = "SKIP"
    schedule_default_version_binding: Literal["PINNED", "LATEST_APPROVED"] = "PINNED"

    # PRD-TBD-14：执行预算数值、最大 SQL 时长、自动修复轮数上限
    default_max_repair_attempts: int = 3
    default_max_sql_timeout_seconds: int = 30
    default_max_read_rows: int = 5_000_000
    default_max_write_bytes: int = 5 * 1024 * 1024 * 1024

    # Outbox Dispatcher 轮询与租约参数
    outbox_batch_size: int = 50
    outbox_lease_seconds: int = 30
    outbox_max_attempts: int = 5


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取单例全局配置对象。"""
    return Settings()

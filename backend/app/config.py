"""控制面应用全局配置模块。

依据 docs/implementation/DECISION_REGISTER.md 将所有 [CONFIGURABLE/TBD] 参数显式配置化，
禁止在业务代码中硬编码伪称已锁定的业务阈值。
同时支持容器内（Docker Compose 网络主机名）与 Windows 宿主机原生调试（自动映射至 127.0.0.1）。
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _REPO_ROOT / ".env"
_VAULT_INIT_FILE = _REPO_ROOT / "infra" / "vault" / "init" / "vault-init.json"


def _running_inside_docker() -> bool:
    """判断当前进程是否运行在 Linux 容器内部。"""
    return Path("/.dockerenv").exists() or os.getenv("RUNNING_IN_DOCKER", "").lower() in {"1", "true"}


def _rewrite_host_if_local(value: str, mapping: dict[str, str]) -> str:
    """当进程运行在 Windows 宿主机而非 Docker 内网时，自动将 Compose 服务名映射为 127.0.0.1。"""
    if _running_inside_docker():
        return value
    updated = value
    for src, dst in mapping.items():
        updated = updated.replace(src, dst)
    return updated


class Settings(BaseSettings):
    """FastAPI 控制面、Celery Worker 与 Outbox Dispatcher 的集中配置类。"""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE.is_file() else ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    service_name: str = "etl-agent-control-plane"
    service_version: str = "1.0.0"
    environment: Literal["dev", "staging", "prod", "test", "local"] = Field(
        default="dev",
        validation_alias=AliasChoices("ENVIRONMENT", "APP_ENV"),
    )

    # 数据库三角色物理分离连接串（遵循 ADR-002，同时兼容 .env 中的 DATABASE_* 与 *_DATABASE_URL）
    # etl_migrator：仅用于 Alembic 迁移和表结构拥有者（Table Owner），运行时严禁使用
    migrator_database_url: str = Field(
        default="postgresql+psycopg://etl_migrator:dev_only_migrator_placeholder_not_for_prod@127.0.0.1:5432/etl_control_plane",
        validation_alias=AliasChoices("MIGRATOR_DATABASE_URL", "DATABASE_MIGRATOR_URL"),
    )
    # etl_runtime：FastAPI 与 Celery Worker 常规查询角色（无表所有权，强制 NOBYPASSRLS）
    runtime_database_url: str = Field(
        default="postgresql+psycopg://etl_runtime:dev_only_runtime_placeholder_not_for_prod@127.0.0.1:5432/etl_control_plane",
        validation_alias=AliasChoices("RUNTIME_DATABASE_URL", "DATABASE_RUNTIME_URL"),
    )
    # etl_outbox_dispatcher：Outbox 投递器最小权限角色（仅允许访问 outbox_events 与 outbox_deliveries）
    outbox_database_url: str = Field(
        default="postgresql+psycopg://etl_outbox_dispatcher:dev_only_outbox_placeholder_not_for_prod@127.0.0.1:5432/etl_control_plane",
        validation_alias=AliasChoices("OUTBOX_DATABASE_URL", "DATABASE_OUTBOX_URL"),
    )

    # Redis 与 Celery 消息队列配置
    redis_url: str = Field(
        default="redis://:dev_only_redis_placeholder_not_for_prod@127.0.0.1:6379/0"
    )
    celery_broker_url: str = Field(
        default="redis://:dev_only_redis_placeholder_not_for_prod@127.0.0.1:6379/1"
    )
    celery_result_backend: str = Field(
        default="redis://:dev_only_redis_placeholder_not_for_prod@127.0.0.1:6379/2"
    )

    # Vault 与 SecretProvider 配置（遵循 DEC-003，默认持久化模式 + Transit Ed25519）
    vault_addr: str = Field(default="http://127.0.0.1:8200")
    vault_token: str = Field(default="")
    vault_kv_mount: str = Field(default="secret")
    vault_transit_mount: str = Field(default="transit")
    vault_capability_key_name: str = Field(default="etl-agent-capability")
    vault_evidence_key_name: str = Field(default="etl-agent-evidence")
    secret_provider_type: Literal["VAULT_KV2", "ENCRYPTED_PG", "MEMORY"] = "VAULT_KV2"
    secret_encryption_key: str = Field(
        default="local-dev-fernet-fallback-key-32bytes-seed!!",
        validation_alias=AliasChoices("SECRET_ENCRYPTION_KEY", "SECRET_PROVIDER_FALLBACK_KEY"),
    )

    # 外部 OpenAI-Compatible LLM 配置（严禁在此写明文 API Key，真实 Key 仅从 Vault KV v2 读取）
    llm_provider_mode: Literal["openai_compatible", "deterministic_mock"] = Field(
        default="openai_compatible"
    )
    llm_openai_base_url: str = Field(default="https://100milionseek.top/v1")
    llm_openai_model: str = Field(default="deepseek-v4.1-flash-free")
    llm_openai_api_key_secret_ref: str = Field(default="secret/data/llm/openai_api_key")

    # JWT 与 Email OTP（遵循 DEC-009：配本地 Mailpit SMTP 捕获，界面与文档明确标注为“邮件二次验证”）
    jwt_secret_key: str = Field(
        default="etl-agent-local-dev-jwt-secret-change-in-production-32b"
    )
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = Field(
        default=900,
        validation_alias=AliasChoices("ACCESS_TOKEN_TTL_SECONDS", "JWT_ACCESS_TTL_SECONDS"),
    )
    refresh_token_ttl_seconds: int = Field(
        default=604800,
        validation_alias=AliasChoices("REFRESH_TOKEN_TTL_SECONDS", "JWT_REFRESH_TTL_SECONDS"),
    )
    otp_ttl_seconds: int = Field(
        default=300,
        validation_alias=AliasChoices("OTP_TTL_SECONDS", "EMAIL_OTP_TTL_SECONDS"),
    )
    otp_max_attempts: int = 5
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    smtp_from_email: str = "noreply@etl-agent.local"
    smtp_enabled: bool = True  # 本地 Mailpit 容器就绪时默认开启真实 SMTP 投递

    # MinIO / S3 与本地文件受控沙箱目录
    minio_endpoint: str = "http://127.0.0.1:9000"
    minio_access_key: str = Field(
        default="etl_minio_admin",
        validation_alias=AliasChoices("MINIO_ACCESS_KEY", "MINIO_ROOT_USER"),
    )
    minio_secret_key: str = Field(
        default="dev_only_minio_placeholder_not_for_prod",
        validation_alias=AliasChoices("MINIO_SECRET_KEY", "MINIO_ROOT_PASSWORD"),
    )
    minio_bucket: str = Field(
        default="etl-agent-artifacts",
        validation_alias=AliasChoices("MINIO_BUCKET", "MINIO_DEFAULT_BUCKET"),
    )
    allowed_upload_root: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[1] / "data" / "uploads"
    )
    max_upload_size_bytes: int = 50 * 1024 * 1024  # 50 MB 上限 [CONFIGURABLE/TBD]

    # SeaTunnel Zeta 引擎 REST 地址
    seatunnel_rest_url: str = Field(
        default="http://127.0.0.1:5801",
        validation_alias=AliasChoices("SEATUNNEL_REST_URL", "SEATUNNEL_ZETA_REST_URL"),
    )

    # [CONFIGURABLE/TBD] 可配置策略参数（遵循 DECISION_REGISTER.md §2）
    metadata_sample_max_rows: int = 20
    metadata_sample_retention_days: int = Field(
        default=7,
        validation_alias=AliasChoices("METADATA_SAMPLE_RETENTION_DAYS", "POLICY_METADATA_SAMPLE_RETENTION_DAYS"),
    )
    metadata_classification_rule_version: str = "v1.0.0"
    published_dataset_retention_days: int = 30

    # PRD-TBD-08：Reject Rate 与运行监督默认安全参数（>20% 触发已确认，恰好 20% 不触发）
    supervision_max_reject_rate_total: float = Field(
        default=0.20,
        validation_alias=AliasChoices("SUPERVISION_MAX_REJECT_RATE_TOTAL", "POLICY_REJECT_RATE_THRESHOLD"),
    )
    supervision_max_reject_rate_window: float = 0.25
    supervision_min_validated_rows: int = Field(
        default=50,
        validation_alias=AliasChoices("SUPERVISION_MIN_VALIDATED_ROWS", "POLICY_REJECT_RATE_MIN_VALIDATED_ROWS"),
    )
    supervision_max_error_rows_absolute: int = Field(
        default=10000,
        validation_alias=AliasChoices("SUPERVISION_MAX_ERROR_ROWS_ABSOLUTE", "POLICY_MAX_ERROR_ROWS_ABSOLUTE"),
    )

    # PRD-TBD-11：Schedule 默认并发、错过策略与版本绑定策略
    schedule_default_concurrency_policy: Literal["FORBID", "ALLOW", "REPLACE"] = "FORBID"
    schedule_default_misfire_policy: Literal["SKIP", "FIRE_ONCE", "CATCH_UP"] = "SKIP"
    schedule_default_version_binding: Literal["PINNED", "LATEST_APPROVED"] = "PINNED"

    # PRD-TBD-14：执行预算数值、最大 SQL 时长、自动修复轮数上限
    default_max_repair_attempts: int = Field(
        default=3,
        validation_alias=AliasChoices("DEFAULT_MAX_REPAIR_ATTEMPTS", "POLICY_MAX_REPAIR_ATTEMPTS"),
    )
    default_max_sql_timeout_seconds: int = Field(
        default=30,
        validation_alias=AliasChoices("DEFAULT_MAX_SQL_TIMEOUT_SECONDS", "POLICY_MAX_SQL_EXECUTION_SECONDS"),
    )
    default_max_read_rows: int = 5_000_000
    default_max_write_bytes: int = 5 * 1024 * 1024 * 1024

    # Outbox Dispatcher 轮询与租约参数
    outbox_batch_size: int = 50
    outbox_lease_seconds: int = 30
    outbox_max_attempts: int = 5

    @model_validator(mode="after")
    def _normalize_local_runtime_endpoints(self) -> "Settings":
        """宿主机原生运行时自动将容器内网名映射为 127.0.0.1，并在本机持久化 Vault 就绪时自动加载初始化 Token。"""
        self.migrator_database_url = _rewrite_host_if_local(self.migrator_database_url, {"@postgres:": "@127.0.0.1:"})
        self.runtime_database_url = _rewrite_host_if_local(self.runtime_database_url, {"@postgres:": "@127.0.0.1:"})
        self.outbox_database_url = _rewrite_host_if_local(self.outbox_database_url, {"@postgres:": "@127.0.0.1:"})
        self.redis_url = _rewrite_host_if_local(self.redis_url, {"@redis:": "@127.0.0.1:", "//redis:": "//127.0.0.1:"})
        self.celery_broker_url = _rewrite_host_if_local(self.celery_broker_url, {"@redis:": "@127.0.0.1:", "//redis:": "//127.0.0.1:"})
        self.celery_result_backend = _rewrite_host_if_local(self.celery_result_backend, {"@redis:": "@127.0.0.1:", "//redis:": "//127.0.0.1:"})
        self.vault_addr = _rewrite_host_if_local(self.vault_addr, {"http://vault:": "http://127.0.0.1:"})
        self.minio_endpoint = _rewrite_host_if_local(self.minio_endpoint, {"http://minio:": "http://127.0.0.1:"})
        self.seatunnel_rest_url = _rewrite_host_if_local(self.seatunnel_rest_url, {"http://seatunnel-zeta:": "http://127.0.0.1:"})
        if not _running_inside_docker() and self.smtp_host == "mailpit":
            self.smtp_host = "127.0.0.1"

        # 若未显式注入有效 VAULT_TOKEN，则自动从本地被 .gitignore 忽略的持久化初始化文件读取
        if (not self.vault_token or "placeholder" in self.vault_token) and _VAULT_INIT_FILE.is_file():
            try:
                init_json = json.loads(_VAULT_INIT_FILE.read_text(encoding="utf-8-sig"))
                token = str(init_json.get("root_token") or "")
                if token:
                    self.vault_token = token
            except Exception:
                pass
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取单例全局配置对象。"""
    return Settings()

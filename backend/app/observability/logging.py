"""结构化 JSON 日志与强制密钥脱敏过滤器。

严格遵循 contracts/observability/telemetry_spec.md 第 2 节与第 4 节规范：
1. 注入 8 个统一关联字段（trace_id 等）。
2. 对所有敏感键名及敏感字符串内容（DSN、Bearer Token、PEM 私钥等）执行强制脱敏，替换为 "[REDACTED]"。
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any, Iterator

# 统一脱敏占位符
REDACTED_PLACEHOLDER = "[REDACTED]"

# 统一关联字段（来自 contracts/observability/telemetry_spec.md 第 2 节）
CORRELATION_FIELDS: tuple[str, ...] = (
    "trace_id",
    "organization_id",
    "project_id",
    "pipeline_id",
    "pipeline_version_id",
    "preparation_id",
    "execution_id",
    "external_job_id",
)

# 强制脱敏键名正则（不区分大小写）
SENSITIVE_KEY_PATTERN = re.compile(
    r"(password|passwd|secret|token|api_key|apikey|private_key|"
    r"authorization|cookie|access_key|secret_key|credential|dsn|connection_uri)",
    re.IGNORECASE,
)

# 字符串内容级脱敏正则列表
# 1. 数据库连接 URI（mysql://user:pass@、postgresql://user:pass@ 及带驱动前缀如 mysql+pymysql://）
_DB_URI_PATTERN = re.compile(
    r"((?:mysql|postgresql|postgres)(?:\+[a-zA-Z0-9_]+)?://)([^\s:@/]+):([^\s@/]+)@",
    re.IGNORECASE,
)
# 2. HTTP Bearer 认证令牌
_BEARER_PATTERN = re.compile(r"Bearer\s+[A-Za-z0-9._\-+/=]+", re.IGNORECASE)
# 3. 完整 PEM 私钥块（跨行）
_PEM_BLOCK_PATTERN = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
)
# 4. 单行或截断的 PEM 私钥头
_PEM_HEADER_PATTERN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")

# 基于 contextvars 的协程/线程安全关联上下文
_correlation_ctx: contextvars.ContextVar[dict[str, str | None]] = contextvars.ContextVar(
    "etl_observability_correlation_ctx",
    default={},
)

# logging.LogRecord 原生属性集合，用于区分用户传入的 extra 字段
_STANDARD_LOG_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {
    "message",
    "asctime",
}


def redact_string(value: str) -> str:
    """对字符串内容执行强制脱敏（DSN 凭据、Bearer Token、PEM 私钥）。"""
    if not value:
        return value
    masked = _PEM_BLOCK_PATTERN.sub(REDACTED_PLACEHOLDER, value)
    masked = _PEM_HEADER_PATTERN.sub(REDACTED_PLACEHOLDER, masked)
    masked = _DB_URI_PATTERN.sub(rf"\1{REDACTED_PLACEHOLDER}@", masked)
    masked = _BEARER_PATTERN.sub(f"Bearer {REDACTED_PLACEHOLDER}", masked)
    return masked


def redact_secrets(data: Any) -> Any:
    """递归遍历字典、列表、元组与字符串，执行键名与内容级强制脱敏。"""
    if isinstance(data, dict):
        sanitized: dict[Any, Any] = {}
        for k, v in data.items():
            key_str = str(k)
            if SENSITIVE_KEY_PATTERN.search(key_str):
                sanitized[k] = REDACTED_PLACEHOLDER
            else:
                sanitized[k] = redact_secrets(v)
        return sanitized
    if isinstance(data, list):
        return [redact_secrets(item) for item in data]
    if isinstance(data, tuple):
        return tuple(redact_secrets(item) for item in data)
    if isinstance(data, str):
        return redact_string(data)
    return data


def bind_correlation_context(**kwargs: str | None) -> dict[str, str | None]:
    """向当前上下文绑定关联标识（仅接受 CORRELATION_FIELDS 中的字段）。"""
    current = dict(_correlation_ctx.get())
    for field in CORRELATION_FIELDS:
        if field in kwargs and kwargs[field] is not None:
            current[field] = str(kwargs[field])
    _correlation_ctx.set(current)
    return current


def get_correlation_context() -> dict[str, str | None]:
    """获取当前上下文中已绑定的关联标识字典。"""
    return dict(_correlation_ctx.get())


def clear_correlation_context() -> None:
    """清空当前上下文中的关联标识。"""
    _correlation_ctx.set({})


@contextlib.contextmanager
def correlation_context(**kwargs: str | None) -> Iterator[dict[str, str | None]]:
    """上下文管理器：在作用域内临时绑定关联标识，退出时自动恢复原上下文。"""
    previous = _correlation_ctx.get()
    merged = dict(previous)
    for field in CORRELATION_FIELDS:
        if field in kwargs and kwargs[field] is not None:
            merged[field] = str(kwargs[field])
    token = _correlation_ctx.set(merged)
    try:
        yield merged
    finally:
        _correlation_ctx.reset(token)


class StructuredJsonFormatter(logging.Formatter):
    """结构化 JSON 日志格式化器，自动注入关联标识并执行强制脱敏。"""

    def __init__(self, service_name: str = "etl-agent") -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        """将 LogRecord 格式化为单行脱敏 JSON 字符串。"""
        raw_message = record.getMessage()
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "message": redact_string(raw_message),
        }

        # 注入上下文中的 8 个标准关联字段，record 中的同名属性优先级更高
        ctx = _correlation_ctx.get()
        for field in CORRELATION_FIELDS:
            val = getattr(record, field, None)
            if val is None:
                val = ctx.get(field)
            payload[field] = str(val) if val is not None else None

        # 提取并脱敏额外的结构化字段
        extras: dict[str, Any] = {}
        for key, value in record.__dict__.items():
            if key in _STANDARD_LOG_RECORD_ATTRS or key in CORRELATION_FIELDS:
                continue
            extras[key] = value
        if extras:
            payload["extra"] = redact_secrets(extras)

        if record.exc_info:
            exc_text = self.formatException(record.exc_info)
            payload["exception"] = redact_string(exc_text)

        return json.dumps(payload, ensure_ascii=False, default=str)


def get_logger(
    name: str,
    service_name: str = "etl-agent",
    level: int = logging.INFO,
) -> logging.Logger:
    """获取已配置 StructuredJsonFormatter 的结构化日志记录器。"""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    if not any(isinstance(getattr(h, "formatter", None), StructuredJsonFormatter) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredJsonFormatter(service_name=service_name))
        logger.addHandler(handler)
    logger.propagate = False
    return logger

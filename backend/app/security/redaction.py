"""日志、错误载荷与字典结构的敏感信息脱敏模块。"""
from __future__ import annotations

import logging
import re
from typing import Any

# 统一脱敏占位标记
REDACTED = "***REDACTED***"

# PEM 私钥块、连接串内嵌凭据、Bearer/Basic 头、键值对机密、JWS 令牌、Vault Token 等匹配正则
_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)", re.S)
_URI_CRED = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)([^/\s:@]*):([^@\s/]+)@")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/]+=*")
_KV = re.compile(
    r"(?i)(\b[\w.\-]*(?:password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|credential)[\w.\-]*\b[\"']?\s*[=:]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;&\"']+)"
)
_JWS = re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]*")
_VAULT_TOKEN = re.compile(r"\b(?:hvs|hvb|s)\.[A-Za-z0-9]{20,}\b")
_SENSITIVE_KEY = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|credential|authorization|"
    r"connection[_-]?(uri|string)|jdbc[_-]?url|dsn|capability_jws)"
)
# 受管密钥占位符（如 ${SECRET:SRC_PWD}）属于安全引用，不脱敏
_PLACEHOLDER = re.compile(r"^\$\{SECRET:[A-Za-z0-9_.\-/]+\}$")


def redact_text(text: str) -> str:
    """对自由文本中的密码、连接 URI、Bearer Token、PEM 私钥、JWS 与 Vault Token 进行脱敏。"""
    if not isinstance(text, str):
        return text
    text = _PEM.sub(REDACTED, text)
    text = _URI_CRED.sub(lambda m: f"{m.group(1)}{m.group(2)}:{REDACTED}@", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _JWS.sub(REDACTED, text)
    text = _VAULT_TOKEN.sub(REDACTED, text)

    def _kv(m: re.Match) -> str:
        # 保留 ${SECRET:...} 占位符原样，便于审计与排障
        if _PLACEHOLDER.match(m.group(2).strip("\"'")):
            return m.group(0)
        return m.group(1) + REDACTED

    return _KV.sub(_kv, text)


def redact(obj: Any) -> Any:
    """递归深拷贝并脱敏字典、列表、字符串或异常对象，绝不原地修改输入。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SENSITIVE_KEY.search(k):
                if isinstance(v, str) and _PLACEHOLDER.match(v):
                    out[k] = v
                else:
                    out[k] = REDACTED
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, BaseException):
        return redact_text(str(obj))
    return obj


class RedactingFilter(logging.Filter):
    """标准库 logging.Filter：自动清洗日志消息、格式化参数与异常堆栈中的明文密钥。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover
            msg = str(record.msg)
        record.msg = redact_text(msg)
        record.args = None
        if record.exc_info and record.exc_info[1] is not None:
            import traceback

            record.exc_text = redact_text("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        return True


def install_redaction(logger: logging.Logger | None = None) -> RedactingFilter:
    """在指定 Logger（默认根 Logger）及其所有 Handler 上挂载脱敏过滤器。"""
    f = RedactingFilter()
    target = logger or logging.getLogger()
    target.addFilter(f)
    for h in target.handlers:
        h.addFilter(f)
    return f


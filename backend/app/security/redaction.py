"""Secret redaction for logs, error payloads and dicts."""
from __future__ import annotations

import logging
import re
from typing import Any

REDACTED = "***REDACTED***"

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
_PLACEHOLDER = re.compile(r"^\$\{SECRET:[A-Za-z0-9_.\-/]+\}$")


def redact_text(text: str) -> str:
    if not isinstance(text, str):
        return text
    text = _PEM.sub(REDACTED, text)
    text = _URI_CRED.sub(lambda m: f"{m.group(1)}{m.group(2)}:{REDACTED}@", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _JWS.sub(REDACTED, text)
    text = _VAULT_TOKEN.sub(REDACTED, text)

    def _kv(m: re.Match) -> str:
        if _PLACEHOLDER.match(m.group(2).strip("\"'")):
            return m.group(0)
        return m.group(1) + REDACTED

    return _KV.sub(_kv, text)


def redact(obj: Any) -> Any:
    """Deep-redact dicts/lists/strings. Returns a new object; input is untouched."""
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
    """logging.Filter that scrubs message, args and exception text."""

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
    f = RedactingFilter()
    target = logger or logging.getLogger()
    target.addFilter(f)
    for h in target.handlers:
        h.addFilter(f)
    return f

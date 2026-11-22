"""外部 LLM 载荷脱敏 / 最小化。

发送给外部模型前必须经过本模块：
* 剔除密码、令牌、密钥等敏感键；
* 把 URI / 连接串 / 内嵌凭据替换为占位符；
* 未经批准的原始样本行（rows / sample_rows 等）整体丢弃，仅保留行数；
* 绝不外送 ${SECRET:...} 之外的任何凭据形态。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_SENSITIVE_KEY = re.compile(
    r"(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|authorization)", re.I
)
_ROW_KEYS = {"rows", "sample_rows", "raw_rows", "sample_data", "preview_rows", "records"}
_URI = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>]+")
_JDBC = re.compile(r"\bjdbc:[a-zA-Z0-9]+:[^\s\"'<>]+")
_INLINE_CRED = re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*[^\s&;,\"']+")
_PLACEHOLDER = re.compile(r"\$\{SECRET:[A-Z0-9_]+\}")


@dataclass
class RedactionReport:
    secret_keys_removed: int = 0
    uris_redacted: int = 0
    inline_credentials_redacted: int = 0
    row_sets_dropped: int = 0
    paths: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "secret_keys_removed": self.secret_keys_removed,
            "uris_redacted": self.uris_redacted,
            "inline_credentials_redacted": self.inline_credentials_redacted,
            "row_sets_dropped": self.row_sets_dropped,
        }


def _clean_str(s: str, rep: RedactionReport, path: str) -> str:
    if _PLACEHOLDER.fullmatch(s):
        return s  # 占位符本身不含机密，可保留
    out = _JDBC.sub(lambda m: (_bump(rep, "uri", path), "[REDACTED_URI]")[1], s)
    out = _URI.sub(lambda m: (_bump(rep, "uri", path), "[REDACTED_URI]")[1], out)
    out = _INLINE_CRED.sub(lambda m: (_bump(rep, "cred", path), m.group(1) + "=[REDACTED]")[1], out)
    return out


def _bump(rep: RedactionReport, kind: str, path: str) -> None:
    if kind == "uri":
        rep.uris_redacted += 1
    else:
        rep.inline_credentials_redacted += 1
    rep.paths.append(path)


def sanitize_for_llm(
    payload: Any, *, approved_row_access: bool = False, _rep: RedactionReport | None = None, _path: str = ""
) -> tuple[Any, RedactionReport]:
    """返回 (脱敏后载荷, 报告)。approved_row_access=True 仅表示行数据已获显式批准，
    此时行内字符串仍会逐项做 URI/凭据清洗。"""
    rep = _rep or RedactionReport()
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for k, v in payload.items():
            p = f"{_path}.{k}" if _path else str(k)
            if _SENSITIVE_KEY.search(str(k)):
                rep.secret_keys_removed += 1
                rep.paths.append(p)
                continue
            if str(k).lower() in _ROW_KEYS and not approved_row_access:
                rep.row_sets_dropped += 1
                rep.paths.append(p)
                out[f"{k}_count"] = len(v) if isinstance(v, (list, tuple)) else 0
                continue
            out[k] = sanitize_for_llm(v, approved_row_access=approved_row_access, _rep=rep, _path=p)[0]
        return out, rep
    if isinstance(payload, (list, tuple)):
        return [
            sanitize_for_llm(v, approved_row_access=approved_row_access, _rep=rep, _path=f"{_path}[{i}]")[0]
            for i, v in enumerate(payload)
        ], rep
    if isinstance(payload, str):
        return _clean_str(payload, rep, _path), rep
    return payload, rep

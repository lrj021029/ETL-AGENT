"""SeaTunnel HOCON 结构解析 / 校验器：语法、必要段落、零明文密钥。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pyhocon import ConfigFactory
from pyhocon.exceptions import ConfigException

ERROR_CODE = "HOCON_VALIDATION_FAILED"

_SECRET_PLACEHOLDER = re.compile(r"^\$\{SECRET:[A-Z0-9_]+\}$")
_SENSITIVE_KEY = re.compile(r"(password|passwd|pwd|secret|token|access[_-]?key|private[_-]?key|credential|api[_-]?key)", re.I)
_URI_WITH_CREDENTIALS = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@", re.I)
_INLINE_CRED_PARAM = re.compile(r"(password|pwd|secret|token)=[^&\s]+", re.I)


@dataclass(frozen=True)
class HoconIssue:
    code: str
    path: str
    detail: str


class HoconValidationError(Exception):
    code = ERROR_CODE

    def __init__(self, issues: list[HoconIssue]):
        self.issues = issues
        super().__init__("; ".join(f"{i.path}: {i.detail}" for i in issues))


def _walk(node: Any, path: str):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, node


def validate_hocon(text: str) -> list[HoconIssue]:
    """返回问题列表；空列表表示通过。"""
    issues: list[HoconIssue] = []
    try:
        conf = ConfigFactory.parse_string(text)
    except (ConfigException, Exception) as e:  # noqa: BLE001  语法错误一律视为失败
        return [HoconIssue(ERROR_CODE, "<root>", f"syntax error: {type(e).__name__}")]
    tree = conf.as_plain_ordered_dict()
    for section in ("env", "source", "sink"):
        if section not in tree:
            issues.append(HoconIssue(ERROR_CODE, section, "missing required section"))
    for section in ("source", "sink"):
        if section in tree and (not isinstance(tree[section], dict) or not tree[section]):
            issues.append(HoconIssue(ERROR_CODE, section, "must contain at least one plugin"))
    for path, value in _walk(tree, ""):
        key = path.rsplit(".", 1)[-1]
        if isinstance(value, str):
            is_ph = bool(_SECRET_PLACEHOLDER.match(value))
            if _SENSITIVE_KEY.search(key) and not is_ph:
                issues.append(HoconIssue("PLAINTEXT_SECRET", path, "sensitive key must use ${SECRET:NAME} placeholder"))
            elif "${" in value and not is_ph:
                issues.append(HoconIssue("INVALID_PLACEHOLDER", path, "only ${SECRET:NAME} placeholders allowed"))
            if _URI_WITH_CREDENTIALS.search(value) or _INLINE_CRED_PARAM.search(value):
                issues.append(HoconIssue("PLAINTEXT_SECRET", path, "credentials embedded in URI/value"))
        elif _SENSITIVE_KEY.search(key) and value is not None:
            issues.append(HoconIssue("PLAINTEXT_SECRET", path, "sensitive key must use placeholder"))
    return issues


def enforce_hocon(text: str) -> None:
    issues = validate_hocon(text)
    if issues:
        raise HoconValidationError(issues)


def secret_placeholders(text: str) -> set[str]:
    return set(re.findall(r"\$\{SECRET:[A-Z0-9_]+\}", text))

"""Harness 统一异常类型，携带契约标准错误码（见 contracts/openapi/error_codes.yaml）。"""
from __future__ import annotations

from .redaction import redact, redact_text


class HarnessError(Exception):
    """安全内核异常：初始化时自动对错误消息与详情字典执行敏感信息脱敏。"""

    def __init__(self, code: str, message: str = "", details: dict | None = None):
        self.code = code
        self.message = redact_text(message or code)
        self.details = redact(details or {})
        super().__init__(f"{code}: {self.message}")

    def to_payload(self) -> dict:
        """导出为已脱敏的结构化错误响应载荷。"""
        return {"code": self.code, "message": self.message, "details": self.details}


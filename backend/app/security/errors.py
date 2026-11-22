"""Harness error type carrying contract error codes (contracts/openapi/error_codes.yaml)."""
from __future__ import annotations

from .redaction import redact, redact_text


class HarnessError(Exception):
    def __init__(self, code: str, message: str = "", details: dict | None = None):
        self.code = code
        self.message = redact_text(message or code)
        self.details = redact(details or {})
        super().__init__(f"{code}: {self.message}")

    def to_payload(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}

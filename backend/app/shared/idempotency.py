"""API 幂等键（Idempotency-Key）校验与防冲突辅助模块。

用于 Commit、Schedule 触发与受管命令提交等关键副作用接口，
当相同 Idempotency-Key 携带不同请求负载时拒绝并返回 IDEMPOTENCY_CONFLICT (409)。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.shared.errors import AppError, ErrorCode


@dataclass
class IdempotencyRecord:
    """单次幂等请求指纹与响应快照。"""

    idempotency_key: str
    tenant_scope: str
    payload_sha256: str
    response_payload: dict[str, Any]
    status_code: int
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class IdempotencyGuard:
    """幂等键管理器，支持按租户作用域校验请求负载一致性并重放幂等响应。"""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], IdempotencyRecord] = {}

    @staticmethod
    def validate_key_format(idempotency_key: str | None) -> str:
        """校验 Idempotency-Key 长度与非空约束（契约要求 8~128 字符）。"""
        if not idempotency_key or not (8 <= len(idempotency_key.strip()) <= 128):
            raise AppError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "Idempotency-Key 请求头缺失或长度不在 8~128 字符范围内",
                detail={"idempotency_key": idempotency_key},
            )
        return idempotency_key.strip()

    @staticmethod
    def compute_payload_hash(payload: dict[str, Any] | None) -> str:
        """计算请求体的确定性 SHA256 摘要。"""
        normalized = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def check_or_store(
        self,
        tenant_scope: str,
        idempotency_key: str | None,
        payload: dict[str, Any] | None,
        response_payload: dict[str, Any] | None = None,
        status_code: int = 202,
    ) -> IdempotencyRecord | None:
        """检查是否已存在幂等记录；若存在且负载不同则抛出 IDEMPOTENCY_CONFLICT。"""
        clean_key = self.validate_key_format(idempotency_key)
        payload_hash = self.compute_payload_hash(payload)
        composite_key = (tenant_scope, clean_key)

        existing = self._records.get(composite_key)
        if existing is not None:
            if existing.payload_sha256 != payload_hash:
                raise AppError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "相同的 Idempotency-Key 被用于不同的请求负载",
                    detail={
                        "idempotency_key": clean_key,
                        "expected_hash": existing.payload_sha256,
                        "actual_hash": payload_hash,
                    },
                )
            return existing

        if response_payload is not None:
            record = IdempotencyRecord(
                idempotency_key=clean_key,
                tenant_scope=tenant_scope,
                payload_sha256=payload_hash,
                response_payload=response_payload,
                status_code=status_code,
            )
            self._records[composite_key] = record
            return record
        return None

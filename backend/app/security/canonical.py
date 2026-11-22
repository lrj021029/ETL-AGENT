"""Canonical JSON (RFC 8785 JCS subset) + SHA256 helpers (ADR-005)."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

GENESIS_PREVIOUS_HASH = "sha256:" + "0" * 64


def _normalize(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("NaN/Infinity not allowed in canonical JSON")
        if value == 0:
            return 0  # -0.0 -> 0
        if value.is_integer() and abs(value) < 2**53:
            return int(value)
        return value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError("canonical JSON object keys must be strings")
            out[k] = _normalize(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    raise TypeError(f"unsupported type for canonical JSON: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_digest(value: Any) -> str:
    return sha256_digest(canonical_bytes(value))


def event_hash(event_without_hash: Any, previous_hash: str) -> str:
    return sha256_digest(canonical_bytes(event_without_hash) + b"\n" + previous_hash.encode("utf-8"))

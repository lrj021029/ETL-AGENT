"""Canonical JSON（RFC 8785 JCS 子集）与 SHA256 摘要辅助函数（ADR-005）。"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

# 创世事件的前置哈希固定值（64 个零）
GENESIS_PREVIOUS_HASH = "sha256:" + "0" * 64


def _normalize(value: Any) -> Any:
    """递归规范化数值与容器：禁止 NaN/Infinity，-0.0 归一为 0，整值浮点转为整数。"""
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("NaN/Infinity not allowed in canonical JSON")
        if value == 0:
            return 0  # 将 -0.0 统一归一为整数 0
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
    """将任意 JSON 兼容对象编码为符合 RFC 8785 JCS 规范的 UTF-8 字节串。"""
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_digest(data: bytes) -> str:
    """计算字节串的 SHA256 摘要，返回带 `sha256:` 前缀的十六进制字符串。"""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_digest(value: Any) -> str:
    """对输入对象进行 Canonical JSON 序列化后计算 SHA256 摘要。"""
    return sha256_digest(canonical_bytes(value))


def event_hash(event_without_hash: Any, previous_hash: str) -> str:
    """按 ADR-005 布局计算证据账本事件哈希：SHA256(canonical_bytes(event) + '\\n' + previous_hash)。"""
    return sha256_digest(canonical_bytes(event_without_hash) + b"\n" + previous_hash.encode("utf-8"))


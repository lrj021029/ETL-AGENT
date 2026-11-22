"""RFC 8785 (JCS) 兼容的 Canonical JSON 序列化与 SHA256 摘要计算（遵循 contracts/crypto/canonicalization.md）。"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def _utf16_key(s: str) -> bytes:
    """按 UTF-16 码元顺序比较对象键（与 RFC 8785 / Unicode BMP 码点升序一致）。"""
    return s.encode("utf-16-be", errors="surrogatepass")


def _normalize(v: Any) -> Any:
    """递归规范化：排序键、拒绝 NaN/Infinity、将 -0.0 与整值浮点归一为整数。"""
    if isinstance(v, bool) or v is None or isinstance(v, (str, int)):
        return v
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            raise ValueError("Canonical JSON 禁止出现 NaN / Infinity")
        if v == 0:
            return 0  # -0.0 归一为 0
        if v.is_integer() and abs(v) < 1e21:
            return int(v)
        return v
    if isinstance(v, dict):
        out = {}
        for k in sorted(v.keys(), key=lambda k: _utf16_key(str(k))):
            if not isinstance(k, str):
                raise TypeError("Canonical JSON 对象的键必须为字符串")
            out[k] = _normalize(v[k])
        return out
    if isinstance(v, (list, tuple)):
        return [_normalize(x) for x in v]
    raise TypeError(f"Canonical JSON 不支持的类型: {type(v).__name__}")


def canonical_json(obj: Any) -> str:
    """将对象序列化为紧凑、确定性的 Canonical JSON 字符串。"""
    return json.dumps(
        _normalize(obj), ensure_ascii=False, separators=(",", ":"), allow_nan=False, sort_keys=False
    )


def canonical_bytes(obj: Any) -> bytes:
    """返回无 BOM 的 UTF-8 Canonical JSON 字节串。"""
    return canonical_json(obj).encode("utf-8")


def sha256_digest(obj: Any) -> str:
    """计算形如 'sha256:<hex>' 的 Canonical JSON 摘要。"""
    return "sha256:" + hashlib.sha256(canonical_bytes(obj)).hexdigest()


GENESIS_PREVIOUS_HASH = "sha256:" + "0" * 64


def evidence_event_hash(event_without_hash: Any, previous_hash: str) -> str:
    """按 Layer 0 契约计算证据账本事件链哈希。"""
    data = canonical_bytes(event_without_hash) + b"\n" + previous_hash.encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()

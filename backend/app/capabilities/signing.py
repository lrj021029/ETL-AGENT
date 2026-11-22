"""Ed25519 签名器（本地内存私钥 + Vault Transit 签名引擎适配）与公钥环（KeyRing）。所有签名均为 64 字节原始 Ed25519 签名。"""
from __future__ import annotations

import base64
from typing import Any, Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.security.errors import HarnessError


def b64u_encode(b: bytes) -> str:
    """按 JWS 规范编码为无 `=` 填充的 Base64URL 字符串。"""
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def b64u_decode(s: str) -> bytes:
    """严格校验并解码无填充 Base64URL 字符串。"""
    if not isinstance(s, str) or not all(c.isalnum() or c in "-_" for c in s):
        raise ValueError("invalid base64url")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Signer(Protocol):
    """统一签名器协议：暴露密钥版本标识 `kid` 并返回 64 字节原始 Ed25519 签名。"""

    kid: str

    def sign(self, data: bytes) -> bytes: ...


class KeyRing:
    """`kid -> Ed25519PublicKey` 公钥环：保留历史轮转密钥以支持旧令牌与历史证据检查点验签。"""

    def __init__(self) -> None:
        self._keys: dict[str, Ed25519PublicKey] = {}

    def add(self, kid: str, public_key: Ed25519PublicKey) -> None:
        self._keys[kid] = public_key

    def add_raw(self, kid: str, raw32: bytes) -> None:
        self._keys[kid] = Ed25519PublicKey.from_public_bytes(raw32)

    def add_vault_public_key(self, kid: str, vault_public_key_b64: str) -> None:
        """注册 Vault Transit 导出的 Ed25519 公钥（`keys.<ver>.public_key` 为 32 字节原始公钥的标准 Base64 编码）。"""
        self.add_raw(kid, base64.b64decode(vault_public_key_b64))

    def get(self, kid: str) -> Ed25519PublicKey:
        try:
            return self._keys[kid]
        except KeyError:
            raise KeyError(f"unknown kid {kid}") from None

    def __contains__(self, kid: str) -> bool:
        return kid in self._keys


class LocalEd25519Signer:
    """本地 Ed25519 签名器（用于开发、单测及离线校验）。"""

    def __init__(self, kid: str, private_key: Ed25519PrivateKey | None = None):
        self.kid = kid
        self._k = private_key or Ed25519PrivateKey.generate()

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._k.public_key()

    def public_raw(self) -> bytes:
        return self.public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def sign(self, data: bytes) -> bytes:
        return self._k.sign(data)

    def register(self, ring: KeyRing) -> None:
        ring.add(self.kid, self.public_key)


class TransitClient(Protocol):
    """Vault Transit 签名客户端协议。"""

    def sign(self, key_name: str, input_b64: str, key_version: int | None = None) -> str:
        """返回 Vault Transit `signature` 字段，格式为 `'vault:v1:<64字节原始签名的标准Base64>'`。"""


class HvacTransitClient:
    """将 `hvac.Client` 适配为 `TransitClient` 协议。"""

    def __init__(self, hvac_client: Any, mount_point: str = "transit"):
        self._c, self._m = hvac_client, mount_point

    def sign(self, key_name: str, input_b64: str, key_version: int | None = None) -> str:
        kw = {"key_version": key_version} if key_version else {}
        r = self._c.secrets.transit.sign_data(
            name=key_name, hash_input=input_b64, mount_point=self._m, **kw
        )
        return r["data"]["signature"]


def vault_signature_to_jws(vault_signature: str) -> bytes:
    """将 Vault Transit 返回的 `'vault:v<N>:<标准Base64>'` 解析并校验为 64 字节原始 Ed25519 签名。"""
    parts = vault_signature.split(":")
    if len(parts) != 3 or parts[0] != "vault" or not parts[1].startswith("v"):
        raise HarnessError("SECRET_PROVIDER_ERROR", "unexpected Vault Transit signature format")
    raw = base64.b64decode(parts[2], validate=True)
    if len(raw) != 64:
        raise HarnessError("SECRET_PROVIDER_ERROR", "Vault Transit Ed25519 signature must be 64 bytes")
    return raw


def vault_signature_to_b64url(vault_signature: str) -> str:
    """将 Vault Transit 签名字符串直接转换为标准 JWS `EdDSA` Base64URL 签名段。"""
    return b64u_encode(vault_signature_to_jws(vault_signature))


class VaultTransitEd25519Signer:
    """基于 Vault Transit 的 Ed25519 签名器（私钥永不出 Vault）：自动将 Vault 标准 Base64 原始签名转换为 JWS `EdDSA` 签名。"""

    def __init__(self, client: TransitClient, key_name: str, key_version: int = 1):
        self._c, self._name, self._ver = client, key_name, key_version
        self.kid = f"{key_name}:v{key_version}"

    def sign(self, data: bytes) -> bytes:
        try:
            sig = self._c.sign(self._name, base64.b64encode(data).decode("ascii"), self._ver)
        except HarnessError:
            raise
        except Exception as e:  # noqa: BLE001
            raise HarnessError("SECRET_PROVIDER_ERROR", f"vault transit sign failed: {type(e).__name__}") from None
        return vault_signature_to_jws(sig)


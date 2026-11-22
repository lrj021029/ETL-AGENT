"""Ed25519 signers (local + Vault Transit) and key ring. Signatures are raw 64-byte Ed25519."""
from __future__ import annotations

import base64
from typing import Any, Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.security.errors import HarnessError


def b64u_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def b64u_decode(s: str) -> bytes:
    if not isinstance(s, str) or not all(c.isalnum() or c in "-_" for c in s):
        raise ValueError("invalid base64url")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Signer(Protocol):
    kid: str

    def sign(self, data: bytes) -> bytes: ...


class KeyRing:
    """kid -> Ed25519 public key. Retains rotated keys so old tokens/checkpoints verify."""

    def __init__(self) -> None:
        self._keys: dict[str, Ed25519PublicKey] = {}

    def add(self, kid: str, public_key: Ed25519PublicKey) -> None:
        self._keys[kid] = public_key

    def add_raw(self, kid: str, raw32: bytes) -> None:
        self._keys[kid] = Ed25519PublicKey.from_public_bytes(raw32)

    def add_vault_public_key(self, kid: str, vault_public_key_b64: str) -> None:
        """Vault transit key `keys.<ver>.public_key` for ed25519 is base64 of raw 32 bytes."""
        self.add_raw(kid, base64.b64decode(vault_public_key_b64))

    def get(self, kid: str) -> Ed25519PublicKey:
        try:
            return self._keys[kid]
        except KeyError:
            raise KeyError(f"unknown kid {kid}") from None

    def __contains__(self, kid: str) -> bool:
        return kid in self._keys


class LocalEd25519Signer:
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
    def sign(self, key_name: str, input_b64: str, key_version: int | None = None) -> str:
        """Return Vault's `signature` field, e.g. 'vault:v1:<std-base64 of raw 64-byte sig>'."""


class HvacTransitClient:
    """Adapter from an hvac.Client to TransitClient."""

    def __init__(self, hvac_client: Any, mount_point: str = "transit"):
        self._c, self._m = hvac_client, mount_point

    def sign(self, key_name: str, input_b64: str, key_version: int | None = None) -> str:
        kw = {"key_version": key_version} if key_version else {}
        r = self._c.secrets.transit.sign_data(
            name=key_name, hash_input=input_b64, mount_point=self._m, **kw
        )
        return r["data"]["signature"]


def vault_signature_to_jws(vault_signature: str) -> bytes:
    """'vault:v<N>:<std b64>' -> raw 64-byte Ed25519 signature."""
    parts = vault_signature.split(":")
    if len(parts) != 3 or parts[0] != "vault" or not parts[1].startswith("v"):
        raise HarnessError("SECRET_PROVIDER_ERROR", "unexpected Vault Transit signature format")
    raw = base64.b64decode(parts[2], validate=True)
    if len(raw) != 64:
        raise HarnessError("SECRET_PROVIDER_ERROR", "Vault Transit Ed25519 signature must be 64 bytes")
    return raw


def vault_signature_to_b64url(vault_signature: str) -> str:
    return b64u_encode(vault_signature_to_jws(vault_signature))


class VaultTransitEd25519Signer:
    """Signs through Vault Transit (private key never leaves Vault). Converts Vault's
    standard-base64 raw signature into the Base64URL JWS `EdDSA` signature."""

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

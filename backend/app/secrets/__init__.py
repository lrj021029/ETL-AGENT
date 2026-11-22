"""SecretProvider abstraction: Vault KV v2 (default) and Fernet-encrypted PostgreSQL fallback."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app.security.errors import HarnessError


class SecretValue:
    """Holds a secret; repr/str are always masked."""

    __slots__ = ("_v", "version")

    def __init__(self, value: str, version: int):
        self._v = value
        self.version = version

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return f"SecretValue(version={self.version}, value={mask(self._v)})"

    __str__ = __repr__


def mask(value: str | None, keep: int = 0) -> str:
    if not value:
        return ""
    if keep and len(value) > keep * 2 + 4:
        return value[:keep] + "****" + value[-keep:]
    return "********"


@runtime_checkable
class SecretProvider(Protocol):
    def put(self, ref: str, value: str) -> int: ...
    def get(self, ref: str, version: int | None = None) -> SecretValue: ...
    def current_version(self, ref: str) -> int: ...
    def delete(self, ref: str) -> None: ...


class VaultKV2SecretProvider:
    """Vault KV v2 provider. `client` is an hvac.Client (or any object with the same
    `secrets.kv.v2` surface). Secret payload is stored under key `value`."""

    def __init__(self, client: Any, mount_point: str = "secret", prefix: str = "etl-agent"):
        self._c = client
        self._mount = mount_point
        self._prefix = prefix.strip("/")

    def _path(self, ref: str) -> str:
        return f"{self._prefix}/{ref.strip('/')}"

    def put(self, ref: str, value: str) -> int:
        try:
            r = self._c.secrets.kv.v2.create_or_update_secret(
                path=self._path(ref), secret={"value": value}, mount_point=self._mount
            )
            return int(r["data"]["version"])
        except Exception as e:  # noqa: BLE001
            raise HarnessError("SECRET_PROVIDER_ERROR", f"vault put failed: {type(e).__name__}") from None

    def get(self, ref: str, version: int | None = None) -> SecretValue:
        try:
            r = self._c.secrets.kv.v2.read_secret_version(
                path=self._path(ref), version=version, mount_point=self._mount, raise_on_deleted_version=True
            )
            return SecretValue(r["data"]["data"]["value"], int(r["data"]["metadata"]["version"]))
        except Exception as e:  # noqa: BLE001
            raise HarnessError("SECRET_PROVIDER_ERROR", f"vault get failed: {type(e).__name__}") from None

    def current_version(self, ref: str) -> int:
        try:
            r = self._c.secrets.kv.v2.read_secret_metadata(path=self._path(ref), mount_point=self._mount)
            return int(r["data"]["current_version"])
        except Exception as e:  # noqa: BLE001
            raise HarnessError("SECRET_PROVIDER_ERROR", f"vault metadata failed: {type(e).__name__}") from None

    def delete(self, ref: str) -> None:
        try:
            self._c.secrets.kv.v2.delete_metadata_and_all_versions(path=self._path(ref), mount_point=self._mount)
        except Exception as e:  # noqa: BLE001
            raise HarnessError("SECRET_PROVIDER_ERROR", f"vault delete failed: {type(e).__name__}") from None


@dataclass
class EncryptedRow:
    ref: str
    version: int
    ciphertext: bytes


class SecretRowStore(Protocol):
    def append(self, ref: str, ciphertext: bytes) -> int: ...
    def fetch(self, ref: str, version: int | None) -> EncryptedRow | None: ...
    def latest_version(self, ref: str) -> int | None: ...
    def remove(self, ref: str) -> None: ...


class InMemorySecretRowStore:
    def __init__(self) -> None:
        self._rows: dict[str, list[EncryptedRow]] = {}
        self._lock = threading.Lock()

    def append(self, ref: str, ciphertext: bytes) -> int:
        with self._lock:
            rows = self._rows.setdefault(ref, [])
            v = len(rows) + 1
            rows.append(EncryptedRow(ref, v, ciphertext))
            return v

    def fetch(self, ref: str, version: int | None) -> EncryptedRow | None:
        rows = self._rows.get(ref) or []
        if not rows:
            return None
        if version is None:
            return rows[-1]
        return rows[version - 1] if 1 <= version <= len(rows) else None

    def latest_version(self, ref: str) -> int | None:
        rows = self._rows.get(ref)
        return len(rows) if rows else None

    def remove(self, ref: str) -> None:
        self._rows.pop(ref, None)


class EncryptedPostgresSecretProvider:
    """Fernet (AES-128-CBC + HMAC-SHA256) encrypted secrets; ciphertext persisted via a
    SecretRowStore (bind to the Backend's `secrets` table in integration). The ref is bound
    as Fernet-wrapped associated data so ciphertext cannot be swapped between refs.
    Master key(s) are supplied via env/Vault; first key encrypts, all keys decrypt (rotation)."""

    def __init__(self, store: SecretRowStore, master_keys: list[bytes | str]):
        from cryptography.fernet import Fernet, MultiFernet

        if not master_keys:
            raise HarnessError("SECRET_PROVIDER_ERROR", "no master key configured")
        self._f = MultiFernet([Fernet(k) for k in master_keys])
        self._store = store

    @staticmethod
    def generate_key() -> bytes:
        from cryptography.fernet import Fernet

        return Fernet.generate_key()

    def put(self, ref: str, value: str) -> int:
        import json

        token = self._f.encrypt(json.dumps({"ref": ref, "value": value}).encode())
        return self._store.append(ref, token)

    def get(self, ref: str, version: int | None = None) -> SecretValue:
        import json

        from cryptography.fernet import InvalidToken

        row = self._store.fetch(ref, version)
        if row is None:
            raise HarnessError("SECRET_PROVIDER_ERROR", "secret not found")
        try:
            doc = json.loads(self._f.decrypt(row.ciphertext))
        except InvalidToken:
            raise HarnessError("SECRET_PROVIDER_ERROR", "secret decryption failed") from None
        if doc.get("ref") != ref:
            raise HarnessError("SECRET_PROVIDER_ERROR", "secret ref binding mismatch")
        return SecretValue(doc["value"], row.version)

    def current_version(self, ref: str) -> int:
        v = self._store.latest_version(ref)
        if v is None:
            raise HarnessError("SECRET_PROVIDER_ERROR", "secret not found")
        return v

    def delete(self, ref: str) -> None:
        self._store.remove(ref)

    def rotate_master_key(self, ref: str) -> int:
        """Re-encrypt latest version under the primary key (new version)."""
        return self.put(ref, self.get(ref).reveal())

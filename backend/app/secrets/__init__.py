"""SecretProvider 抽象与实现：Vault KV v2（默认生产实现）及 Fernet 加密 PostgreSQL 回退实现。"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app.security.errors import HarnessError


class SecretValue:
    """密钥值包装器：`repr`/`str` 始终输出掩码，仅允许通过 `reveal()` 显式解封明文。"""

    __slots__ = ("_v", "version")

    def __init__(self, value: str, version: int):
        self._v = value
        self.version = version

    def reveal(self) -> str:
        """显式读取密钥明文。"""
        return self._v

    def __repr__(self) -> str:
        return f"SecretValue(version={self.version}, value={mask(self._v)})"

    __str__ = __repr__


def mask(value: str | None, keep: int = 0) -> str:
    """对密钥字符串进行星号掩码显示。"""
    if not value:
        return ""
    if keep and len(value) > keep * 2 + 4:
        return value[:keep] + "****" + value[-keep:]
    return "********"


@runtime_checkable
class SecretProvider(Protocol):
    """统一密钥提供者协议：支持按版本存取、查询当前版本与删除。"""

    def put(self, ref: str, value: str) -> int: ...
    def get(self, ref: str, version: int | None = None) -> SecretValue: ...
    def current_version(self, ref: str) -> int: ...
    def delete(self, ref: str) -> None: ...


class VaultKV2SecretProvider:
    """基于 HashiCorp Vault KV v2 的密钥提供者；`client` 接收 `hvac.Client` 或兼容接口对象，密钥存储在字段 `value` 下。"""

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
    """加密密钥存储行：记录密钥引用标识、版本号与密文字节。"""

    ref: str
    version: int
    ciphertext: bytes


class SecretRowStore(Protocol):
    """底层密文持久化协议（集成阶段绑定至 PostgreSQL `secrets` 表）。"""

    def append(self, ref: str, ciphertext: bytes) -> int: ...
    def fetch(self, ref: str, version: int | None) -> EncryptedRow | None: ...
    def latest_version(self, ref: str) -> int | None: ...
    def remove(self, ref: str) -> None: ...


class InMemorySecretRowStore:
    """线程安全的内存密文行存储，用于确定性单元测试。"""

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
    """基于 Fernet（AES-128-CBC + HMAC-SHA256）的应用层加密密钥提供者。
    密文通过 `SecretRowStore` 持久化至数据库；`ref` 作为关联数据一并封装签名以防止跨引用密文替换攻击。
    主密钥由环境变量或 Vault 注入：`MultiFernet` 使用首把密钥加密，所有历史密钥均可解密以支持平滑轮转。"""

    def __init__(self, store: SecretRowStore, master_keys: list[bytes | str]):
        from cryptography.fernet import Fernet, MultiFernet

        if not master_keys:
            raise HarnessError("SECRET_PROVIDER_ERROR", "no master key configured")
        self._f = MultiFernet([Fernet(k) for k in master_keys])
        self._store = store

    @staticmethod
    def generate_key() -> bytes:
        """生成新的 Fernet 主密钥。"""
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
        """使用当前首选主密钥对最新版本重新加密并生成新版本号。"""
        return self.put(ref, self.get(ref).reveal())


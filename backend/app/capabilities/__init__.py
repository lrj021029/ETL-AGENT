"""Capability JWS (EdDSA) issuance + verification + replay guard."""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from typing import Any, Callable, Protocol

from app.security.errors import HarnessError

from .signing import (
    KeyRing, LocalEd25519Signer, Signer, VaultTransitEd25519Signer, b64u_decode, b64u_encode,
    vault_signature_to_b64url,
)

TTL_SECONDS = 300
ISS = "etl-agent-broker"
AUDIENCES = {"seatunnel-worker", "publish-adapter", "rollback-executor"}
TOOLS = {
    "seatunnel.submit_batch", "seatunnel.submit_cdc", "seatunnel.cancel_job", "doris.atomic_swap",
    "rollback.shadow_cleanup", "rollback.swap_restore", "rollback.cdc_recover",
}
TOOL_AUDIENCE = {
    "seatunnel.submit_batch": "seatunnel-worker", "seatunnel.submit_cdc": "seatunnel-worker",
    "seatunnel.cancel_job": "seatunnel-worker", "doris.atomic_swap": "publish-adapter",
    "rollback.shadow_cleanup": "rollback-executor", "rollback.swap_restore": "rollback-executor",
    "rollback.cdc_recover": "rollback-executor",
}
CLAIMS = (
    "iss", "sub", "aud", "jti", "iat", "exp", "organization_id", "project_id", "environment", "tool",
    "preparation_id", "pipeline_version_hash", "approval_digest", "policy_version", "nonce",
)
_SHA = re.compile(r"^sha256:[a-f0-9]{64}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
INVALID = "CAPABILITY_INVALID_SIGNATURE"
BOUND = ("aud", "tool", "organization_id", "project_id", "environment", "preparation_id",
         "pipeline_version_hash", "approval_digest", "policy_version", "sub")


def validate_claims(c: dict) -> None:
    """Strict validation equivalent to contracts/harness/capability_jws.schema.json."""
    if set(c) != set(CLAIMS):
        raise HarnessError(INVALID, "claim set mismatch")
    ok = (
        c["iss"] == ISS and isinstance(c["sub"], str) and c["sub"]
        and c["aud"] in AUDIENCES and c["tool"] in TOOLS and c["environment"] in {"dev", "staging", "prod"}
        and all(isinstance(c[k], str) and _UUID.match(c[k]) for k in ("jti", "organization_id", "project_id", "preparation_id"))
        and all(isinstance(c[k], str) and _SHA.match(c[k]) for k in ("pipeline_version_hash", "approval_digest"))
        and isinstance(c["policy_version"], str) and c["policy_version"]
        and isinstance(c["nonce"], str) and 16 <= len(c["nonce"]) <= 128
        and all(isinstance(c[k], int) and not isinstance(c[k], bool) for k in ("iat", "exp"))
    )
    if not ok:
        raise HarnessError(INVALID, "claims violate capability schema")
    if c["exp"] - c["iat"] > TTL_SECONDS or c["exp"] <= c["iat"]:
        raise HarnessError(INVALID, "ttl exceeds 300s")
    if TOOL_AUDIENCE[c["tool"]] != c["aud"]:
        raise HarnessError(INVALID, "tool/audience mismatch")


def _compact(obj: dict) -> str:
    return b64u_encode(json.dumps(obj, separators=(",", ":"), sort_keys=True).encode())


class CapabilityIssuer:
    def __init__(self, signer: Signer, clock: Callable[[], float] = time.time):
        self.signer, self._clock = signer, clock

    def issue(self, *, sub: str, tool: str, organization_id: str, project_id: str, environment: str,
              preparation_id: str, pipeline_version_hash: str, approval_digest: str, policy_version: str,
              ttl: int = TTL_SECONDS, jti: str | None = None, nonce: str | None = None) -> tuple[str, dict]:
        now = int(self._clock())
        claims = {
            "iss": ISS, "sub": sub, "aud": TOOL_AUDIENCE.get(tool, "?"), "jti": jti or str(uuid.uuid4()),
            "iat": now, "exp": now + min(ttl, TTL_SECONDS), "organization_id": organization_id,
            "project_id": project_id, "environment": environment, "tool": tool,
            "preparation_id": preparation_id, "pipeline_version_hash": pipeline_version_hash,
            "approval_digest": approval_digest, "policy_version": policy_version,
            "nonce": nonce or uuid.uuid4().hex,
        }
        validate_claims(claims)
        header = {"alg": "EdDSA", "typ": "JWT", "kid": self.signer.kid}
        signing_input = f"{_compact(header)}.{_compact(claims)}"
        sig = self.signer.sign(signing_input.encode("ascii"))
        return f"{signing_input}.{b64u_encode(sig)}", claims


class CapabilityVerifier:
    def __init__(self, keyring: KeyRing, clock: Callable[[], float] = time.time, leeway: int = 0):
        self.keyring, self._clock, self.leeway = keyring, clock, leeway

    def verify(self, token: str, expected: dict | None = None) -> dict:
        """Verify signature, schema, TTL and (optionally) binding to `expected` context values
        (any subset of BOUND keys). Replay is enforced separately (ReplayGuard / PG unique jti)."""
        try:
            h64, c64, s64 = token.split(".")
            header = json.loads(b64u_decode(h64))
            claims = json.loads(b64u_decode(c64))
            sig = b64u_decode(s64)
        except Exception:  # noqa: BLE001
            raise HarnessError(INVALID, "malformed token") from None
        if not isinstance(header, dict) or header.get("alg") != "EdDSA" or not header.get("kid"):
            raise HarnessError(INVALID, "unsupported alg or missing kid")  # blocks alg=none/HS256 confusion
        try:
            pub = self.keyring.get(header["kid"])
            pub.verify(sig, f"{h64}.{c64}".encode("ascii"))
        except Exception:  # noqa: BLE001
            raise HarnessError(INVALID, "signature verification failed") from None
        if not isinstance(claims, dict):
            raise HarnessError(INVALID, "claims malformed")
        validate_claims(claims)
        if self._clock() >= claims["exp"] + self.leeway:
            raise HarnessError("CAPABILITY_EXPIRED", "capability expired")
        if claims["iat"] > self._clock() + 30:
            raise HarnessError(INVALID, "iat in the future")
        for k, v in (expected or {}).items():
            if k not in BOUND:
                raise ValueError(f"unknown binding {k}")
            if claims[k] != v:
                raise HarnessError(INVALID, f"capability not bound to this request ({k})", {"claim": k})
        return claims


# ---------------------------------------------------------------- replay guard
class FastPath(Protocol):
    def set_nx(self, key: str, ttl_seconds: int) -> bool: ...


class InMemoryFastPath:
    """Redis `SET key 1 NX EX ttl` stand-in."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._d: dict[str, float] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def set_nx(self, key: str, ttl_seconds: int) -> bool:
        with self._lock:
            exp = self._d.get(key)
            if exp is not None and exp > self._clock():
                return False
            self._d[key] = self._clock() + ttl_seconds
            return True


class RedisFastPath:
    def __init__(self, client: Any, prefix: str = "etl:cap:jti:"):
        self._c, self._p = client, prefix

    def set_nx(self, key: str, ttl_seconds: int) -> bool:
        return bool(self._c.set(self._p + key, "1", nx=True, ex=ttl_seconds))


class DuplicateJti(Exception):
    pass


class ConsumptionStore(Protocol):
    def insert(self, jti: str, **fields: Any) -> None:
        """Raise DuplicateJti on unique violation."""

    def exists(self, jti: str) -> bool: ...


class InMemoryConsumptionStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self._lock = threading.Lock()

    def insert(self, jti: str, **fields: Any) -> None:
        with self._lock:
            if jti in self.rows:
                raise DuplicateJti(jti)
            self.rows[jti] = fields

    def exists(self, jti: str) -> bool:
        return jti in self.rows


class SqlAlchemyConsumptionStore:
    """Core-table adapter for `capability_consumptions(jti PRIMARY KEY, ...)`. Pass the
    Backend's Table (or use `make_table`). Insert on an open Connection so it joins the
    caller's transaction."""

    def __init__(self, connection: Any, table: Any):
        self._conn, self._t = connection, table

    @staticmethod
    def make_table(metadata: Any, name: str = "capability_consumptions") -> Any:
        import sqlalchemy as sa

        return sa.Table(name, metadata, sa.Column("jti", sa.String(36), primary_key=True),
                        sa.Column("preparation_id", sa.String(36)), sa.Column("command_id", sa.String(36)))

    def insert(self, jti: str, **fields: Any) -> None:
        from sqlalchemy.exc import IntegrityError

        cols = {c.name for c in self._t.columns}
        vals = {k: v for k, v in fields.items() if k in cols}
        try:
            with self._conn.begin_nested():
                self._conn.execute(self._t.insert().values(jti=jti, **vals))
        except IntegrityError:
            raise DuplicateJti(jti) from None

    def exists(self, jti: str) -> bool:
        import sqlalchemy as sa

        return self._conn.execute(sa.select(self._t.c.jti).where(self._t.c.jti == jti)).first() is not None


class ReplayGuard:
    """Two layers: Redis fast path (SET NX EX 360) and PG unique(jti) as the source of truth."""

    FAST_TTL = 360

    def __init__(self, fast: FastPath, store: ConsumptionStore):
        self.fast, self.store = fast, store

    def reserve(self, jti: str) -> None:
        if not self.fast.set_nx(jti, self.FAST_TTL):
            raise HarnessError("CAPABILITY_REPLAY_DETECTED", "jti already seen (fast path)")

    def consume(self, jti: str, **fields: Any) -> None:
        try:
            self.store.insert(jti, **fields)
        except DuplicateJti:
            raise HarnessError("CAPABILITY_REPLAY_DETECTED", "jti already consumed") from None

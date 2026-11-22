import base64
import json
import threading

import jsonschema
import pytest

from app.capabilities import (
    CapabilityIssuer, CapabilityVerifier, DuplicateJti, InMemoryConsumptionStore, InMemoryFastPath, ReplayGuard,
    SqlAlchemyConsumptionStore, TOOL_AUDIENCE,
)
from app.capabilities.signing import (
    KeyRing, LocalEd25519Signer, VaultTransitEd25519Signer, b64u_decode, b64u_encode, vault_signature_to_b64url,
)
from app.security.errors import HarnessError
from conftest import Clock, H, ORG, PROJ, schema

PREP = "33333333-3333-3333-3333-333333333333"


def issue(issuer, **over):
    kw = dict(sub="operator", tool="seatunnel.submit_batch", organization_id=ORG, project_id=PROJ, environment="prod",
              preparation_id=PREP, pipeline_version_hash=H("a"), approval_digest=H("d"), policy_version="policy-1")
    kw.update(over)
    return issuer.issue(**kw)


@pytest.fixture
def kit():
    clock, signer, ring = Clock(), LocalEd25519Signer("cap:v1"), KeyRing()
    signer.register(ring)
    return clock, signer, ring, CapabilityIssuer(signer, clock), CapabilityVerifier(ring, clock)


def code(fn):
    with pytest.raises(HarnessError) as e:
        fn()
    return e.value.code


def tamper_claims(tok, **ch):
    h, c, s = tok.split(".")
    claims = json.loads(b64u_decode(c))
    claims.update(ch)
    return f"{h}.{b64u_encode(json.dumps(claims, separators=(',', ':')).encode())}.{s}"


def test_valid_token_schema_and_header(kit):
    clock, signer, ring, issuer, verifier = kit
    tok, claims = issue(issuer)
    jsonschema.validate(claims, schema("capability_jws.schema.json"))
    assert len(claims) == 15 and claims["exp"] - claims["iat"] == 300 and claims["aud"] == "seatunnel-worker"
    assert json.loads(b64u_decode(tok.split(".")[0])) == {"alg": "EdDSA", "kid": "cap:v1", "typ": "JWT"}
    assert verifier.verify(tok, {"tool": "seatunnel.submit_batch", "aud": "seatunnel-worker", "project_id": PROJ,
                                 "environment": "prod"}) == claims


def test_expired_blocked(kit):
    clock, signer, ring, issuer, verifier = kit
    tok, _ = issue(issuer)
    clock.t += 299
    verifier.verify(tok)
    clock.t += 2
    assert code(lambda: verifier.verify(tok)) == "CAPABILITY_EXPIRED"


def test_ttl_over_300_cannot_be_issued_or_forged(kit):
    clock, signer, ring, issuer, verifier = kit
    _, claims = issue(issuer, ttl=9999)
    assert claims["exp"] - claims["iat"] == 300
    # 即使签名合法，TTL 超过 300 秒的令牌仍会被拒绝
    import app.capabilities as cap

    bad = dict(claims, exp=claims["iat"] + 3600)
    h = b64u_encode(json.dumps({"alg": "EdDSA", "kid": "cap:v1"}).encode())
    c = b64u_encode(json.dumps(bad).encode())
    tok = f"{h}.{c}.{b64u_encode(signer.sign(f'{h}.{c}'.encode()))}"
    assert code(lambda: verifier.verify(tok)) == "CAPABILITY_INVALID_SIGNATURE"


@pytest.mark.parametrize("bind", [
    {"aud": "publish-adapter"}, {"tool": "doris.atomic_swap"}, {"project_id": ORG}, {"organization_id": PROJ},
    {"environment": "dev"}, {"pipeline_version_hash": H("e")}, {"approval_digest": H("e")},
    {"preparation_id": ORG}, {"policy_version": "policy-2"},
])
def test_binding_mismatch_blocked(kit, bind):
    clock, signer, ring, issuer, verifier = kit
    tok, _ = issue(issuer)
    assert code(lambda: verifier.verify(tok, bind)) == "CAPABILITY_INVALID_SIGNATURE"


@pytest.mark.parametrize("field,val", [
    ("pipeline_version_hash", H("e")), ("approval_digest", H("e")), ("tool", "doris.atomic_swap"),
    ("project_id", ORG), ("environment", "dev"), ("exp", 9_999_999_999), ("aud", "rollback-executor"),
])
def test_modified_claims_break_signature(kit, field, val):
    clock, signer, ring, issuer, verifier = kit
    tok, _ = issue(issuer)
    assert code(lambda: verifier.verify(tamper_claims(tok, **{field: val}))) == "CAPABILITY_INVALID_SIGNATURE"


def test_invalid_signature_unknown_kid_alg_confusion(kit):
    clock, signer, ring, issuer, verifier = kit
    tok, _ = issue(issuer)
    other = CapabilityIssuer(LocalEd25519Signer("cap:v1"), clock)
    forged, _ = issue(other)  # 相同 kid 但使用攻击者私钥签名
    assert code(lambda: verifier.verify(forged)) == "CAPABILITY_INVALID_SIGNATURE"
    rogue, _ = issue(CapabilityIssuer(LocalEd25519Signer("rogue:v1"), clock))
    assert code(lambda: verifier.verify(rogue)) == "CAPABILITY_INVALID_SIGNATURE"
    h, c, s = tok.split(".")
    for hdr in ({"alg": "none", "kid": "cap:v1"}, {"alg": "HS256", "kid": "cap:v1"}, {"alg": "EdDSA"}):
        nh = b64u_encode(json.dumps(hdr).encode())
        assert code(lambda: verifier.verify(f"{nh}.{c}.{s}")) == "CAPABILITY_INVALID_SIGNATURE"
    assert code(lambda: verifier.verify(f"{h}.{c}.")) == "CAPABILITY_INVALID_SIGNATURE"
    assert code(lambda: verifier.verify("garbage")) == "CAPABILITY_INVALID_SIGNATURE"


def test_tool_audience_pairing_enforced(kit):
    clock, signer, ring, issuer, verifier = kit
    for tool, aud in TOOL_AUDIENCE.items():
        _, c = issue(issuer, tool=tool)
        assert c["aud"] == aud
    assert code(lambda: issue(issuer, tool="shell.exec")) == "CAPABILITY_INVALID_SIGNATURE"


# ---------------- Vault Transit 签名适配器（真实 Ed25519 密钥对）
class FakeVaultTransit:
    """模拟 Vault Transit /sign Ed25519 接口：返回 'vault:v1:<64字节原始签名的标准Base64>'。"""

    def __init__(self):
        self.key = LocalEd25519Signer("x")
        self.calls = []

    def sign(self, key_name, input_b64, key_version=None):
        self.calls.append((key_name, key_version))
        raw = self.key.sign(base64.b64decode(input_b64))
        return f"vault:v{key_version or 1}:" + base64.b64encode(raw).decode()


def test_vault_transit_signature_is_converted_to_jws_base64url():
    vt = FakeVaultTransit()
    signer = VaultTransitEd25519Signer(vt, "etl-agent-capability", 1)
    ring = KeyRing()
    ring.add_vault_public_key(signer.kid, base64.b64encode(vt.key.public_raw()).decode())  # Vault 返回 32 字节原始公钥的标准 Base64
    clock = Clock()
    tok, claims = issue(CapabilityIssuer(signer, clock))
    sig_part = tok.split(".")[2]
    assert "=" not in sig_part and "+" not in sig_part and "/" not in sig_part and len(b64u_decode(sig_part)) == 64
    assert json.loads(b64u_decode(tok.split(".")[0]))["kid"] == "etl-agent-capability:v1"
    assert CapabilityVerifier(ring, clock).verify(tok) == claims
    assert vt.calls == [("etl-agent-capability", 1)]
    # 直接测试适配器签名格式转换
    raw = vt.key.sign(b"m")
    assert vault_signature_to_b64url("vault:v1:" + base64.b64encode(raw).decode()) == b64u_encode(raw)
    for bad in ("v1:xx", "vault:v1:" + base64.b64encode(b"short").decode(), "garbage"):
        with pytest.raises(HarnessError):
            vault_signature_to_b64url(bad)


def test_key_rotation_old_kid_still_verifies():
    clock, ring = Clock(), KeyRing()
    old, new = LocalEd25519Signer("cap:v1"), LocalEd25519Signer("cap:v2")
    old.register(ring), new.register(ring)
    tok_old, _ = issue(CapabilityIssuer(old, clock))
    tok_new, _ = issue(CapabilityIssuer(new, clock))
    v = CapabilityVerifier(ring, clock)
    v.verify(tok_old), v.verify(tok_new)


# ---------------- 双层防重放（ReplayGuard）
def test_jti_replay_fast_path_blocked():
    g = ReplayGuard(InMemoryFastPath(), InMemoryConsumptionStore())
    g.reserve("j1")
    assert code(lambda: g.reserve("j1")) == "CAPABILITY_REPLAY_DETECTED"


def test_jti_replay_pg_unique_blocked_even_if_redis_flushed():
    store = InMemoryConsumptionStore()
    ReplayGuard(InMemoryFastPath(), store).consume("j1")
    fresh_redis = ReplayGuard(InMemoryFastPath(), store)
    fresh_redis.reserve("j1")  # 模拟 Redis 丢失键值（重启/驱逐），PG 唯一约束兜底阻断
    assert code(lambda: fresh_redis.consume("j1")) == "CAPABILITY_REPLAY_DETECTED"


def test_pg_unique_constraint_via_sqlalchemy_adapter():
    sa = pytest.importorskip("sqlalchemy")
    eng = sa.create_engine("sqlite://")
    md = sa.MetaData()
    table = SqlAlchemyConsumptionStore.make_table(md)
    md.create_all(eng)
    with eng.connect() as conn:
        store = SqlAlchemyConsumptionStore(conn, table)
        guard = ReplayGuard(InMemoryFastPath(), store)
        guard.consume("j1", preparation_id="p", command_id="c")
        assert store.exists("j1")
        assert code(lambda: guard.consume("j1")) == "CAPABILITY_REPLAY_DETECTED"
        guard.consume("j2")


def test_concurrent_same_jti_only_one_wins():
    store = InMemoryConsumptionStore()
    res, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        try:
            ReplayGuard(InMemoryFastPath(), store).consume("same")
            res.append("ok")
        except HarnessError as e:
            res.append(e.code)

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts], [t.join() for t in ts]
    assert res.count("ok") == 1 and res.count("CAPABILITY_REPLAY_DETECTED") == 7


def test_redis_fast_path_adapter_uses_set_nx_ex():
    from app.capabilities import RedisFastPath

    calls = []

    class R:
        def set(self, k, v, nx, ex):
            calls.append((k, nx, ex))
            return len(calls) == 1 or None

    f = RedisFastPath(R())
    assert f.set_nx("j", 360) is True and f.set_nx("j", 360) is False
    assert calls[0][1:] == (True, 360)

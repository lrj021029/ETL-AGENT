import logging

import pytest

from app.secrets import (
    EncryptedPostgresSecretProvider, InMemorySecretRowStore, SecretProvider, SecretValue, VaultKV2SecretProvider, mask,
)
from app.security.errors import HarnessError
from app.security.redaction import REDACTED, RedactingFilter, redact, redact_text

PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\nabcdef==\n-----END PRIVATE KEY-----"
SECRETS = ["S3cr3tPassw0rd!", "tok_abcDEF123456789xyz", "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"]


def leaked(s):
    return any(x in s for x in SECRETS)


def test_text_redaction():
    cases = [
        "connect to postgresql://etl:S3cr3tPassw0rd!@db:5432/x failed",
        "jdbc:mysql://root:S3cr3tPassw0rd!@mysql:3306/d",
        "Authorization: Bearer tok_abcDEF123456789xyz",
        "password=S3cr3tPassw0rd! host=a",
        '{"password": "S3cr3tPassw0rd!"}',
        "key:\n" + PEM,
        "api_key: tok_abcDEF123456789xyz",
    ]
    for c in cases:
        out = redact_text(c)
        assert not leaked(out), (c, out)
        assert REDACTED in out
    assert "db:5432/x" in redact_text(cases[0])  # non-secret context preserved


def test_jws_and_vault_tokens_redacted():
    jws = "eyJhbGciOiJFZERTQSJ9.eyJqdGkiOiJ4In0.c2lnbmF0dXJl"
    assert jws not in redact_text(f"token was {jws}")
    assert "hvs.CAESIabcdefghijklmnopqrstuvwxyz" not in redact_text("x hvs.CAESIabcdefghijklmnopqrstuvwxyz")


def test_dict_redaction_deep_and_placeholders_kept():
    d = {"conn": {"password": "S3cr3tPassw0rd!", "uri": "mysql://u:S3cr3tPassw0rd!@h/db"},
         "headers": {"Authorization": "Bearer tok_abcDEF123456789xyz"}, "keys": [PEM, {"private_key": PEM}],
         "password_ref": "${SECRET:SRC_PWD}", "password": "${SECRET:SRC_PWD}", "note": "ok", "n": 3}
    out = redact(d)
    assert not leaked(repr(out))
    assert out["password"] == "${SECRET:SRC_PWD}" and out["note"] == "ok" and out["n"] == 3
    assert d["conn"]["password"] == "S3cr3tPassw0rd!"  # input untouched


def test_logging_filter_scrubs_message_args_and_exceptions():
    class H(logging.Handler):
        def __init__(self):
            super().__init__()
            self.out = []

        def emit(self, r):
            self.out.append(self.format(r))

    h = H()
    h.setFormatter(logging.Formatter("%(message)s"))
    h.addFilter(RedactingFilter())
    lg = logging.getLogger("sec-test")
    lg.setLevel(logging.DEBUG)
    lg.addHandler(h)
    lg.error("login %s with password=%s", "bob", "S3cr3tPassw0rd!")
    lg.error("uri=%s", "postgresql://a:S3cr3tPassw0rd!@h/d")
    try:
        raise RuntimeError("bad Bearer tok_abcDEF123456789xyz")
    except RuntimeError:
        lg.exception("boom")
    lg.warning(PEM)
    assert h.out and not any(leaked(o) for o in h.out)


def test_error_payload_never_contains_secret():
    e = HarnessError("SECRET_PROVIDER_ERROR", "failed postgresql://u:S3cr3tPassw0rd!@h/d",
                     {"password": "S3cr3tPassw0rd!", "hdr": "Bearer tok_abcDEF123456789xyz"})
    assert not leaked(str(e)) and not leaked(repr(e.to_payload()))


def test_secret_value_repr_masked():
    sv = SecretValue("S3cr3tPassw0rd!", 3)
    assert not leaked(repr(sv)) and not leaked(str(sv)) and not leaked(f"{sv}") and sv.reveal() == "S3cr3tPassw0rd!"
    assert mask("S3cr3tPassw0rd!") == "********" and mask("") == ""


def test_encrypted_postgres_provider_roundtrip_versions_and_tamper():
    store = InMemorySecretRowStore()
    key = EncryptedPostgresSecretProvider.generate_key()
    p = EncryptedPostgresSecretProvider(store, [key])
    assert isinstance(p, SecretProvider)
    assert p.put("a", "v1") == 1 and p.put("a", "v2") == 2
    assert p.get("a").reveal() == "v2" and p.get("a", 1).reveal() == "v1" and p.current_version("a") == 2
    assert b"v2" not in store.fetch("a", 2).ciphertext  # encrypted at rest
    # swapping ciphertext between refs is detected
    p.put("b", "other")
    store._rows["b"][0].ciphertext = store._rows["a"][0].ciphertext
    with pytest.raises(HarnessError):
        p.get("b")
    with pytest.raises(HarnessError):  # wrong key
        EncryptedPostgresSecretProvider(store, [EncryptedPostgresSecretProvider.generate_key()]).get("a")
    # key rotation: old key still decrypts, new key encrypts
    new = EncryptedPostgresSecretProvider.generate_key()
    rot = EncryptedPostgresSecretProvider(store, [new, key])
    assert rot.get("a").reveal() == "v2" and rot.rotate_master_key("a") == 3
    p.delete("a")
    with pytest.raises(HarnessError):
        p.current_version("a")


class FakeKV2:
    def __init__(self):
        self.d = {}
        self.secrets = type("S", (), {"kv": type("K", (), {"v2": self})()})()

    def create_or_update_secret(self, path, secret, mount_point):
        self.d.setdefault((mount_point, path), []).append(secret)
        return {"data": {"version": len(self.d[(mount_point, path)])}}

    def read_secret_version(self, path, version, mount_point, raise_on_deleted_version):
        vs = self.d[(mount_point, path)]
        v = version or len(vs)
        return {"data": {"data": vs[v - 1], "metadata": {"version": v}}}

    def read_secret_metadata(self, path, mount_point):
        return {"data": {"current_version": len(self.d[(mount_point, path)])}}

    def delete_metadata_and_all_versions(self, path, mount_point):
        self.d.pop((mount_point, path))


def test_vault_kv2_provider():
    p = VaultKV2SecretProvider(FakeKV2())
    assert isinstance(p, SecretProvider)
    assert p.put("conn/src", "pw1") == 1 and p.put("conn/src", "pw2") == 2
    assert p.get("conn/src").reveal() == "pw2" and p.get("conn/src", 1).reveal() == "pw1" and p.current_version("conn/src") == 2
    p.delete("conn/src")


def test_vault_errors_are_wrapped_and_redacted():
    class Boom:
        secrets = None

    with pytest.raises(HarnessError) as e:
        VaultKV2SecretProvider(Boom()).get("x")
    assert e.value.code == "SECRET_PROVIDER_ERROR"

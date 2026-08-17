from __future__ import annotations

import json
import os

import httpx
import pytest

from costcompass import api, vault
from costcompass.vault import crypto as vault_crypto

PASSWORD = "correct horse battery staple"
SAMPLE_DOC = {
    "schema_version": 1,
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2024-01-01T00:00:00Z",
    "future_field": "must survive round-trip",
    "entries": [
        {
            "id": "1",
            "provider": "anthropic",
            "api_key": "sk-ant",
            "metadata": {"instance_key": ""},
        },
        {
            "id": "2",
            "provider": "google",
            "api_key": "ya29-rt",
            "metadata": {"instance_key": "__google_oauth__"},
        },
        {
            "id": "3",
            "provider": "google",
            "api_key": "proj-key",
            "metadata": {"instance_key": "proj-2"},
        },
    ],
}


@pytest.fixture
def jwe_blob() -> str:
    p2s = os.urandom(16)
    plaintext = json.dumps(SAMPLE_DOC).encode()
    return vault.encrypt_jwe(plaintext, PASSWORD, p2s, vault.DEFAULT_PBKDF2_ITERS)


def test_roundtrip(jwe_blob):
    plaintext, p2s, p2c = vault.decrypt_jwe(jwe_blob, PASSWORD)
    assert json.loads(plaintext)["future_field"] == "must survive round-trip"
    assert len(p2s) == 16
    assert p2c == vault.DEFAULT_PBKDF2_ITERS


def test_wrong_password(jwe_blob):
    with pytest.raises(vault.VaultError, match="could not decrypt"):
        vault.decrypt_jwe(jwe_blob, "wrong")


def test_tampered_ciphertext(jwe_blob):
    parts = jwe_blob.split(".")
    # Flip a char in the ciphertext segment.
    ct = parts[3]
    parts[3] = ("A" if ct[0] != "A" else "B") + ct[1:]
    with pytest.raises(vault.VaultError):
        vault.decrypt_jwe(".".join(parts), PASSWORD)


def test_missing_p2s_raises_vault_error(jwe_blob):
    import base64 as _b64

    parts = jwe_blob.split(".")
    header = json.loads(vault_crypto._b64u_decode(parts[0]))
    del header["p2s"]
    parts[0] = _b64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
    with pytest.raises(vault.VaultError, match="p2s"):
        vault.decrypt_jwe(".".join(parts), PASSWORD)


def test_below_iteration_floor_rejected():
    p2s = os.urandom(16)
    blob = vault.encrypt_jwe(b"{}", PASSWORD, p2s, 1000)
    with pytest.raises(vault.VaultError, match="iteration"):
        vault.decrypt_jwe(blob, PASSWORD)


def _retarget_p2c(jwe_blob, p2c):
    """Rewrite a valid blob's header `p2c`, leaving everything else intact so
    the iteration band is the only reason validation can fail."""
    parts = jwe_blob.split(".")
    header = json.loads(vault_crypto._b64u_decode(parts[0]))
    header["p2c"] = p2c
    parts[0] = vault_crypto._b64u_encode(json.dumps(header).encode())
    return ".".join(parts)


def test_above_iteration_cap_rejected(jwe_blob):
    # A real blob at the cap+1 is expensive to mint, so tamper a valid header
    # instead. The cap must reject during header validation — before any
    # PBKDF2 work runs.
    tampered = _retarget_p2c(jwe_blob, vault.MAX_PBKDF2_ITERS + 1)
    with pytest.raises(vault.VaultError, match="iteration"):
        vault.decrypt_jwe(tampered, PASSWORD)


def test_32bit_max_iterations_rejected(jwe_blob):
    """Unbounded `p2c` would make every unlock burn billions of PBKDF2
    iterations before the auth tag could reject the blob."""
    tampered = _retarget_p2c(jwe_blob, 0xFFFF_FFFF)
    with pytest.raises(vault.VaultError, match="iteration"):
        vault.decrypt_jwe(tampered, PASSWORD)


def test_iteration_cap_is_small_multiple_of_default():
    assert vault.MAX_PBKDF2_ITERS == 4 * vault.DEFAULT_PBKDF2_ITERS


def _lift_jwcrypto_iter_cap(monkeypatch):
    # jwcrypto ships a conservative default max p2c below our 600k floor;
    # raise it so the library will round-trip a spec-compliant blob.
    jwa = pytest.importorskip("jwcrypto.jwa")
    monkeypatch.setattr(jwa, "default_max_pbkdf2_iterations", 10_000_000, raising=False)


def test_cross_impl_jwcrypto_reads_ours(jwe_blob, monkeypatch):
    _lift_jwcrypto_iter_cap(monkeypatch)
    jwcrypto_jwe = pytest.importorskip("jwcrypto.jwe")
    jwk = pytest.importorskip("jwcrypto.jwk")
    token = jwcrypto_jwe.JWE()
    token.deserialize(jwe_blob, key=jwk.JWK.from_password(PASSWORD))
    assert json.loads(token.payload)["entries"][0]["provider"] == "anthropic"


def test_cross_impl_we_read_jwcrypto(monkeypatch):
    _lift_jwcrypto_iter_cap(monkeypatch)
    jwcrypto_jwe = pytest.importorskip("jwcrypto.jwe")
    jwk = pytest.importorskip("jwcrypto.jwk")
    protected = json.dumps(
        {"alg": "PBES2-HS256+A128KW", "enc": "A256GCM", "p2c": 600_000}
    )
    token = jwcrypto_jwe.JWE(json.dumps(SAMPLE_DOC).encode(), protected)
    token.add_recipient(jwk.JWK.from_password(PASSWORD))
    compact = token.serialize(compact=True)
    plaintext, _, _ = vault.decrypt_jwe(compact, PASSWORD)
    assert json.loads(plaintext)["entries"][2]["api_key"] == "proj-key"


def test_rejects_non_base64url_alphabet(jwe_blob):
    # A '+' is valid standard-base64 but NOT base64url; strict decode rejects
    # it before crypto rather than silently coercing (mirrors frontend parseJwe).
    parts = jwe_blob.split(".")
    parts[3] = "++" + parts[3][2:]
    with pytest.raises(vault.VaultError, match="base64url"):
        vault.decrypt_jwe(".".join(parts), PASSWORD)


def test_rejects_impossible_segment_length(jwe_blob):
    # A base64 segment can never have length % 4 == 1; reject it explicitly.
    parts = jwe_blob.split(".")
    parts[3] = "AAAAA"  # len 5 → 5 % 4 == 1, an impossible base64url length
    with pytest.raises(vault.VaultError, match="base64url"):
        vault.decrypt_jwe(".".join(parts), PASSWORD)


def test_entry_for_default_and_explicit(jwe_blob):
    plaintext, p2s, p2c = vault.decrypt_jwe(jwe_blob, PASSWORD)
    v = vault.Vault(doc=json.loads(plaintext), p2s=p2s, p2c=p2c, revision=3)
    assert v.entry_for("anthropic")["api_key"] == "sk-ant"
    assert v.entry_for("anthropic", "")["api_key"] == "sk-ant"
    assert v.entry_for("google", "__google_oauth__")["api_key"] == "ya29-rt"
    assert v.entry_for("google", "proj-2")["api_key"] == "proj-key"
    assert v.entry_for("openai") is None


def test_entry_for_skips_non_dict_element():
    """A non-object element inside ``entries`` is stepped over, not fatal.

    The shared corpus (``vectors/relay/vault-entry-lookup.json``) deliberately
    cannot cover this: the browser's lookup is only reachable through
    ``deserializeVault``, which rejects such a document before ``findEntry``
    runs, so pinning it there would fabricate an agreement the three ports
    cannot reach. Each port covers it locally instead. Before this guard the
    scan raised ``AttributeError`` here while the other two degraded to
    "no match".
    """
    v = vault.Vault(
        doc={"entries": [None, "nope", 42, ["x"], {"provider": "a", "api_key": "k"}]},
        p2s=b"\x00" * 16,
        p2c=vault.DEFAULT_PBKDF2_ITERS,
        revision=1,
    )
    assert v.entry_for("a")["api_key"] == "k"
    assert v.entry_for("missing") is None


def test_fetch_and_decrypt(jwe_blob):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"jwe": jwe_blob, "revision": 7, "updated_at": "x"}
        )

    client = api.Client(
        "https://x/api/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    v = vault.fetch_and_decrypt(client, PASSWORD)
    assert v.revision == 7
    assert v.entry_for("anthropic")["api_key"] == "sk-ant"


def test_fetch_and_decrypt_no_vault():
    client = api.Client(
        "https://x/api/v1",
        "sk",
        http=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(404, json={"error": "x"})
            )
        ),
    )
    with pytest.raises(vault.VaultError, match="No vault"):
        vault.fetch_and_decrypt(client, PASSWORD)


def test_write_back_bumps_revision(jwe_blob):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            seen["body"] = request.content
            return httpx.Response(200, json={"revision": 8, "updated_at": "y"})
        return httpx.Response(404)

    client = api.Client(
        "https://x/api/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    plaintext, p2s, p2c = vault.decrypt_jwe(jwe_blob, PASSWORD)
    v = vault.Vault(doc=json.loads(plaintext), p2s=p2s, p2c=p2c, revision=7)
    new_rev = vault.write_back(client, v, PASSWORD)
    assert new_rev == 8
    assert v.revision == 8
    assert (
        b'"expected_revision":7' in seen["body"]
        or b'"expected_revision": 7' in seen["body"]
    )


# --- Best-effort key-material scrubbing -------------------------------------
#
# The relay holds the byte-shaped secrets it owns (KEK, CEK, decrypted vault
# plaintext) in `bytearray` and overwrites them in place once used. These tests
# prove the scrub happens on both the success path AND every failure path, and
# that it never corrupts the crypto output.


class _ZeroSpy:
    """Wraps the real `vault_crypto._zero`: snapshots each buffer at call time (to prove
    it held live key material) then scrubs it for real (to prove it ends zeroed).
    After the code under test returns, `.snapshots`/`.buffers` are inspected."""

    def __init__(self):
        self.snapshots: list[bytes] = []
        self.buffers: list[bytearray] = []
        self._real = vault_crypto._zero  # capture the real impl before patching

    def __call__(self, buf: bytearray) -> None:
        self.snapshots.append(bytes(buf))
        self.buffers.append(buf)
        self._real(buf)

    def install(self, monkeypatch):
        # Patch the module global so decrypt_jwe/encrypt_jwe/etc. resolve us at
        # call time; we delegate to the real _zero captured in __init__.
        # `vault.crypto` is the one place it lives — the write-back path in the
        # package's __init__ reaches it the same way, so both are covered.
        monkeypatch.setattr(vault_crypto, "_zero", self)
        return self


def _assert_all_scrubbed(spy: _ZeroSpy) -> None:
    for snap, live in zip(spy.snapshots, spy.buffers):
        assert any(snap), "buffer should have held live key material before wipe"
        assert not any(live), "buffer should be all-zero after wipe"


def test_zero_overwrites_in_place():
    buf = bytearray(b"\x11" * 32)
    ident = id(buf)
    vault_crypto._zero(buf)
    assert bytes(buf) == b"\x00" * 32
    assert len(buf) == 32
    assert id(buf) == ident  # in-place, no realloc that would strand the original


def test_zero_empty_is_noop():
    vault_crypto._zero(bytearray())  # must not raise


def test_decrypt_zeroizes_kek_and_cek(jwe_blob, monkeypatch):
    spy = _ZeroSpy().install(monkeypatch)
    plaintext, _, _ = vault.decrypt_jwe(jwe_blob, PASSWORD)
    # KEK (16B) + CEK (32B), both scrubbed; plaintext is returned live.
    assert [len(s) for s in spy.snapshots] == [
        32,
        16,
    ]  # inner CEK zeroed before outer KEK
    _assert_all_scrubbed(spy)
    assert json.loads(plaintext)["future_field"] == "must survive round-trip"


def test_decrypt_zeroizes_kek_on_unwrap_failure(jwe_blob, monkeypatch):
    spy = _ZeroSpy().install(monkeypatch)
    with pytest.raises(vault.VaultError, match="could not decrypt"):
        vault.decrypt_jwe(jwe_blob, "wrong")
    # Wrong password fails at unwrap, before the CEK exists: only the KEK is scrubbed.
    assert [len(s) for s in spy.snapshots] == [16]
    _assert_all_scrubbed(spy)


def test_decrypt_zeroizes_cek_on_gcm_failure(jwe_blob, monkeypatch):
    # Correct password (unwrap succeeds -> CEK created) but tampered ciphertext
    # (GCM open fails) -> both KEK and CEK must be scrubbed.
    parts = jwe_blob.split(".")
    ct = parts[3]
    parts[3] = ("A" if ct[0] != "A" else "B") + ct[1:]
    tampered = ".".join(parts)
    spy = _ZeroSpy().install(monkeypatch)
    with pytest.raises(vault.VaultError):
        vault.decrypt_jwe(tampered, PASSWORD)
    assert [len(s) for s in spy.snapshots] == [32, 16]
    _assert_all_scrubbed(spy)


def test_encrypt_zeroizes_kek_and_cek(monkeypatch):
    p2s = os.urandom(16)
    plaintext = json.dumps(SAMPLE_DOC).encode()
    spy = _ZeroSpy().install(monkeypatch)
    blob = vault.encrypt_jwe(plaintext, PASSWORD, p2s, vault.DEFAULT_PBKDF2_ITERS)
    assert sorted(len(s) for s in spy.snapshots) == [16, 32]
    _assert_all_scrubbed(spy)
    # Scrubbing didn't touch the ciphertext path: the blob still decrypts.
    out, _, _ = vault.decrypt_jwe(blob, PASSWORD)
    assert json.loads(out) == SAMPLE_DOC


def test_encrypt_zeroizes_kek_and_cek_on_wrap_failure(monkeypatch):
    # Inject a fault after KEK+CEK are allocated (aes_key_wrap raises) to prove
    # encrypt_jwe's finally scrubs both even when the crypto step blows up.
    def boom(*_args, **_kwargs):
        raise RuntimeError("wrap exploded")

    monkeypatch.setattr(vault_crypto, "aes_key_wrap", boom)
    spy = _ZeroSpy().install(monkeypatch)
    with pytest.raises(RuntimeError, match="wrap exploded"):
        vault.encrypt_jwe(
            json.dumps(SAMPLE_DOC).encode(),
            PASSWORD,
            os.urandom(16),
            vault.DEFAULT_PBKDF2_ITERS,
        )
    # Both KEK (16B) and CEK (32B) were allocated before the fault, so both scrub.
    assert sorted(len(s) for s in spy.snapshots) == [16, 32]
    _assert_all_scrubbed(spy)


def test_fetch_and_decrypt_zeroizes_plaintext(jwe_blob, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"jwe": jwe_blob, "revision": 7, "updated_at": "x"}
        )

    client = api.Client(
        "https://x/api/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    spy = _ZeroSpy().install(monkeypatch)
    v = vault.fetch_and_decrypt(client, PASSWORD)
    # Parsed doc is intact even though its source plaintext was wiped.
    assert v.entry_for("anthropic")["api_key"] == "sk-ant"
    # KEK + CEK (from decrypt_jwe) + plaintext (from fetch_and_decrypt) = 3 scrubs.
    assert len(spy.snapshots) == 3
    assert any(s.startswith(b"{") and b"sk-ant" in s for s in spy.snapshots)
    _assert_all_scrubbed(spy)


def test_write_back_zeroizes_plaintext(jwe_blob, monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            seen["jwe"] = json.loads(request.content)["jwe"]
            return httpx.Response(200, json={"revision": 8, "updated_at": "y"})
        return httpx.Response(404)

    client = api.Client(
        "https://x/api/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    plaintext, p2s, p2c = vault.decrypt_jwe(jwe_blob, PASSWORD)
    v = vault.Vault(doc=json.loads(plaintext), p2s=p2s, p2c=p2c, revision=7)

    spy = _ZeroSpy().install(monkeypatch)
    vault.write_back(client, v, PASSWORD)
    # The serialized plaintext held the secrets and was scrubbed.
    assert any(b"sk-ant" in s for s in spy.snapshots)
    _assert_all_scrubbed(spy)
    # Functional round-trip: the PUT body decrypts back to the original doc.
    out, _, _ = vault.decrypt_jwe(seen["jwe"], PASSWORD)
    assert json.loads(out) == SAMPLE_DOC


def test_write_back_zeroizes_plaintext_on_encrypt_failure(jwe_blob, monkeypatch):
    put_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        put_calls.append(request.method)
        return httpx.Response(200, json={"revision": 8, "updated_at": "y"})

    client = api.Client(
        "https://x/api/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    # Wrong-length p2s makes encrypt_jwe raise inside write_back's try.
    v = vault.Vault(
        doc=SAMPLE_DOC, p2s=b"\x00\x01\x02", p2c=vault.DEFAULT_PBKDF2_ITERS, revision=7
    )

    spy = _ZeroSpy().install(monkeypatch)
    with pytest.raises(vault.VaultError, match="p2s"):
        vault.write_back(client, v, PASSWORD)
    # Plaintext scrubbed by the finally even though encryption failed...
    assert any(b"sk-ant" in s for s in spy.snapshots)
    _assert_all_scrubbed(spy)
    # ...and no PUT was ever issued (plaintext wiped before the network call).
    assert put_calls == []

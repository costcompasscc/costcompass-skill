# LOCKSTEP: one of three relay implementations (browser is the
# reference) — semantic changes here must land in the CostCompass
# monorepo, checked out alongside this repo, at
# ../costcompass/frontend/src/lib/vault/jwe-compact.ts, crypto.ts,
# and entries.ts, and ../costcompass/client/macos/CostCompassKit/Sources/
#   CostCompassKit/Vault/JWECompact.swift + Vault.swift.
# See "Three relay implementations" in that repo's root CLAUDE.md;
# `make lockstep` there enumerates the whole set across both repos.

"""Vault retrieval + JWE decrypt/encrypt and entry lookup.

Mirrors the browser vault (`../costcompass/frontend/src/lib/vault/`) and
the format contract in `../costcompass/doc/design/vault-format-spec.md`:

    JWE compact, alg = PBES2-HS256+A128KW, enc = A256GCM
    <protected_header>.<encrypted_key>.<iv>.<ciphertext>.<tag>
    PBKDF2 salt = UTF8(alg) || 0x00 || p2s         (RFC 7518 §4.8.1.1)
    AES-GCM AAD = ASCII(base64url(protected_header))

The decrypted vault stays in process memory only — never written to
disk, never logged.
"""

from __future__ import annotations

import base64
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.keywrap import aes_key_unwrap, aes_key_wrap

from . import api

PBES2_ALG = "PBES2-HS256+A128KW"
ENC_ALG = "A256GCM"
MIN_PBKDF2_ITERS = 600_000
DEFAULT_PBKDF2_ITERS = 600_000
_P2S_SIZE = 16
_IV_SIZE = 12
_TAG_SIZE = 16
_WRAPPED_CEK_SIZE = 40
_CEK_SIZE = 32


class VaultError(Exception):
    """Vault retrieval or decryption failure (wrong password, no vault, …).

    ``category`` is the language-neutral outcome code the shared relay
    golden-vector corpus asserts on (``test-vectors/relay/``): ``"wrong_password"``
    for an indistinguishable crypto failure (bad password or tampered blob),
    ``"unsupported_format"`` for a structural/policy rejection, and
    ``"invalid_plaintext_json"`` for a blob that decrypts cleanly but whose
    plaintext is not the JSON vault document. It is set explicitly at the raise
    site rather than parsed back out of the message, so the category is a source
    of truth, not a shadow of the wording. Mirrors the browser's error classes
    (``InvalidPasswordError`` / ``UnsupportedFormatError`` / non-JSON plaintext)
    and the macOS ``VaultError`` enum (``.wrongPassword`` / structural /
    ``.invalidPlaintextJSON``).
    """

    def __init__(self, message: str, *, category: str = "unsupported_format") -> None:
        super().__init__(message)
        self.category = category


# Unpadded base64url alphabet. We validate strictly before decoding so a
# malformed segment fails loudly rather than being silently coerced —
# `base64.urlsafe_b64decode` otherwise ignores stray bytes and accepts the
# standard `+/` alphabet. Mirrors the frontend's `parseJwe` (which rejects
# any char outside this set and the impossible `len % 4 == 1` case) so the
# two implementations agree on what a well-formed JWE segment is.
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]*$")


def _b64u_decode(s: str) -> bytes:
    if not _B64URL_RE.match(s) or len(s) % 4 == 1:
        raise VaultError("vault segment is not valid base64url")
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + "=" * pad)


def _b64u_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


# Best-effort heap hygiene (the Rust broker uses secrecy/zeroize; the managed
# runtimes here can only partially match it). We scrub the byte-shaped secrets
# we own outright — the derived KEK, the unwrapped/generated CEK, and the
# decrypted/re-serialized vault plaintext — by holding them in `bytearray` and
# overwriting in place once used. What is deliberately NOT scrubbed, because it
# is immutable `str`/`bytes` we cannot overwrite (any wrapper leaks copies the
# moment it is serialized into a request): the vault password, the per-entry
# api_key/refresh-token strings inside the parsed `Vault.doc`, the minted OAuth
# access tokens (`refresh/oauth.py`), and the programmatic API key
# (`config.py`). This matches Apple CryptoKit's posture — zeroize the
# byte-buffers, leave the String-shaped material to the runtime.
def _zero(buf: bytearray) -> None:
    """Overwrite a mutable secret buffer in place (best-effort scrub).

    Equal-length slice assignment mutates the existing buffer — no realloc, so
    the original bytes are the ones overwritten (not a fresh allocation left
    beside a stranded copy).
    """
    buf[:] = b"\x00" * len(buf)


def _derive_kek(password: str, p2s: bytes, p2c: int) -> bytes:
    salt = PBES2_ALG.encode("ascii") + b"\x00" + p2s
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=16, salt=salt, iterations=p2c)
    return kdf.derive(unicodedata.normalize("NFC", password).encode("utf-8"))


def decrypt_jwe(jwe: str, password: str) -> tuple[bytearray, bytes, int]:
    """Return (plaintext, p2s, p2c). Raises VaultError on any failure.

    `plaintext` is a `bytearray` the caller owns and should `_zero` once parsed
    (see `fetch_and_decrypt`); the KEK/CEK are scrubbed here before returning.
    """
    parts = jwe.split(".")
    if len(parts) != 5:
        raise VaultError("vault blob is malformed")
    header_b64u, enc_key_b64u, iv_b64u, ct_b64u, tag_b64u = parts
    try:
        header = json.loads(_b64u_decode(header_b64u))
    except (ValueError, json.JSONDecodeError) as exc:
        raise VaultError("vault header is not valid JSON") from exc

    if header.get("alg") != PBES2_ALG or header.get("enc") != ENC_ALG:
        raise VaultError("unsupported vault algorithm")
    p2c = header.get("p2c")
    if not isinstance(p2c, int) or p2c < MIN_PBKDF2_ITERS:
        raise VaultError("vault iteration count is invalid or below the floor")
    p2s_raw = header.get("p2s")
    if not isinstance(p2s_raw, str):
        raise VaultError("vault header is missing p2s")
    p2s = _b64u_decode(p2s_raw)
    if len(p2s) != _P2S_SIZE:
        raise VaultError("vault salt has the wrong length")

    enc_key = _b64u_decode(enc_key_b64u)
    iv = _b64u_decode(iv_b64u)
    ciphertext = _b64u_decode(ct_b64u)
    tag = _b64u_decode(tag_b64u)
    if (
        len(enc_key) != _WRAPPED_CEK_SIZE
        or len(iv) != _IV_SIZE
        or len(tag) != _TAG_SIZE
    ):
        raise VaultError("vault segment has the wrong length")

    aad = header_b64u.encode("ascii")
    kek = bytearray(_derive_kek(password, p2s, p2c))
    try:
        # Wrong password and tampered blob are deliberately indistinguishable.
        try:
            cek = bytearray(aes_key_unwrap(kek, enc_key))
        except Exception as exc:  # noqa: BLE001 - any unwrap failure = bad password
            raise VaultError(
                "could not decrypt vault (wrong password?)", category="wrong_password"
            ) from exc
        try:
            plaintext = bytearray(AESGCM(cek).decrypt(iv, ciphertext + tag, aad))
        except Exception as exc:  # noqa: BLE001
            raise VaultError(
                "could not decrypt vault (wrong password?)", category="wrong_password"
            ) from exc
        finally:
            _zero(cek)
    finally:
        _zero(kek)
    return plaintext, p2s, p2c


def encrypt_jwe(
    plaintext: bytes | bytearray, password: str, p2s: bytes, p2c: int
) -> str:
    """Re-encrypt (write-back path). Fresh CEK + IV every time."""
    if len(p2s) != _P2S_SIZE:
        raise VaultError("p2s has the wrong length")
    iv = os.urandom(_IV_SIZE)
    header = {"alg": PBES2_ALG, "enc": ENC_ALG, "p2c": p2c, "p2s": _b64u_encode(p2s)}
    header_b64u = _b64u_encode(
        json.dumps(header, separators=(",", ":")).encode("utf-8")
    )
    aad = header_b64u.encode("ascii")
    kek = bytearray(_derive_kek(password, p2s, p2c))
    cek = bytearray(os.urandom(_CEK_SIZE))
    try:
        wrapped = aes_key_wrap(kek, cek)
        ct_and_tag = AESGCM(cek).encrypt(iv, plaintext, aad)
    finally:
        _zero(cek)
        _zero(kek)
    ciphertext, tag = ct_and_tag[:-_TAG_SIZE], ct_and_tag[-_TAG_SIZE:]
    return ".".join(
        [
            header_b64u,
            _b64u_encode(wrapped),
            _b64u_encode(iv),
            _b64u_encode(ciphertext),
            _b64u_encode(tag),
        ]
    )


@dataclass
class Vault:
    """A decrypted vault document plus the params needed to write it back."""

    doc: dict[str, Any]
    p2s: bytes
    p2c: int
    revision: int

    def entry_for(
        self, provider: str, instance_key: str | None = None
    ) -> dict[str, Any] | None:
        """Find the entry for (provider, instance_key); mirrors findEntry."""
        for entry in self.doc.get("entries", []):
            if entry.get("provider") != provider:
                continue
            meta = entry.get("metadata") or {}
            ek = meta.get("instance_key") if isinstance(meta, dict) else None
            if not instance_key:  # default card: None or ""
                if not ek:
                    return entry
            elif ek == instance_key:
                return entry
        return None


def decrypt_to_doc(jwe: str, password: str) -> tuple[dict[str, Any], bytes, int]:
    """Decrypt a JWE and parse its plaintext as the vault document.

    The single decrypt-and-parse entry point, shared by ``fetch_and_decrypt``
    and the cross-implementation golden-vector suite so every caller categorizes
    a non-JSON plaintext identically. Mirrors the browser reference's
    decrypt-then-``JSON.parse`` and the macOS ``Vault.decrypt`` (which parses
    inside the same call): a valid decrypt whose plaintext is not JSON is its own
    outcome, ``invalid_plaintext_json`` — not folded into a crypto failure.
    """
    plaintext, p2s, p2c = decrypt_jwe(jwe, password)
    try:
        doc = json.loads(plaintext)
    except (ValueError, json.JSONDecodeError) as exc:
        raise VaultError(
            "vault contents are not valid JSON", category="invalid_plaintext_json"
        ) from exc
    finally:
        _zero(plaintext)
    return doc, p2s, p2c


def fetch_and_decrypt(client: api.Client, password: str) -> Vault:
    """GET /vault and decrypt; raises VaultError if absent or wrong password."""
    blob = client.get_vault()
    if blob is None:
        raise VaultError(
            "No vault found for this account — set one up in the app first."
        )
    doc, p2s, p2c = decrypt_to_doc(blob["jwe"], password)
    return Vault(doc=doc, p2s=p2s, p2c=p2c, revision=int(blob["revision"]))


def write_back(client: api.Client, vault: Vault, password: str) -> int:
    """Re-encrypt the (possibly mutated) doc and PUT it. Returns new revision."""
    plaintext = bytearray(json.dumps(vault.doc, separators=(",", ":")).encode("utf-8"))
    try:
        jwe = encrypt_jwe(plaintext, password, vault.p2s, vault.p2c)
    finally:
        _zero(plaintext)
    result = client.put_vault(jwe, expected_revision=vault.revision)
    vault.revision = int(result["revision"])
    return vault.revision

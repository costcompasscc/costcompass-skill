"""Vault retrieval and write-back, over the two relay concerns beneath it.

The package boundary follows the lockstep concerns, which have separate ports
in the other two relays:

    crypto.py    JWE decrypt/encrypt      <- jwe-compact.ts + crypto.ts / JWECompact.swift
    entries.py   (provider, instance_key) <- entries.ts / Vault.swift

This module is neither: it is the CostCompass-server glue that fetches the
blob, hands it to the crypto layer, and PUTs a re-encrypted one back — the
CLI's analogue of the browser's `vault-api.ts`. Both layers' names are
re-exported so callers keep importing `costcompass.vault` as one module.

The two functions below reach the crypto layer as `crypto.<name>`, not through
those re-exports: a `from`-import binds the function object here, so a test
that replaces `crypto._zero` to observe the scrub would be honoured inside
`decrypt_jwe` and silently bypassed inside `write_back`. One module attribute,
resolved at call time, keeps the two paths from disagreeing.

The decrypted vault stays in process memory only — never written to disk,
never logged.
"""

from __future__ import annotations

import json

from .. import api
from . import crypto
from .crypto import (
    DEFAULT_PBKDF2_ITERS,
    ENC_ALG,
    MAX_PBKDF2_ITERS,
    MIN_PBKDF2_ITERS,
    PBES2_ALG,
    VaultError,
    decrypt_jwe,
    decrypt_to_doc,
    encrypt_jwe,
)
from .entries import Vault

__all__ = [
    "DEFAULT_PBKDF2_ITERS",
    "ENC_ALG",
    "MAX_PBKDF2_ITERS",
    "MIN_PBKDF2_ITERS",
    "PBES2_ALG",
    "Vault",
    "VaultError",
    "decrypt_jwe",
    "decrypt_to_doc",
    "encrypt_jwe",
    "fetch_and_decrypt",
    "write_back",
]


def fetch_and_decrypt(client: api.Client, password: str) -> Vault:
    """GET /vault and decrypt; raises VaultError if absent or wrong password."""
    blob = client.get_vault()
    if blob is None:
        raise VaultError(
            "No vault found for this account — set one up in the app first."
        )
    doc, p2s, p2c = crypto.decrypt_to_doc(blob["jwe"], password)
    return Vault(doc=doc, p2s=p2s, p2c=p2c, revision=int(blob["revision"]))


def write_back(client: api.Client, vault: Vault, password: str) -> int:
    """Re-encrypt the (possibly mutated) doc and PUT it. Returns new revision."""
    plaintext = bytearray(json.dumps(vault.doc, separators=(",", ":")).encode("utf-8"))
    try:
        jwe = crypto.encrypt_jwe(plaintext, password, vault.p2s, vault.p2c)
    finally:
        crypto._zero(plaintext)
    result = client.put_vault(jwe, expected_revision=vault.revision)
    vault.revision = int(result["revision"])
    return vault.revision

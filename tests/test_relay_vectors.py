# LOCKSTEP: CLI side of the shared golden-vector corpus. The reference
# generator is frontend/src/lib/refresh/__tests__/relay-vectors.test.ts;
# the macOS sibling is
# ../costcompass/cli/macos/CostCompassKit/Tests/CostCompassKitTests/
#   RelayVectorsTests.swift. All three load the SAME committed corpus, so a
# change to the browser's expected output forces this suite to conform or
# break. The corpus is vendored under tests/vectors/relay/ (kept byte-identical
# to the monorepo's test-vectors/relay/ by scripts/sync-relay-vectors.sh).
# See "Three relay implementations" in the monorepo root CLAUDE.md.

"""Cross-implementation golden vectors: the CLI relay must decrypt/sign/hash
identically to the browser reference for every committed vector."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from costcompass import vault
from costcompass.refresh import signers

_CORPUS = Path(__file__).parent / "vectors" / "relay"


def _load(name: str) -> list[dict]:
    return json.loads((_CORPUS / name).read_text())["vectors"]


def _ids(vectors: list[dict]) -> list[str]:
    return [v["name"] for v in vectors]


# --- Vendored-corpus integrity ----------------------------------------------
# The sibling repo cannot reach the canonical corpus at test time; this checks
# the vendored copy against the committed sha256 manifest, so a single vendored
# file edited out of step fails here. The canonical<->vendored byte equality is
# enforced from the main repo by `make drift-check`.


def test_vendored_corpus_matches_manifest() -> None:
    lines = (_CORPUS / "MANIFEST.sha256").read_text().splitlines()
    assert lines, "empty MANIFEST.sha256"
    for line in lines:
        want, name = line.split()
        got = hashlib.sha256((_CORPUS / name).read_bytes()).hexdigest()
        assert got == want, f"{name} does not match MANIFEST.sha256"


# --- Vault JWE decrypt ------------------------------------------------------

_JWE = _load("jwe-decrypt.json")


@pytest.mark.parametrize("vec", _JWE, ids=_ids(_JWE))
def test_jwe_decrypt_vector(vec: dict) -> None:
    # decrypt_to_doc is the shared decrypt-AND-parse path (matches the browser's
    # decrypt+JSON.parse and macOS Vault.decrypt), so a non-JSON plaintext is
    # categorized here rather than only on the positive branch.
    if "expect_error" in vec:
        with pytest.raises(vault.VaultError) as ei:
            vault.decrypt_to_doc(vec["jwe"], vec["password"])
        assert ei.value.category == vec["expect_error"]
    else:
        doc, _p2s, _p2c = vault.decrypt_to_doc(vec["jwe"], vec["password"])
        assert doc == vec["expect"]["doc"]


# --- Named signers ----------------------------------------------------------

_SIGNERS = _load("signers.json")


@pytest.mark.parametrize("vec", _SIGNERS, ids=_ids(_SIGNERS))
def test_signer_vector(vec: dict) -> None:
    spec = vec["input"]
    headers = signers.sign_quantconnect_hmac_v1(
        spec, spec["api_token"], now_sec=spec["now_sec"]
    )
    assert headers == vec["expect"]


# --- SHA-256 (the primitive the signer hashes with) -------------------------

_SHA256 = _load("sha256.json")


@pytest.mark.parametrize("vec", _SHA256, ids=_ids(_SHA256))
def test_sha256_vector(vec: dict) -> None:
    # Mirrors the exact digest signers.py computes inline
    # (hashlib.sha256(...).hexdigest() over UTF-8 bytes).
    assert hashlib.sha256(vec["input"].encode("utf-8")).hexdigest() == vec["expect"]

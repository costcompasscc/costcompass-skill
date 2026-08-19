# LOCKSTEP: CLI side of the shared golden-vector corpus. The reference
# generator is frontend/src/lib/refresh/__tests__/relay-vectors.test.ts;
# the macOS sibling is
# ../costcompass/client/macos/CostCompassKit/Tests/CostCompassKitTests/
#   RelayVectorsTests.swift. All three load the SAME committed corpus, so a
# change to the browser's expected output forces this suite to conform or
# break. The corpus is vendored under tests/vectors/relay/ (kept byte-identical
# to the monorepo's test-vectors/relay/ by scripts/sync-relay-vectors.sh).
# See "Three relay implementations" in the monorepo root CLAUDE.md.

"""Cross-implementation golden vectors: the CLI relay must decrypt/sign/hash
identically to the browser reference for every committed vector."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from costcompass import api, vault
from costcompass.refresh import orchestrator, signers
from costcompass.refresh import program as program_mod
from costcompass.refresh.broker import BrokerError

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


# --- body_b64 base64 contract -----------------------------------------------
# The one corpus here with FOUR consumers rather than three: the App Server is
# the fourth, and the canonical-base64 rule this relay was pinned to is ITS
# rule. So it is hand-written rather than generated from the browser reference
# — generating it would make one relay the oracle for a symmetric contract.
#
# Scope is the base64 STAGE only. `_decode_body_strict` layers strict UTF-8,
# BOM, depth and surrogate clauses on top that the server does not implement,
# so asserting it end-to-end here would assert an equality that is deliberately
# false. This Python side also carries the `\Z`-not-`$` trap, which the
# trailing-newline vector is what catches.

_B64_CONTRACT = _load("body-b64-contract.json")


@pytest.mark.parametrize("vec", _B64_CONTRACT, ids=_ids(_B64_CONTRACT))
def test_body_b64_contract_vector(vec: dict) -> None:
    assert program_mod.is_canonical_base64(vec["body_b64"]) is vec["canonical"], vec[
        "why"
    ]


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


# --- Program interpreter ----------------------------------------------------
# Feed each vector's broker_script at the CLI's mock seam (BrokerClient.forward)
# and assert the NORMALIZED outbound forward sequence + relayed/provider-error
# responses match the browser-generated `expect`. The macOS sibling drives the
# same corpus through its transport seam; all three must produce this shape.

_PROGRAM = _load("program.json")


class _CapturingBroker:
    """Replays a vector's scripted broker outcomes in order and records every
    forward request it saw (the CLI analog of the browser's scriptedBroker)."""

    def __init__(self, script: list[dict]) -> None:
        self._script = script
        self._i = 0
        self.requests: list[dict] = []

    def forward(self, request: dict) -> dict:
        self.requests.append(request)
        entry = self._script[self._i]
        self._i += 1
        if entry.get("fail") == "broker_error":
            raise BrokerError(entry["code"], entry["http_status"], entry["message"])
        return {
            "status": entry["status"],
            "headers": entry["headers"],
            "body": entry["body_b64"],
            "signature": entry.get("signature"),
        }


def _norm_request(req: dict) -> dict:
    target = req["target"]
    return {
        "method": target["method"],
        "host": target["host"],
        "path": target["path"],
        "query": target.get("query", ""),
        "headers": req["headers"],
        "body": req.get("body"),
        "signing_token": req.get("signing_token"),
        "req_sig": req.get("req_sig"),
    }


def _norm_response(resp: dict) -> dict:
    # ``synthetic`` is compared on BOTH branches: the flag decides whether the
    # App Server strips a response before plugin.process(), i.e. whether a
    # failed sub-request discards its successful siblings. The corpus used to
    # drop it, which is how this port's flat path diverged from the reference
    # unnoticed.
    synthetic = resp.get("synthetic") is True
    # A provider-error carries a per-impl sentinel host; pin status + decoded
    # error only (the sentinel URL and the raw body-JSON spacing are per-impl).
    if resp["request_url"].startswith("cc-internal://"):
        parsed = json.loads(base64.b64decode(resp["body_b64"]))
        return {
            "kind": "provider_error",
            "status": resp["status"],
            "error": parsed["error"],
            "synthetic": synthetic,
        }
    return {
        "kind": "relayed",
        "status": resp["status"],
        "request_url": resp["request_url"],
        "request_purpose": resp["request_purpose"],
        "body_b64": resp["body_b64"],
        "signature": resp.get("signature"),
        "headers": resp["headers"],
        "synthetic": synthetic,
    }


def _clock(vec: dict):
    # Successive authored values, the last repeating; a constant 0 when absent
    # (a positive deadline_ms then never trips) — matches the browser/macOS.
    clock = vec.get("clock")
    if clock is None:
        return lambda: 0.0
    idx = [0]

    def now() -> float:
        i = min(idx[0], len(clock) - 1)
        idx[0] += 1
        return float(clock[i])

    return now


@pytest.mark.parametrize("vec", _PROGRAM, ids=_ids(_PROGRAM))
def test_program_vector(vec: dict) -> None:
    broker = _CapturingBroker(vec["broker_script"])
    out = program_mod.run_program(
        vec["program"],
        vec["auth_headers"],
        vec.get("signing_token"),
        broker,
        now=_clock(vec),
    )
    got = {
        "requests": [_norm_request(r) for r in broker.requests],
        "responses": [_norm_response(r) for r in out],
    }
    assert got == vec["expect"]


# --- Flat-path broker failure -----------------------------------------------
# The pure (request, BrokerError) -> submitted-payload mapping. Pinned as a
# transform rather than through _run_flat because the three relays' flat
# runners legitimately differ (retry lives in this port's broker client but in
# the browser's orchestrator; this port runs sequentially where the browser
# fans out) — the payload SHAPE is the part that must agree, and it is the part
# that drifted.

_BROKER_FAILURE = _load("broker-failure.json")


@pytest.mark.parametrize("vec", _BROKER_FAILURE, ids=_ids(_BROKER_FAILURE))
def test_broker_failure_vector(vec: dict) -> None:
    e = vec["error"]
    err = BrokerError(
        e["code"], e["http_status"], e["message"], None, e.get("request_id")
    )
    got = orchestrator._synthetic_broker_failure(vec["request"], err)
    assert _norm_response(got) == vec["expect"]


# --- Flat-path run-deadline stub --------------------------------------------
# The request -> submitted-payload mapping for a flat sub-request the run budget
# left unissued. Its own corpus file rather than a broker-failure case: that
# transform takes a BrokerError, this one takes nothing but the abandoned
# request.
#
# This port is the one that already emitted the right shape when the corpus
# gained this file — the browser and macOS were a release behind, banking
# nothing and discarding the entry's fetched siblings. `make lockstep` could not
# see that, because a stale port is an ABSENCE of the new shape rather than a
# drifted line.

_DEADLINE_STUB = _load("deadline-stub.json")


@pytest.mark.parametrize("vec", _DEADLINE_STUB, ids=_ids(_DEADLINE_STUB))
def test_deadline_stub_vector(vec: dict) -> None:
    got = orchestrator._synthetic_deadline(vec["request"])
    assert _norm_response(got) == vec["expect"]


# --- Vault entry lookup -----------------------------------------------------
# Hand-written corpus, like body-b64-contract but for a different reason: the
# rule it pins is a DECISION, not any one relay's behaviour. The three relays
# disagreed three ways on a non-string ``metadata.instance_key`` with no
# majority to defer to, so the browser could not be the oracle. See invariant
# 14 in frontend/src/lib/refresh/CLAUDE.md.
#
# Identity is asserted through ``api_key`` rather than ``id`` because the macOS
# port's VaultEntry carries no id.

_ENTRY_LOOKUP = _load("vault-entry-lookup.json")


@pytest.mark.parametrize("vec", _ENTRY_LOOKUP, ids=_ids(_ENTRY_LOOKUP))
def test_vault_entry_lookup_vector(vec: dict) -> None:
    v = vault.Vault(
        doc=vec["document"],
        p2s=b"\x00" * 16,
        p2c=vault.DEFAULT_PBKDF2_ITERS,
        revision=1,
    )
    selector = vec["selector"]
    found = v.entry_for(selector["provider"], selector.get("instance_key"))
    actual = found["api_key"] if found is not None else None
    assert actual == vec["expect_api_key"], vec["why"]


# --- Ambiguous-failure classification ---------------------------------------
# The STATUS half of "could this failed mutation still have committed
# server-side?" — the question a relay asks before deciding whether a failed
# vault write is worth reconciling against the server's document.
#
# Hand-authored on both sides: the browser's classifier carries arms this port
# has no equivalent for, so generating ``ambiguous`` from it would dress one
# port up as the reference for a rule all three own equally.
#
# The corpus's ``null`` status is "no status line at all" — here an ApiError
# whose ``status`` is None, which is what ``_request`` maps an
# ``httpx.RequestError`` to. Non-status arms (a ``VaultError``, which is raised
# before anything is sent) stay in this port's own tests.

_AMBIGUOUS = _load("ambiguous-failure.json")


@pytest.mark.parametrize("vec", _AMBIGUOUS, ids=_ids(_AMBIGUOUS))
def test_ambiguous_failure_vector(vec: dict) -> None:
    exc = api.ApiError("boom", status=vec["status"])
    assert api.is_ambiguous_failure(exc) is vec["ambiguous"], vec["why"]


# --- Credential routing decision --------------------------------------------
# Which outcome CATEGORY the relay resolves an entry to, given the
# server-authored credential.kind, the subscription_only marker, and whether the
# vault holds a direct entry. A pure decision over data — no mint, no I/O.
#
# The categories are language-neutral because the three relays represent the
# outcome differently: a union in the browser, an enum on macOS, and raised
# exceptions here. The human message is not pinned (each relay names itself in
# it); the wire ``reason`` is, because that is what the App Server reads.

_CREDENTIAL_ROUTING = _load("credential-routing.json")


def _empty_vault() -> vault.Vault:
    return vault.Vault(
        doc={"entries": []},
        p2s=b"\x00" * 16,
        p2c=vault.DEFAULT_PBKDF2_ITERS,
        revision=1,
    )


def _vault_with_direct_entry(provider: str) -> vault.Vault:
    v = _empty_vault()
    v.doc["entries"] = [
        {
            "id": "1",
            "provider": provider,
            "api_key": "sk-direct",
            "metadata": {"instance_key": ""},
        }
    ]
    return v


@pytest.mark.parametrize("vec", _CREDENTIAL_ROUTING, ids=_ids(_CREDENTIAL_ROUTING))
def test_credential_routing_vector(vec: dict) -> None:
    provider = "someprovider"
    entry = {
        "provider_id": provider,
        "instance_key": "",
        "plan": {},
        "credential": vec["entry"]["credential"],
        "public_config": vec["entry"]["public_config"],
    }
    v = _vault_with_direct_entry(provider) if vec["vault_has_entry"] else _empty_vault()

    try:
        orchestrator._resolve_credential(entry, v, None)
        got = {"kind": "credential"}
    except orchestrator.CredentialSkip as exc:
        got = {"kind": "skip", "reason": exc.reason}
    except orchestrator.CredentialMissing:
        got = {"kind": "missing"}

    assert got == vec["expect"]

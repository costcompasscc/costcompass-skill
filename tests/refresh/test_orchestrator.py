from __future__ import annotations

import base64
import json
import os
import time

import httpx
import pytest

from costcompass import api, config, vault
from costcompass.refresh import broker, oauth, orchestrator

PASSWORD = "pw"


def _vault_blob(entries):
    doc = {"schema_version": 1, "entries": entries}
    return vault.encrypt_jwe(
        json.dumps(doc).encode(), PASSWORD, os.urandom(16), vault.DEFAULT_PBKDF2_ITERS
    )


def _flat_run():
    return {
        "run_id": "run-1",
        "fetches": [
            {
                "provider_id": "anthropic",
                "instance_key": "",
                "state": "pending",
                "signing_token": "tok-1",
                "plan": {
                    "requests": [
                        {
                            "url": "https://api.anthropic.com/v1/usage",
                            "method": "GET",
                            "headers": {},
                            "body": None,
                            "purpose": "usage",
                        }
                    ],
                    "auth_header": "x-api-key",
                    "auth_scheme": None,
                },
            }
        ],
    }


def test_flat_refresh_end_to_end():
    captured = {}
    blob = _vault_blob(
        [
            {
                "id": "1",
                "provider": "anthropic",
                "api_key": "sk-ant",
                "metadata": {"instance_key": ""},
            }
        ]
    )

    def api_handler(request: httpx.Request) -> httpx.Response:
        p, m = request.url.path, request.method
        if m == "GET" and p.endswith("/vault"):
            return httpx.Response(
                200, json={"jwe": blob, "revision": 1, "updated_at": "x"}
            )
        if m == "POST" and p.endswith("/fetch-runs"):
            captured["create"] = json.loads(request.content)
            return httpx.Response(200, json=_flat_run())
        if "/responses" in p:
            captured["responses"] = json.loads(request.content)
            return httpx.Response(200, json={"state": "success", "events_ingested": 3})
        if "/finalize" in p:
            captured["finalize"] = True
            return httpx.Response(
                200, json={"run_id": "run-1", "status": "success", "providers": []}
            )
        if p.endswith("/dashboard/summary"):
            return httpx.Response(200, json={"mtd_usd": 9.0})
        return httpx.Response(404, json={"error": p})

    def broker_handler(request: httpx.Request) -> httpx.Response:
        captured["broker_req"] = json.loads(request.content)
        captured["broker_auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={"status": 200, "headers": {}, "body": "Yg==", "signature": "sig-xyz"},
        )

    client = api.Client(
        "https://x/api/v1",
        "sk-cli",
        http=httpx.Client(transport=httpx.MockTransport(api_handler)),
    )
    brk = broker.BrokerClient(
        "https://x/broker/v1",
        "sk-cli",
        http=httpx.Client(transport=httpx.MockTransport(broker_handler)),
    )
    cfg = config.Config(api_key="sk-cli", api_url="https://x/api/v1")

    result = orchestrator.run(
        cfg, "sk-cli", None, PASSWORD, client=client, broker=brk, echo=lambda *_: None
    )

    # full sequence ran
    assert captured["create"]["providers"] is None
    assert captured["finalize"] is True
    # provider auth came from the vault, broker auth came from the CLI key
    assert captured["broker_req"]["headers"]["x-api-key"] == "sk-ant"
    assert captured["broker_auth"] == "Bearer sk-cli"
    # broker signature relayed verbatim into the submit
    submitted = captured["responses"]
    assert submitted["provider_id"] == "anthropic"
    assert submitted["responses"][0]["signature"] == "sig-xyz"
    assert submitted["responses"][0]["body_b64"] == "Yg=="
    assert result.outcomes[0].state == "success"
    assert result.outcomes[0].events_ingested == 3
    assert result.mtd_usd == 9.0


def test_progress_ticker_emits_dots_and_trailing_newline():
    writes: list[str] = []
    with orchestrator._ProgressTicker(True, writes.append, interval=0.01):
        time.sleep(0.05)
    assert writes.count(".") >= 1  # ticked while "in flight"
    assert writes[-1] == "\n"  # one clean newline before results


def test_progress_ticker_disabled_is_silent():
    writes: list[str] = []
    with orchestrator._ProgressTicker(False, writes.append, interval=0.01):
        time.sleep(0.03)
    assert writes == []  # no thread, no output


def test_progress_no_newline_when_no_ticks():
    # A fast refresh (mock completes well under the 1s interval) prints nothing
    # — and crucially no stray trailing newline. Exercises the run() wiring.
    captured: list[str] = []
    blob = _vault_blob(
        [
            {
                "id": "1",
                "provider": "anthropic",
                "api_key": "sk-ant",
                "metadata": {"instance_key": ""},
            }
        ]
    )

    def api_handler(request: httpx.Request) -> httpx.Response:
        p, m = request.url.path, request.method
        if m == "GET" and p.endswith("/vault"):
            return httpx.Response(
                200, json={"jwe": blob, "revision": 1, "updated_at": "x"}
            )
        if m == "POST" and p.endswith("/fetch-runs"):
            return httpx.Response(200, json=_flat_run())
        if "/responses" in p:
            return httpx.Response(200, json={"state": "success", "events_ingested": 1})
        if "/finalize" in p:
            return httpx.Response(
                200, json={"run_id": "run-1", "status": "success", "providers": []}
            )
        if p.endswith("/dashboard/summary"):
            return httpx.Response(200, json={"mtd_usd": 1.0})
        return httpx.Response(404)

    client = api.Client(
        "https://x/api/v1",
        "sk-cli",
        http=httpx.Client(transport=httpx.MockTransport(api_handler)),
    )
    brk = broker.BrokerClient(
        "https://x/broker/v1",
        "sk-cli",
        http=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={
                        "status": 200,
                        "headers": {},
                        "body": "Yg==",
                        "signature": "s",
                    },
                )
            )
        ),
    )
    cfg = config.Config(api_key="sk-cli", api_url="https://x/api/v1")
    orchestrator.run(
        cfg,
        "sk-cli",
        None,
        PASSWORD,
        client=client,
        broker=brk,
        echo=lambda *_: None,
        progress=True,
        progress_write=captured.append,
    )
    assert captured == []


def test_wrong_vault_password_raises_refresh_error():
    blob = _vault_blob(
        [
            {
                "id": "1",
                "provider": "anthropic",
                "api_key": "sk-ant",
                "metadata": {"instance_key": ""},
            }
        ]
    )

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/vault"):
            return httpx.Response(
                200, json={"jwe": blob, "revision": 1, "updated_at": "x"}
            )
        return httpx.Response(404)

    client = api.Client(
        "https://x/api/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(api_handler)),
    )
    cfg = config.Config(api_key="sk", api_url="https://x/api/v1")
    # A wrong password must surface as a clean RefreshError, not a raw VaultError.
    with pytest.raises(orchestrator.RefreshError, match="could not decrypt"):
        orchestrator.run(
            cfg,
            "sk",
            None,
            "WRONG-PASSWORD",
            client=client,
            broker=object(),
            oauth_client=object(),
            echo=lambda *_: None,
        )


def test_keyless_card_raises_credential_skip():
    # A flat/keyless card with no vault entry is a benign skip (subscription-
    # only), NOT an auth failure — mirrors the browser's subscription-only skip.
    blob = _vault_blob([])
    v = vault.Vault(
        doc=json.loads(vault.decrypt_jwe(blob, PASSWORD)[0]),
        p2s=os.urandom(16),
        p2c=vault.DEFAULT_PBKDF2_ITERS,
        revision=1,
    )
    entry = {"provider_id": "anthropic", "instance_key": "", "plan": {}}
    with pytest.raises(orchestrator.CredentialSkip, match="no credential configured"):
        orchestrator._resolve_credential(entry, v, None)


def test_oauth_mint_routing_passes_server_sentinel_and_path():
    # The resolver is invoked with the SERVER-supplied sentinel_key + mint_path
    # from the entry's credential routing — the CLI hardcodes no provider table.
    v = vault.Vault(
        doc={"entries": []},
        p2s=os.urandom(16),
        p2c=vault.DEFAULT_PBKDF2_ITERS,
        revision=1,
    )
    seen = {}

    class FakeResolver:
        def access_token(self, provider, sentinel_key, mint_path):
            seen.update(
                provider=provider, sentinel_key=sentinel_key, mint_path=mint_path
            )
            return "AT"

    entry = {
        "provider_id": "google",
        "instance_key": "card-1",
        "plan": {},
        "credential": {
            "kind": "oauth_mint",
            "sentinel_key": "__google_oauth__",
            "mint_path": "/google/mint",
        },
    }
    assert orchestrator._resolve_credential(entry, v, FakeResolver()) == "AT"
    assert seen == {
        "provider": "google",
        "sentinel_key": "__google_oauth__",
        "mint_path": "/google/mint",
    }


def test_oauth_installation_grant_routing_raises_credential_skip():
    # The server routes an Organization App row to "oauth_installation_grant",
    # a kind the CLI doesn't implement — skip it cleanly (no provider knowledge
    # in the untrusted relay; it acts purely on the server-authored routing kind).
    v = vault.Vault(
        doc={"entries": []},
        p2s=os.urandom(16),
        p2c=vault.DEFAULT_PBKDF2_ITERS,
        revision=1,
    )
    entry = {
        "provider_id": "github",
        "instance_key": "acct-1",
        "plan": {},
        "credential": {"kind": "oauth_installation_grant"},
    }
    with pytest.raises(orchestrator.CredentialSkip, match="installation grant"):
        orchestrator._resolve_credential(entry, v, None)


def test_unknown_future_kind_raises_credential_skip():
    # A kind the CLI doesn't implement (a future server-authored routing) must
    # skip cleanly, never crash — the untrusted relay acts only on kinds it knows.
    v = vault.Vault(
        doc={"entries": []},
        p2s=os.urandom(16),
        p2c=vault.DEFAULT_PBKDF2_ITERS,
        revision=1,
    )
    entry = {
        "provider_id": "someprovider",
        "instance_key": "c",
        "plan": {},
        "credential": {"kind": "mtls_cert"},
    }
    with pytest.raises(orchestrator.CredentialSkip, match="no credential configured"):
        orchestrator._resolve_credential(entry, v, None)


def test_malformed_oauth_mint_routing_distinct_skip():
    # oauth_mint routing missing sentinel_key/mint_path is a plugin bug, not a
    # keyless card — surface a distinguishable message even with a resolver set.
    v = vault.Vault(
        doc={"entries": []},
        p2s=os.urandom(16),
        p2c=vault.DEFAULT_PBKDF2_ITERS,
        revision=1,
    )

    class FakeResolver:
        def access_token(self, provider, sentinel_key, mint_path):
            raise AssertionError("resolver must not be called for malformed routing")

    entry = {
        "provider_id": "google",
        "instance_key": "card-1",
        "plan": {},
        "credential": {"kind": "oauth_mint"},
    }
    with pytest.raises(
        orchestrator.CredentialSkip, match="malformed oauth_mint routing"
    ):
        orchestrator._resolve_credential(entry, v, FakeResolver())


def _run_one_entry_capture(blob, run, response_json):
    """Drive ``run()`` against a single-entry fetch-run, capturing the submitted
    /responses payload. Returns (outcomes, captured_responses_payload)."""
    captured: dict = {}

    def api_handler(request: httpx.Request) -> httpx.Response:
        p, m = request.url.path, request.method
        if m == "GET" and p.endswith("/vault"):
            return httpx.Response(
                200, json={"jwe": blob, "revision": 1, "updated_at": "x"}
            )
        if m == "POST" and p.endswith("/fetch-runs"):
            return httpx.Response(200, json=run)
        if "/responses" in p:
            captured["responses"] = json.loads(request.content)
            return httpx.Response(200, json=response_json)
        if "/finalize" in p:
            return httpx.Response(
                200, json={"run_id": "run-1", "status": "x", "providers": []}
            )
        if p.endswith("/dashboard/summary"):
            return httpx.Response(200, json={"mtd_usd": 0.0})
        return httpx.Response(404)

    client = api.Client(
        "https://x/api/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(api_handler)),
    )
    cfg = config.Config(api_key="sk", api_url="https://x/api/v1")
    result = orchestrator.run(
        cfg,
        "sk",
        None,
        PASSWORD,
        client=client,
        broker=object(),
        oauth_client=object(),
        echo=lambda *_: None,
    )
    return result.outcomes, captured["responses"]


def test_keyless_card_submits_no_credentials_skip():
    # No vault credential for an enabled flat card → ONE no_credentials
    # synthetic (204), so the server records ``skipped`` — not a 401 reauth.
    blob = _vault_blob([])
    outcomes, submitted = _run_one_entry_capture(
        blob, _flat_run(), {"state": "skipped", "events_ingested": 0}
    )
    r = submitted["responses"][0]
    assert r["status"] == 204
    assert r["synthetic"] is True
    assert r["synthetic_reason"] == "no_credentials"
    assert outcomes[0].state == "skipped"


def test_oauth_mint_failure_preserves_status():
    # An OAuth mint that fails with a taxonomy status (429) must be relayed as
    # a NON-synthetic provider-error carrying that status, not collapsed to 401.
    blob = _vault_blob(
        [
            {
                "id": "g",
                "provider": "google",
                "api_key": "rt",
                "metadata": {"instance_key": "__google_oauth__"},
            }
        ]
    )
    run = {
        "run_id": "run-1",
        "fetches": [
            {
                "provider_id": "google",
                "instance_key": "proj-1",
                "state": "pending",
                "signing_token": "tok-1",
                "credential": {
                    "kind": "oauth_mint",
                    "sentinel_key": "__google_oauth__",
                    "mint_path": "/google/mint",
                },
                "plan": {
                    "requests": [
                        {
                            "url": "https://h/p",
                            "method": "GET",
                            "headers": {},
                            "body": None,
                            "purpose": "ts",
                        }
                    ],
                    "auth_header": "authorization",
                    "auth_scheme": "Bearer",
                },
            }
        ],
    }

    def api_handler(request: httpx.Request) -> httpx.Response:
        p, m = request.url.path, request.method
        if m == "GET" and p.endswith("/vault"):
            return httpx.Response(
                200, json={"jwe": blob, "revision": 1, "updated_at": "x"}
            )
        if m == "POST" and p.endswith("/fetch-runs"):
            return httpx.Response(200, json=run)
        if "/responses" in p:
            api_handler.submitted = json.loads(request.content)
            return httpx.Response(
                200, json={"state": "rate_limited", "events_ingested": 0}
            )
        if "/finalize" in p:
            return httpx.Response(
                200, json={"run_id": "run-1", "status": "x", "providers": []}
            )
        if p.endswith("/dashboard/summary"):
            return httpx.Response(200, json={"mtd_usd": 0.0})
        return httpx.Response(404)

    client = api.Client(
        "https://x/api/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(api_handler)),
    )
    # oauth-broker returns 429 on the mint.
    oauth_client = oauth.OAuthBrokerClient(
        "https://x/oauth/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429))),
    )
    cfg = config.Config(api_key="sk", api_url="https://x/api/v1")
    result = orchestrator.run(
        cfg,
        "sk",
        None,
        PASSWORD,
        client=client,
        broker=object(),
        oauth_client=oauth_client,
        echo=lambda *_: None,
    )
    r = api_handler.submitted["responses"][0]
    assert r["status"] == 429  # preserved from the mint, NOT 401
    assert "synthetic" not in r  # non-synthetic → server classifies it
    assert r["request_purpose"] == "ts"
    assert result.outcomes[0].state == "rate_limited"


def test_oauth_mint_409_marks_body_reauth_required():
    # A 409 mint rejection (dead OAuth grant) must relay a body carrying the
    # reauth_required code so the App Server's shared reauth classifier fires.
    # Without the code, a message-only body is misfiled as a generic failure.
    blob = _vault_blob(
        [
            {
                "id": "g",
                "provider": "google",
                "api_key": "rt",
                "metadata": {"instance_key": "__google_oauth__"},
            }
        ]
    )
    run = {
        "run_id": "run-1",
        "fetches": [
            {
                "provider_id": "google",
                "instance_key": "proj-1",
                "state": "pending",
                "signing_token": "tok-1",
                "credential": {
                    "kind": "oauth_mint",
                    "sentinel_key": "__google_oauth__",
                    "mint_path": "/google/mint",
                },
                "plan": {
                    "requests": [
                        {
                            "url": "https://h/p",
                            "method": "GET",
                            "headers": {},
                            "body": None,
                            "purpose": "ts",
                        }
                    ],
                    "auth_header": "authorization",
                    "auth_scheme": "Bearer",
                },
            }
        ],
    }

    def api_handler(request: httpx.Request) -> httpx.Response:
        p, m = request.url.path, request.method
        if m == "GET" and p.endswith("/vault"):
            return httpx.Response(
                200, json={"jwe": blob, "revision": 1, "updated_at": "x"}
            )
        if m == "POST" and p.endswith("/fetch-runs"):
            return httpx.Response(200, json=run)
        if "/responses" in p:
            api_handler.submitted = json.loads(request.content)
            return httpx.Response(
                200, json={"state": "reauth_required", "events_ingested": 0}
            )
        if "/finalize" in p:
            return httpx.Response(
                200, json={"run_id": "run-1", "status": "x", "providers": []}
            )
        if p.endswith("/dashboard/summary"):
            return httpx.Response(200, json={"mtd_usd": 0.0})
        return httpx.Response(404)

    client = api.Client(
        "https://x/api/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(api_handler)),
    )
    # oauth-broker returns 409 reauth_required on the mint.
    oauth_client = oauth.OAuthBrokerClient(
        "https://x/oauth/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(409))),
    )
    cfg = config.Config(api_key="sk", api_url="https://x/api/v1")
    result = orchestrator.run(
        cfg,
        "sk",
        None,
        PASSWORD,
        client=client,
        broker=object(),
        oauth_client=oauth_client,
        echo=lambda *_: None,
    )
    r = api_handler.submitted["responses"][0]
    assert r["status"] == 409  # preserved from the mint
    body = json.loads(base64.b64decode(r["body_b64"]))
    assert body["error"]["code"] == "reauth_required"
    assert result.outcomes[0].state == "reauth_required"


def test_scoped_refresh_shows_label_and_scopes_summary():
    # A per-service refresh must (a) show the human card label, not the raw
    # instance_key UUID, and (b) close with that service's MTD, not the total.
    blob = _vault_blob(
        [
            {
                "id": "g",
                "provider": "google",
                "api_key": "rt",
                "metadata": {"instance_key": "__google_oauth__"},
            },
        ]
    )
    run = {
        "run_id": "run-1",
        "fetches": [
            {
                "provider_id": "google",
                "instance_key": "card-uuid-1",
                "instance_label": "Prod billing",
                "state": "pending",
                "signing_token": "tok-1",
                "credential": {
                    "kind": "oauth_mint",
                    "sentinel_key": "__google_oauth__",
                    "mint_path": "/google/mint",
                },
                "plan": {
                    "requests": [
                        {
                            "url": "https://h/p",
                            "method": "GET",
                            "headers": {},
                            "body": None,
                            "purpose": "ts",
                        }
                    ],
                    "auth_header": "authorization",
                    "auth_scheme": "Bearer",
                },
            }
        ],
    }
    seen = {}

    def api_handler(request: httpx.Request) -> httpx.Response:
        p, m = request.url.path, request.method
        if m == "GET" and p.endswith("/vault"):
            return httpx.Response(
                200, json={"jwe": blob, "revision": 1, "updated_at": "x"}
            )
        if m == "GET" and p.endswith("/providers"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "google",
                        "short_name": "Google",
                        "display_name": "Google Cloud",
                        "enabled": True,
                    }
                ],
            )
        if m == "POST" and p.endswith("/fetch-runs"):
            return httpx.Response(200, json=run)
        if "/responses" in p:
            return httpx.Response(200, json={"state": "success", "events_ingested": 20})
        if "/finalize" in p:
            return httpx.Response(
                200, json={"run_id": "run-1", "status": "success", "providers": []}
            )
        if p.endswith("/dashboard/summary"):
            seen["summary_provider"] = request.url.params.get("provider")
            return httpx.Response(200, json={"mtd_usd": 42.0})
        return httpx.Response(404)

    brk = broker.BrokerClient(
        "https://x/broker/v1",
        "sk",
        http=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={
                        "status": 200,
                        "headers": {},
                        "body": "Yg==",
                        "signature": "s",
                    },
                )
            )
        ),
    )
    oauth_client = oauth.OAuthBrokerClient(
        "https://x/oauth/v1",
        "sk",
        http=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, json={"access_token": "at", "expires_at_utc_secs": 9999}
                )
            )
        ),
    )
    client = api.Client(
        "https://x/api/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(api_handler)),
    )
    cfg = config.Config(api_key="sk", api_url="https://x/api/v1")
    lines: list[str] = []
    result = orchestrator.run(
        cfg,
        "sk",
        "google",
        PASSWORD,
        client=client,
        broker=brk,
        oauth_client=oauth_client,
        echo=lines.append,
    )

    assert any("[Prod billing]" in ln for ln in lines)  # label shown
    assert all("card-uuid-1" not in ln for ln in lines)  # raw UUID hidden
    assert seen["summary_provider"] == "google"  # MTD scoped to the service
    assert result.outcomes[0].instance_label == "Prod billing"
    assert result.mtd_usd == 42.0


def test_program_forward_cap_submits_synthetic_and_finalizes():
    # A runaway program poll loop that trips the per-entry forward cap must
    # become ONE synthetic ``forward_cap_exceeded`` submit for that card —
    # never an exception that aborts the run and leaves it unfinalized.
    # Mirrors the browser's submitForwardCapExceeded and the macOS runPlan.
    blob = _vault_blob(
        [
            {
                "id": "1",
                "provider": "google",
                "api_key": "k",
                "metadata": {"instance_key": ""},
            }
        ]
    )
    step = {
        "kind": "request",
        "request": {
            "host": "h",
            "path": "/p",
            "method": "GET",
            "headers": {},
            "purpose": "bq",
        },
    }
    run = {
        "run_id": "run-1",
        "fetches": [
            {
                "provider_id": "google",
                "instance_key": "",
                "state": "pending",
                "signing_token": "tok-1",
                "plan": {
                    "program": {"purpose": "bq", "bindings": {}, "steps": [step, step]},
                    "auth_header": "authorization",
                    "auth_scheme": "Bearer",
                },
            }
        ],
    }
    captured: dict = {}

    def api_handler(request: httpx.Request) -> httpx.Response:
        p, m = request.url.path, request.method
        if m == "GET" and p.endswith("/vault"):
            return httpx.Response(
                200, json={"jwe": blob, "revision": 1, "updated_at": "x"}
            )
        if m == "POST" and p.endswith("/fetch-runs"):
            return httpx.Response(200, json=run)
        if "/responses" in p:
            captured["responses"] = json.loads(request.content)
            return httpx.Response(
                200, json={"state": "forward_cap_exceeded", "events_ingested": 0}
            )
        if "/finalize" in p:
            captured["finalize"] = True
            return httpx.Response(
                200, json={"run_id": "run-1", "status": "x", "providers": []}
            )
        if p.endswith("/dashboard/summary"):
            return httpx.Response(200, json={"mtd_usd": 0.0})
        return httpx.Response(404)

    client = api.Client(
        "https://x/api/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(api_handler)),
    )
    # forward_cap=1: the program's first forward succeeds, the second trips.
    brk = broker.BrokerClient(
        "https://x/broker/v1",
        "sk",
        forward_cap=1,
        http=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={
                        "status": 200,
                        "headers": {},
                        "body": "Yg==",
                        "signature": "s",
                    },
                )
            )
        ),
    )
    cfg = config.Config(api_key="sk", api_url="https://x/api/v1")
    result = orchestrator.run(
        cfg,
        "sk",
        None,
        PASSWORD,
        client=client,
        broker=brk,
        oauth_client=object(),
        echo=lambda *_: None,
    )

    submitted = captured["responses"]["responses"]
    assert len(submitted) == 1  # collected responses discarded, one stub only
    assert submitted[0]["status"] == 429
    assert submitted[0]["synthetic"] is True
    assert submitted[0]["synthetic_reason"] == "forward_cap_exceeded"
    assert submitted[0]["request_purpose"] == "bq"  # program purpose
    assert submitted[0]["request_url"] == "synthetic://forward_cap_exceeded"
    assert captured["finalize"] is True  # run still finalized
    assert result.outcomes[0].state == "forward_cap_exceeded"


def test_flat_broker_error_classified_not_502():
    # A transient broker failure that exhausts retries relays a CLASSIFIED
    # provider-error (rate_limited → 429), not a generic 502, without aborting.
    brk = broker.BrokerClient(
        "https://x/broker/v1",
        "sk",
        http=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    429, json={"error": {"code": "rate_limited", "message": "slow"}}
                )
            )
        ),
        retry_attempts=1,
        sleep=lambda *_: None,
    )
    plan = {
        "requests": [
            {"url": "https://h/p", "method": "GET", "headers": {}, "purpose": "u"}
        ],
        "auth_header": "x",
        "auth_scheme": None,
    }
    out = orchestrator._run_flat(plan, {"x": "k"}, "tok", brk)
    assert out[0]["status"] == 429  # mapped from rate_limited, not a generic 502
    assert "synthetic" not in out[0]


def test_submit_failure_degrades_with_a_reason():
    # The normal fetch→submit path passes no local_message. A submit failure
    # must still surface a non-empty error_message so the card isn't a blank,
    # undiagnosable "failed".
    class _FailingSubmit:
        def submit_responses(self, run_id, payload):
            raise api.ApiError("network down")

    outcome = orchestrator._submit_outcome(
        _FailingSubmit(),
        "run-1",
        "anthropic",
        "",
        responses=[],
        fallback_state="failed",
    )
    assert outcome.state == "failed"
    assert outcome.error_message  # non-empty reason attached

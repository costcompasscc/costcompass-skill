from __future__ import annotations

import json
import os

import httpx
import pytest

from costcompass import api, vault
from costcompass.refresh import oauth

PASSWORD = "pw"


def _vault_with_sentinel(refresh_token="rt-old"):
    doc = {
        "schema_version": 1,
        "entries": [
            {
                "id": "g",
                "provider": "google",
                "api_key": refresh_token,
                "metadata": {"instance_key": "__google_oauth__"},
            },
        ],
    }
    blob = vault.encrypt_jwe(
        json.dumps(doc).encode(), PASSWORD, os.urandom(16), vault.DEFAULT_PBKDF2_ITERS
    )
    plaintext, p2s, p2c = vault.decrypt_jwe(blob, PASSWORD)
    return vault.Vault(doc=json.loads(plaintext), p2s=p2s, p2c=p2c, revision=4)


def test_oauth_url_from_api():
    assert (
        oauth.oauth_url_from_api("https://costcompass.cc/api/v1")
        == "https://costcompass.cc/oauth/v1"
    )
    assert (
        oauth.oauth_url_from_api("http://localhost:8080/api/v1/")
        == "http://localhost:8080/oauth/v1"
    )


def test_mint_sends_bearer_and_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"access_token": "at-1", "expires_at_utc_secs": 9999}
        )

    client = oauth.OAuthBrokerClient(
        "https://x/oauth/v1",
        "sk-cli",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    out = client.mint("/google/mint", "rt-old")
    assert out["access_token"] == "at-1"
    assert seen["auth"] == "Bearer sk-cli"
    assert seen["path"] == "/oauth/v1/google/mint"
    assert seen["body"] == {"refresh_token": "rt-old"}


def test_mint_401_raises():
    client = oauth.OAuthBrokerClient(
        "https://x/oauth/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401))),
    )
    with pytest.raises(oauth.OAuthError, match="reconnect") as exc:
        client.mint("/google/mint", "rt")
    assert exc.value.status == 401


def test_mint_429_preserves_status():
    # A rate-limit from the oauth-broker must carry status 429 so the server
    # classifies it as rate_limited, not a 401 reauth.
    client = oauth.OAuthBrokerClient(
        "https://x/oauth/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429))),
    )
    with pytest.raises(oauth.OAuthError) as exc:
        client.mint("/google/mint", "rt")
    assert exc.value.status == 429


def test_mint_5xx_preserves_status():
    client = oauth.OAuthBrokerClient(
        "https://x/oauth/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))),
    )
    with pytest.raises(oauth.OAuthError) as exc:
        client.mint("/google/mint", "rt")
    assert exc.value.status == 503


def test_mint_network_error_is_transient_502():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = oauth.OAuthBrokerClient(
        "https://x/oauth/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(oauth.OAuthError) as exc:
        client.mint("/google/mint", "rt")
    assert exc.value.status == 502


def test_resolver_mints_and_caches():
    calls = {"mint": 0}

    def oauth_handler(request: httpx.Request) -> httpx.Response:
        calls["mint"] += 1
        return httpx.Response(
            200, json={"access_token": "at-x", "expires_at_utc_secs": 9999}
        )

    oauth_client = oauth.OAuthBrokerClient(
        "https://x/oauth/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(oauth_handler)),
    )
    api_client = api.Client(
        "https://x/api/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
    )
    resolver = oauth.OAuthResolver(
        oauth_client, api_client, _vault_with_sentinel(), PASSWORD
    )

    assert resolver.access_token("google", "__google_oauth__", "/google/mint") == "at-x"
    assert (
        resolver.access_token("google", "__google_oauth__", "/google/mint") == "at-x"
    )  # cached
    assert calls["mint"] == 1


def test_resolver_writes_back_rotated_token():
    put_seen = {}

    def oauth_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "at-x",
                "expires_at_utc_secs": 9999,
                "refresh_token": "rt-new",
            },
        )

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path.endswith("/vault"):
            put_seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"revision": 5, "updated_at": "y"})
        return httpx.Response(404)

    oauth_client = oauth.OAuthBrokerClient(
        "https://x/oauth/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(oauth_handler)),
    )
    api_client = api.Client(
        "https://x/api/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(api_handler)),
    )
    v = _vault_with_sentinel("rt-old")
    resolver = oauth.OAuthResolver(oauth_client, api_client, v, PASSWORD)

    resolver.access_token("google", "__google_oauth__", "/google/mint")
    # sentinel updated in-memory and persisted
    assert v.entry_for("google", "__google_oauth__")["api_key"] == "rt-new"
    assert put_seen["body"]["expected_revision"] == 4
    assert v.revision == 5


def test_resolver_write_back_failure_raises_oauth_error():
    # Mint rotates the token, but persisting it (PUT /vault) fails — this must
    # surface as a per-card OAuthError, not an uncaught ApiError that aborts the run.
    def oauth_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "at",
                "expires_at_utc_secs": 9999,
                "refresh_token": "rt-new",
            },
        )

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(409, json={"error": "revision conflict"})
        return httpx.Response(404)

    oauth_client = oauth.OAuthBrokerClient(
        "https://x/oauth/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(oauth_handler)),
    )
    api_client = api.Client(
        "https://x/api/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(api_handler)),
    )
    resolver = oauth.OAuthResolver(
        oauth_client, api_client, _vault_with_sentinel("rt-old"), PASSWORD
    )
    with pytest.raises(oauth.OAuthError, match="could not persist"):
        resolver.access_token("google", "__google_oauth__", "/google/mint")


def test_mint_409_is_reauth():
    client = oauth.OAuthBrokerClient(
        "https://x/oauth/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(409))),
    )
    with pytest.raises(oauth.OAuthError, match="reconnect") as exc:
        client.mint("/google/mint", "rt")
    assert exc.value.status == 409  # preserved → server records reauth_required


def _vault_blob(refresh_token="rt-old", revision=4):
    """A GET /vault response body ({jwe, revision}) with one google sentinel."""
    doc = {
        "schema_version": 1,
        "entries": [
            {
                "id": "g",
                "provider": "google",
                "api_key": refresh_token,
                "metadata": {"instance_key": "__google_oauth__"},
            }
        ],
    }
    jwe = vault.encrypt_jwe(
        json.dumps(doc).encode(), PASSWORD, os.urandom(16), vault.DEFAULT_PBKDF2_ITERS
    )
    return {"jwe": jwe, "revision": revision, "updated_at": "t"}


def test_resolver_retries_mint_after_reauth_with_server_fresh_token():
    # Another relay rotated the shared sentinel out from under us: our first
    # mint (rt-old) is rejected 409, but the server vault already holds the
    # fresher rt-fresh. The resolver must re-read the vault and retry once.
    mint_calls = []

    def oauth_handler(request: httpx.Request) -> httpx.Response:
        token = json.loads(request.content)["refresh_token"]
        mint_calls.append(token)
        if token == "rt-old":
            return httpx.Response(409, json={"error": {"code": "reauth_required"}})
        return httpx.Response(
            200,
            json={
                "access_token": "at-ok",
                "expires_at_utc_secs": 9999,
                "refresh_token": "rt-newer",
            },
        )

    put_seen = {}

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/vault"):
            return httpx.Response(200, json=_vault_blob("rt-fresh", revision=7))
        if request.method == "PUT" and request.url.path.endswith("/vault"):
            put_seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"revision": 8, "updated_at": "z"})
        return httpx.Response(404)

    oauth_client = oauth.OAuthBrokerClient(
        "https://x/oauth/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(oauth_handler)),
    )
    api_client = api.Client(
        "https://x/api/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(api_handler)),
    )
    resolver = oauth.OAuthResolver(
        oauth_client, api_client, _vault_with_sentinel("rt-old"), PASSWORD
    )

    assert (
        resolver.access_token("google", "__google_oauth__", "/google/mint") == "at-ok"
    )
    # First mint used the stale token, second used the server-fresh one.
    assert mint_calls == ["rt-old", "rt-fresh"]
    # The retry's own rotation persisted against the FRESH revision (7), not
    # our original stale 4 — else the write-back would conflict again.
    assert put_seen["body"]["expected_revision"] == 7


def test_resolver_reauth_retry_second_409_propagates():
    # Retry-once: if the retried mint (with the server-fresh token) ALSO 409s,
    # that failure propagates — the resolver does not loop.
    mint_calls = []

    def oauth_handler(request: httpx.Request) -> httpx.Response:
        mint_calls.append(json.loads(request.content)["refresh_token"])
        return httpx.Response(409, json={"error": {"code": "reauth_required"}})

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/vault"):
            return httpx.Response(200, json=_vault_blob("rt-fresh", revision=7))
        return httpx.Response(404)

    oauth_client = oauth.OAuthBrokerClient(
        "https://x/oauth/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(oauth_handler)),
    )
    api_client = api.Client(
        "https://x/api/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(api_handler)),
    )
    resolver = oauth.OAuthResolver(
        oauth_client, api_client, _vault_with_sentinel("rt-old"), PASSWORD
    )

    with pytest.raises(oauth.OAuthError) as exc:
        resolver.access_token("google", "__google_oauth__", "/google/mint")
    assert exc.value.status == 409
    assert mint_calls == ["rt-old", "rt-fresh"]  # exactly one retry, then propagate


def test_resolver_reauth_surfaces_when_server_token_unchanged():
    # 409, but the server vault holds the SAME token we already tried — a
    # genuine dead grant, not a desync. No retry; surface reauth (409).
    mint_calls = []

    def oauth_handler(request: httpx.Request) -> httpx.Response:
        mint_calls.append(json.loads(request.content)["refresh_token"])
        return httpx.Response(409, json={"error": {"code": "reauth_required"}})

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/vault"):
            return httpx.Response(200, json=_vault_blob("rt-old", revision=4))
        return httpx.Response(404)

    oauth_client = oauth.OAuthBrokerClient(
        "https://x/oauth/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(oauth_handler)),
    )
    api_client = api.Client(
        "https://x/api/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(api_handler)),
    )
    resolver = oauth.OAuthResolver(
        oauth_client, api_client, _vault_with_sentinel("rt-old"), PASSWORD
    )

    with pytest.raises(oauth.OAuthError) as exc:
        resolver.access_token("google", "__google_oauth__", "/google/mint")
    assert exc.value.status == 409  # preserved → server records reauth_required
    assert mint_calls == ["rt-old"]  # no wasted retry on an unchanged token


def test_resolver_non_reauth_failure_does_not_reload():
    # A 429 (or any non-409) can't be helped by a fresher token — no vault
    # re-read, status preserved for correct server classification.
    reloaded = {"get_vault": 0}

    def oauth_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/vault"):
            reloaded["get_vault"] += 1
        return httpx.Response(404)

    oauth_client = oauth.OAuthBrokerClient(
        "https://x/oauth/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(oauth_handler)),
    )
    api_client = api.Client(
        "https://x/api/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(api_handler)),
    )
    resolver = oauth.OAuthResolver(
        oauth_client, api_client, _vault_with_sentinel("rt-old"), PASSWORD
    )

    with pytest.raises(oauth.OAuthError) as exc:
        resolver.access_token("google", "__google_oauth__", "/google/mint")
    assert exc.value.status == 429
    assert reloaded["get_vault"] == 0


def test_resolver_no_sentinel_raises():
    empty = vault.Vault(
        doc={"entries": []},
        p2s=os.urandom(16),
        p2c=vault.DEFAULT_PBKDF2_ITERS,
        revision=1,
    )
    oauth_client = oauth.OAuthBrokerClient(
        "https://x/oauth/v1",
        "sk",
        http=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        ),
    )
    api_client = api.Client(
        "https://x/api/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
    )
    resolver = oauth.OAuthResolver(oauth_client, api_client, empty, PASSWORD)
    with pytest.raises(oauth.OAuthError, match="Reconnect"):
        resolver.access_token("google", "__google_oauth__", "/google/mint")

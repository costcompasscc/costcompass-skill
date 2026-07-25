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


def test_mint_non_json_success_is_transient_502():
    # A 2xx with a non-JSON body (proxy page) is broker-side breakage — a
    # clean 502 OAuthError, never a raw ValueError, and never echoing the body.
    client = oauth.OAuthBrokerClient(
        "https://x/oauth/v1",
        "sk",
        http=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, text="<html>proxy says hi</html>")
            )
        ),
    )
    with pytest.raises(oauth.OAuthError) as exc:
        client.mint("/google/mint", "rt")
    assert exc.value.status == 502
    assert "proxy says hi" not in str(exc.value)


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
    # Mint rotates the token, but persisting it (PUT /vault) can never land —
    # this must surface as a per-card OAuthError, not an uncaught ApiError that
    # aborts the run. With no readable vault to re-read, the retry can't run.
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
    with pytest.raises(oauth.OAuthError, match="could not save it") as exc:
        resolver.access_token("google", "__google_oauth__", "/google/mint")
    # 409 so the orchestrator tags the body reauth_required: the old token is
    # dead upstream, so an unsaved rotation is a dead grant, not a blip.
    assert exc.value.status == 409


# ---- rotation write-back: revision-conflict retry ----------------------
#
# Jitter is forced OFF in every test below. The retry's correctness rests
# entirely on the server's compare-and-set, never on a delay, so the whole
# block runs with zero backoff — a regression that leaned on timing would fail
# here rather than pass by luck on a slow machine.

NO_JITTER = {"sleep": lambda _s: None, "random": lambda: 0.0}


def _rotating_resolver(api_handler, *, sentinel="rt-old"):
    """A resolver whose mint always rotates rt-old -> rt-new."""

    def oauth_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "at",
                "expires_at_utc_secs": 9999,
                "refresh_token": "rt-new",
            },
        )

    return oauth.OAuthResolver(
        oauth.OAuthBrokerClient(
            "https://x/oauth/v1",
            "sk",
            http=httpx.Client(transport=httpx.MockTransport(oauth_handler)),
        ),
        api.Client(
            "https://x/api/v1",
            "sk",
            http=httpx.Client(transport=httpx.MockTransport(api_handler)),
        ),
        _vault_with_sentinel(sentinel),
        PASSWORD,
    )


def test_rotation_write_back_retries_onto_the_fresh_document():
    # The core regression: a rotation that loses the revision race used to be
    # discarded while the old token was already dead upstream.
    puts = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            puts.append(json.loads(request.content)["expected_revision"])
            if len(puts) == 1:
                return httpx.Response(409, json={"error": "revision conflict"})
            return httpx.Response(200, json={"revision": 12, "updated_at": "z"})
        # The server moved to revision 11 under us, sentinel still rt-old.
        return httpx.Response(200, json=_vault_blob("rt-old", 11))

    resolver = _rotating_resolver(api_handler)
    outcome = resolver._persist_rotated_sentinel(
        "google", "__google_oauth__", "rt-old", "rt-new", **NO_JITTER
    )
    assert outcome is oauth.RotationPersistOutcome.PERSISTED
    # First PUT used our stale revision, the retry used the server's fresh one.
    assert puts == [4, 11]
    assert resolver._vault.entry_for("google", "__google_oauth__")["api_key"] == (
        "rt-new"
    )


def test_rotation_write_back_stops_when_another_relay_already_saved_it():
    puts = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            puts.append(1)
            return httpx.Response(409, json={"error": "revision conflict"})
        return httpx.Response(200, json=_vault_blob("rt-new", 11))

    resolver = _rotating_resolver(api_handler)
    outcome = resolver._persist_rotated_sentinel(
        "google", "__google_oauth__", "rt-old", "rt-new", **NO_JITTER
    )
    # PERSISTED, not an abandon: the grant the vault denotes IS the one we
    # minted, so the caller must go on and fetch.
    assert outcome is oauth.RotationPersistOutcome.PERSISTED
    assert len(puts) == 1  # no second write: the server already holds ours


def test_rotation_write_back_abandons_a_different_grant():
    # A third value can only mean the user reconnected while we were minting.
    # Clobbering it would destroy a working grant to save a superseded one.
    puts = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            puts.append(1)
            return httpx.Response(409, json={"error": "revision conflict"})
        return httpx.Response(200, json=_vault_blob("rt-reconnected", 11))

    resolver = _rotating_resolver(api_handler)
    outcome = resolver._persist_rotated_sentinel(
        "google", "__google_oauth__", "rt-old", "rt-new", **NO_JITTER
    )
    assert outcome is oauth.RotationPersistOutcome.SUPERSEDED
    assert outcome.aborted
    assert len(puts) == 1


def test_rotation_write_back_abandons_a_vanished_sentinel():
    # The bead's stronger case: the user disconnected the provider WHILE the
    # refresh was running, so the server-fresh document no longer has the
    # sentinel at all. Distinguished from SUPERSEDED because a disconnect and a
    # reconnect mean different things to the user, and the App Server records
    # which one happened.
    puts = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            puts.append(1)
            return httpx.Response(409, json={"error": "revision conflict"})
        # Server-fresh document with the sentinel entry removed entirely.
        return httpx.Response(200, json=_vault_blob(revision=11, entries=[]))

    resolver = _rotating_resolver(api_handler)
    outcome = resolver._persist_rotated_sentinel(
        "google", "__google_oauth__", "rt-old", "rt-new", **NO_JITTER
    )
    assert outcome is oauth.RotationPersistOutcome.SENTINEL_VANISHED
    assert outcome.aborted
    # One attempt, then abandoned: never re-created the entry the user deleted.
    assert puts == [1]


def test_rotation_write_back_abandons_when_the_sentinel_is_already_gone():
    # The other route to SENTINEL_VANISHED: the entry is missing on the very
    # first pass, before any write is attempted.
    puts = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            puts.append(1)
        return httpx.Response(404)

    resolver = _rotating_resolver(api_handler)
    outcome = resolver._persist_rotated_sentinel(
        "cloudflare", "__cloudflare_oauth__", "rt-old", "rt-new", **NO_JITTER
    )
    assert outcome is oauth.RotationPersistOutcome.SENTINEL_VANISHED
    # Never even attempted a write — there was nothing to write onto.
    assert puts == []


def test_access_token_discards_the_minted_token_on_an_abandoned_rotation():
    # The heart of the fix. The mint SUCCEEDED and rotated, but the write-back
    # was abandoned because the vault now holds a different grant. The access
    # token in hand no longer speaks for this card, so access_token must raise
    # instead of returning it — and must not cache it, or a sibling card
    # sharing the sentinel would fetch with it moments later.
    puts = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            puts.append(1)
            return httpx.Response(409, json={"error": "revision conflict"})
        return httpx.Response(200, json=_vault_blob("rt-reconnected", 11))

    resolver = _rotating_resolver(api_handler)
    with pytest.raises(oauth.RotationAborted) as exc:
        resolver.access_token("google", "__google_oauth__", "/google/mint")

    assert exc.value.outcome is oauth.RotationPersistOutcome.SUPERSEDED
    # NOT an OAuthError: that path carries a 409 the App Server classifies as
    # reauth_required, and the replacement grant is healthy. Badging it would
    # tell the user to reconnect a working connection.
    assert not isinstance(exc.value, oauth.OAuthError)
    assert resolver._access_cache == {}


def test_rotation_write_back_gives_up_as_reauth_after_the_budget():
    puts = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            puts.append(1)
            return httpx.Response(409, json={"error": "revision conflict"})
        return httpx.Response(200, json=_vault_blob("rt-old", 11 + len(puts)))

    resolver = _rotating_resolver(api_handler)
    with pytest.raises(oauth.OAuthError, match="could not save it") as exc:
        resolver._persist_rotated_sentinel(
            "google", "__google_oauth__", "rt-old", "rt-new", **NO_JITTER
        )
    assert exc.value.status == 409
    assert len(puts) == oauth.ROTATION_PERSIST_ATTEMPTS


def test_rotation_write_back_does_not_retry_a_non_conflict_failure():
    puts = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            puts.append(1)
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json=_vault_blob("rt-old", 11))

    resolver = _rotating_resolver(api_handler)
    with pytest.raises(oauth.OAuthError, match="could not save it"):
        resolver._persist_rotated_sentinel(
            "google", "__google_oauth__", "rt-old", "rt-new", **NO_JITTER
        )
    assert len(puts) == 1


def test_rotation_write_back_failure_leaves_no_unsaved_edit_behind():
    # A rotation we report as FAILED must not ride along on someone else's
    # later write. The resolver mutates the in-memory document before
    # uploading, and that document outlives this call: without a revert, the
    # next successful write_back in the same run (another provider's rotation,
    # at a revision that still validates) would quietly persist the token we
    # just told the user was lost — a card saying "reconnect" over a vault
    # that actually holds the new token.
    bodies = []
    accept_writes = False

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            bodies.append(json.loads(request.content))
            if accept_writes:
                return httpx.Response(200, json={"revision": 20, "updated_at": "z"})
            # Until then every attempt conflicts, exhausting the budget.
            return httpx.Response(409, json={"error": "revision conflict"})
        return httpx.Response(200, json=_vault_blob("rt-old", 11))

    resolver = _rotating_resolver(api_handler)
    with pytest.raises(oauth.OAuthError):
        resolver._persist_rotated_sentinel(
            "google", "__google_oauth__", "rt-old", "rt-new", **NO_JITTER
        )

    # The document the resolver still holds must be back to the old token.
    entry = resolver._vault.entry_for("google", "__google_oauth__")
    assert entry is not None
    assert entry["api_key"] == "rt-old"

    # And an unrelated write that DOES land must not carry the rotation.
    accept_writes = True
    bodies.clear()
    vault.write_back(resolver._api, resolver._vault, PASSWORD)
    assert len(bodies) == 1
    written, _, _ = vault.decrypt_to_doc(bodies[0]["jwe"], PASSWORD)
    saved = [e for e in written["entries"] if e["provider"] == "google"]
    assert [e["api_key"] for e in saved] == ["rt-old"]


def test_rotation_write_back_restores_on_an_unmodelled_exception():
    # The restore must not depend on us having enumerated the exception. Any
    # failure leaves the document unaccepted, so an error type the handlers
    # do not name must still not leave the rotated token sitting in the
    # long-lived in-memory vault for someone else's write to carry along.
    class Unmodelled(Exception):
        pass

    def api_handler(request: httpx.Request) -> httpx.Response:
        raise Unmodelled("transport exploded")

    resolver = _rotating_resolver(api_handler)
    with pytest.raises(Exception) as exc:
        resolver._persist_rotated_sentinel(
            "google", "__google_oauth__", "rt-old", "rt-new", **NO_JITTER
        )
    # It propagates rather than being reclassified as reauth...
    assert not isinstance(exc.value, oauth.OAuthError)
    # ...and the document is clean regardless.
    entry = resolver._vault.entry_for("google", "__google_oauth__")
    assert entry is not None
    assert entry["api_key"] == "rt-old"


def test_rotation_backoff_is_contention_relief_only():
    # Every other test in this block runs with the delay forced to zero and
    # still passes — the delay is never load-bearing. This one only pins that a
    # real retry does wait, so two relays don't collide in lockstep.
    slept = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(409, json={"error": "revision conflict"})
        return httpx.Response(200, json=_vault_blob("rt-old", 11))

    resolver = _rotating_resolver(api_handler)
    with pytest.raises(oauth.OAuthError):
        resolver._persist_rotated_sentinel(
            "google",
            "__google_oauth__",
            "rt-old",
            "rt-new",
            sleep=slept.append,
            random=lambda: 0.5,
        )
    assert len(slept) == oauth.ROTATION_PERSIST_ATTEMPTS - 1
    assert all(s > 0 for s in slept)


def test_mint_409_is_reauth():
    client = oauth.OAuthBrokerClient(
        "https://x/oauth/v1",
        "sk",
        http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(409))),
    )
    with pytest.raises(oauth.OAuthError, match="reconnect") as exc:
        client.mint("/google/mint", "rt")
    assert exc.value.status == 409  # preserved → server records reauth_required


def _vault_blob(refresh_token="rt-old", revision=4, *, entries=None):
    """A GET /vault response body ({jwe, revision}) with one google sentinel.

    ``entries`` overrides the entry list wholesale — pass ``[]`` to model the
    document the server holds after the user disconnects the provider.
    """
    doc = {
        "schema_version": 1,
        "entries": (
            [
                {
                    "id": "g",
                    "provider": "google",
                    "api_key": refresh_token,
                    "metadata": {"instance_key": "__google_oauth__"},
                }
            ]
            if entries is None
            else entries
        ),
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

from __future__ import annotations

import json
import os

import httpx
import pytest

import costcompass
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
        seen["ua"] = request.headers.get("User-Agent")
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
    assert seen["ua"] == costcompass.user_agent()
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


def _rotating_resolver(api_handler, *, sentinel="rt-old", vault=None):
    """A resolver whose mint always rotates rt-old -> rt-new.

    Pass ``vault`` to keep a handle on the caller's object — the run's ONE
    vault, which the orchestrator reads too.
    """

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
        vault if vault is not None else _vault_with_sentinel(sentinel),
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

    v = _vault_with_sentinel("rt-old")
    resolver = _rotating_resolver(api_handler, vault=v)
    outcome = resolver._persist_rotated_sentinel(
        "google", "__google_oauth__", "rt-old", "rt-new", **NO_JITTER
    )
    assert outcome is oauth.RotationPersistOutcome.PERSISTED
    # First PUT used our stale revision, the retry used the server's fresh one.
    assert puts == [4, 11]
    assert resolver._vault.entry_for("google", "__google_oauth__")["api_key"] == (
        "rt-new"
    )
    # And the CALLER sees it, because there is one vault per run. The reload
    # this retry performed must not have detached the orchestrator's view: it
    # resolves later cards' direct ``vault_key`` lookups off this object.
    assert v is resolver._vault
    assert v.entry_for("google", "__google_oauth__")["api_key"] == "rt-new"
    assert v.revision == 12


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


@pytest.mark.parametrize(
    "reloaded, outcome",
    [
        (None, oauth.RotationPersistOutcome.SENTINEL_VANISHED),
        ("rt-reconnected", oauth.RotationPersistOutcome.SUPERSEDED),
    ],
)
def test_an_aborted_rotation_decides_every_sibling_card(reloaded, outcome):
    # Every card of a provider shares one sentinel, so they share one grant:
    # whatever the first card learns about it is true for the second.
    #
    # Before the run-scoped memo the sibling resolved on its own and reached a
    # DIFFERENT answer in both cases. Disconnected: the sentinel is gone, so
    # the lookup raised a 401 and the card was badged reauth_required — for a
    # provider the user had just removed. Reconnected: it minted against the
    # replacement grant and fetched, which is how usage from a freshly
    # connected account could land on a card that denotes the old one.
    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(409, json={"error": "revision conflict"})
        if reloaded is None:
            # The document the server holds after a disconnect.
            return httpx.Response(200, json=_vault_blob(revision=11, entries=[]))
        return httpx.Response(200, json=_vault_blob(reloaded, 11))

    resolver = _rotating_resolver(api_handler)
    mints = _count_mints(resolver)

    with pytest.raises(oauth.RotationAborted) as first:
        resolver.access_token("google", "__google_oauth__", "/google/mint")
    with pytest.raises(oauth.RotationAborted) as sibling:
        resolver.access_token("google", "__google_oauth__", "/google/mint")

    assert first.value.outcome is outcome
    # Same outcome and same sentence, so the sibling's card records the same
    # marker and the same last_error as the card that noticed.
    assert sibling.value.outcome is outcome
    assert str(sibling.value) == str(first.value)
    # The sibling's own mint would have SUCCEEDED on the reconnect path. It
    # must never be reached.
    assert len(mints) == 1


def test_an_aborted_rotation_does_not_decide_another_providers_cards():
    # The memo keys on provider + sentinel. Sentinel keys are
    # provider-namespaced by construction, so this can only break if the key
    # ever drops the provider id.
    cloudflare_entry = {
        "id": "c",
        "provider": "cloudflare",
        "api_key": "cf-old",
        "metadata": {"instance_key": "__cloudflare_oauth__"},
    }
    google_entry = {
        "id": "g",
        "provider": "google",
        "api_key": "rt-reconnected",
        "metadata": {"instance_key": "__google_oauth__"},
    }

    puts = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            puts.append(1)
            # Only google's write-back loses the race; cloudflare's lands.
            if len(puts) == 1:
                return httpx.Response(409, json={"error": "revision conflict"})
            return httpx.Response(200, json={"revision": 12, "updated_at": "z"})
        # The reload carries BOTH sentinels — google's replaced (so its
        # write-back abandons), cloudflare's untouched. A reload that dropped
        # the other provider's entry would fail this test for the wrong reason:
        # it replaces the resolver's whole document.
        return httpx.Response(
            200, json=_vault_blob(revision=11, entries=[google_entry, cloudflare_entry])
        )

    resolver = _rotating_resolver(api_handler)
    # A second sentinel, for a different provider, in the same document.
    resolver._vault.doc["entries"].append(cloudflare_entry)
    mints = _count_mints(resolver)

    with pytest.raises(oauth.RotationAborted):
        resolver.access_token("google", "__google_oauth__", "/google/mint")
    # cloudflare's card is resolved on its own terms — its own mint, its own
    # write-back — rather than inheriting google's verdict.
    assert (
        resolver.access_token("cloudflare", "__cloudflare_oauth__", "/cf/mint") == "at"
    )
    assert mints == ["/google/mint", "/cf/mint"]


def _count_mints(resolver):
    """Record each mint the resolver actually performs.

    The point of the memo is the mints that DON'T happen, so the assertions
    need to see the calls rather than their results.
    """
    calls = []
    real_mint = resolver._oauth.mint

    def counting_mint(path, refresh_token):
        calls.append(path)
        return real_mint(path, refresh_token)

    resolver._oauth.mint = counting_mint
    return calls


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
    # later write. The document outlives this call and the orchestrator reads
    # it too, so a rotation applied before the server accepted it would be both
    # readable as a credential and quietly persisted by the next successful
    # write_back in the same run (another provider's rotation, at a revision
    # that still validates) — a card saying "reconnect" over a vault that
    # actually holds the new token.
    #
    # Now unreachable by construction: the rotation is applied to a candidate
    # copy and adopted only once PUT /vault returns. This still pins the
    # OUTCOME, which is what a future refactor could take away.
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


def test_rotation_write_back_leaves_the_document_clean_on_an_unmodelled_exception():
    # Cleanliness must not depend on us having enumerated the exception. This
    # used to be an undo in a ``finally`` — correct, but only as complete as the
    # exits it covered. Writing to a candidate makes an unnamed error type
    # indistinguishable from a named one: the run's vault was never touched.
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


# ---- one vault owner per run -------------------------------------------
#
# LOCKSTEP INVARIANT (browser invariant 11): the resolver and the orchestrator
# read ONE vault document, holding committed state only. `run` builds a single
# `Vault` and hands the same object to both, so the tests below assert on the
# CALLER's object — the one `_resolve_credential` reads for every card's direct
# `vault_key` lookup — not on `resolver._vault`.
#
# The failure it guards is quiet: `_reload_vault` used to rebind `self._vault`
# to the object `fetch_and_decrypt` returns, which detached the orchestrator
# from the resolver mid-run. Nothing raised; later cards simply resolved
# against the run-start document forever.


def test_a_mid_run_reload_reaches_the_orchestrators_view_of_the_vault():
    # The reloaded document carries a pasted key that did not exist at run
    # start — the user re-pasted it in the browser while this run was in
    # flight. It is the only place that value exists, so reading it is proof of
    # which document the caller is holding.
    server_entries = [
        {
            "id": "g",
            "provider": "google",
            "api_key": "rt-old",
            "metadata": {"instance_key": "__google_oauth__"},
        },
        {
            "id": "o",
            "provider": "openai",
            "api_key": "sk-reloaded",
            "metadata": {"instance_key": ""},
        },
    ]
    puts = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            puts.append(1)
            if len(puts) == 1:
                return httpx.Response(409, json={"error": "revision conflict"})
            return httpx.Response(200, json={"revision": 12, "updated_at": "z"})
        return httpx.Response(
            200, json=_vault_blob("rt-old", 11, entries=server_entries)
        )

    v = _vault_with_sentinel("rt-old")
    assert v.entry_for("openai", "") is None  # absent at run start

    resolver = _rotating_resolver(api_handler, vault=v)
    outcome = resolver._persist_rotated_sentinel(
        "google", "__google_oauth__", "rt-old", "rt-new", **NO_JITTER
    )
    assert outcome is oauth.RotationPersistOutcome.PERSISTED

    # One vault: the reload landed on the caller's object, so a card resolved
    # after this point sees the key the user just pasted rather than a miss.
    assert v is resolver._vault
    assert v.entry_for("openai", "")["api_key"] == "sk-reloaded"


def test_a_rotation_the_server_refused_is_never_readable_from_the_vault():
    # The other half of the invariant. A sentinel is addressed by
    # (provider, sentinel_key) and a card by (provider, instance_key), so a
    # card whose instance_key EQUALS the sentinel key resolves to the sentinel
    # row itself. That collision does not occur in production — sentinel keys
    # are `__provider_oauth__`-shaped and server-authored — but relying on it
    # is exactly what "committed state only" removes, and an assumption no test
    # states is one a refactor can silently break.
    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            # Non-transient: no retry, no reload, the rotation is simply lost.
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json=_vault_blob("rt-old", 11))

    v = _vault_with_sentinel("rt-old")
    resolver = _rotating_resolver(api_handler, vault=v)
    with pytest.raises(oauth.OAuthError):
        resolver._persist_rotated_sentinel(
            "google", "__google_oauth__", "rt-old", "rt-new", **NO_JITTER
        )

    # A direct lookup reads what the vault actually holds, never the rotation
    # we just reported as unsaveable.
    assert v.entry_for("google", "__google_oauth__")["api_key"] == "rt-old"

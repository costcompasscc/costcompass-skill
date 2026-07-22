# LOCKSTEP: one of three relay implementations (browser is the
# reference) — semantic changes here must land in the CostCompass
# monorepo, checked out alongside this repo, at
# ../costcompass/frontend/src/lib/refresh/oauth-mint.ts,
# ../costcompass/frontend/src/lib/refresh/oauth-broker-fetch.ts, and
# ../costcompass/client/macos/CostCompassKit/Sources/CostCompassKit/
#   Refresh/OAuth.swift.
# See "Three relay implementations" in that repo's root CLAUDE.md;
# `make lockstep` there enumerates the whole set across both repos.

"""OAuth access-token minting for the CLI refresh path.

OAuth cards keep a per-user refresh-token in a vault sentinel entry. Per
fetch the CLI exchanges it at the oauth-broker for a short-lived access
token, mirroring the browser's `_shared/oauthMint.ts`. A rotated
refresh-token is written back to the vault sentinel (`PUT /vault`) so the
next refresh still works.

There is **no provider table here**: the vault sentinel key and the
oauth-broker mint path are server-authored — they arrive on each
fetch-run entry's `credential` routing (`{kind: "oauth_mint",
sentinel_key, mint_path}`). The CLI just executes the routing it's given,
so adding an OAuth provider never touches this file.
"""

from __future__ import annotations

import random as _random
import time
from collections.abc import Callable
from typing import Any

import httpx

from .. import api
from .. import vault as vault_mod

# Total write-back attempts (the first try plus retries) before a rotated
# token is declared unsaveable. Small on purpose: each attempt re-reads the
# server's current blob, so a value that keeps losing the race is contended
# enough that a fourth attempt is unlikely to change the outcome.
ROTATION_PERSIST_ATTEMPTS = 3


def _conflict_backoff_secs(attempt: int, random: Callable[[], float]) -> float:
    """Backoff before re-reading the vault after a lost revision race.

    This is CONTENTION RELIEF ONLY — it is never what makes the write correct.
    Correctness comes entirely from the server's compare-and-set on the
    revision: a retry re-reads the current document and re-uploads at the
    revision the server just reported, so a write either lands against the
    revision it was built on or fails again. The delay only stops two relays
    retrying in lockstep from colliding repeatedly. The implementation must be
    correct with this forced to zero, and a test pins that.
    """
    return (0.05 + attempt * 0.1) * (0.5 + random())


def _restore_api_key(entry: dict[str, Any], previous: str | None) -> None:
    """Undo a speculative ``api_key`` edit on a vault entry.

    ``previous`` is whatever ``entry.get("api_key")`` returned before the edit,
    so a missing key restores to missing rather than to an empty string.
    """
    if previous is None:
        entry.pop("api_key", None)
    else:
        entry["api_key"] = previous


def _rotation_persist_failed(provider: str, cause: Exception) -> OAuthError:
    """The error raised when a rotated refresh-token could not be saved.

    Status 409 so the orchestrator tags the body with ``reauth_required`` and
    the App Server's shared classifier marks the card accordingly. That state
    is the honest one: the upstream invalidated the old token the moment it
    issued the rotated one, so a token we could not save is a dead grant and
    the user must reconnect. Reporting it as a transient 502 (the old
    behaviour) hid a permanently broken connection behind a generic error
    until some later refresh happened to surface it.
    """
    return OAuthError(
        f"rotated the refresh-token for '{provider}' but could not save it "
        f"({cause}). The previous token is no longer valid upstream, so the "
        f"connection must be reconnected.",
        status=409,
    )


class OAuthError(Exception):
    """An OAuth mint or token rotation failed.

    ``status`` is the HTTP status the App Server's response taxonomy should
    classify the failure under (401/403 → reauth, 429 → rate-limited, 5xx →
    transient), mirroring the browser plugin's ``fetch_failure`` status
    preservation. The orchestrator relays it as a non-synthetic provider-error
    so a 429 isn't mislabelled as a 401 reauth (and vice-versa). Defaults to
    401 — the "you need to (re)connect" case.
    """

    def __init__(self, message: str, *, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


def oauth_url_from_api(api_url: str) -> str:
    """`https://host/api/v1` -> `https://host/oauth/v1`."""
    base = api_url.rstrip("/")
    if base.endswith("/api/v1"):
        base = base[: -len("/api/v1")]
    return f"{base}/oauth/v1"


class OAuthBrokerClient:
    def __init__(
        self, base_url: str, api_key: str, *, http: httpx.Client | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=60.0)
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def mint(self, path: str, refresh_token: str) -> dict[str, Any]:
        try:
            resp = self._http.post(
                f"{self.base_url}{path}",
                headers=self._headers,
                json={"refresh_token": refresh_token},
            )
        except httpx.RequestError as exc:
            # Couldn't reach the oauth-broker — a transient infrastructure
            # failure, not a credential problem. Classify as 502 so it isn't
            # mislabelled a reauth.
            raise OAuthError(
                f"could not reach oauth-broker: {exc}", status=502
            ) from exc
        if resp.status_code in (401, 409):
            # 401 = caller auth; 409 = upstream credential rejected (e.g. Google
            # invalid_grant after the 7-day Testing-mode refresh expiry). Both
            # mean the user must reconnect — preserve the real status so the
            # server classifies it (409 → reauth_required).
            raise OAuthError(
                "OAuth credential rejected — reconnect this provider in the app.",
                status=resp.status_code,
            )
        if resp.status_code >= 400:
            # Never echo the broker's error body — it may carry the upstream
            # token-exchange response. Preserve only the status for taxonomy.
            raise OAuthError(
                f"oauth mint failed ({resp.status_code})", status=resp.status_code
            )
        try:
            return resp.json()
        except ValueError as exc:
            # A 2xx with a non-JSON body (proxy page, redirect) is broker-side
            # breakage, not a credential problem — 502, and never echo the body.
            raise OAuthError(
                "oauth-broker returned a non-JSON response", status=502
            ) from exc


class OAuthResolver:
    """Mints + caches access tokens per provider for one refresh run, and
    persists any rotated refresh-token back to the vault sentinel."""

    def __init__(
        self,
        oauth_client: OAuthBrokerClient,
        api_client: api.Client,
        vault: vault_mod.Vault,
        password: str,
    ) -> None:
        self._oauth = oauth_client
        self._api = api_client
        self._vault = vault
        self._password = password
        self._access_cache: dict[str, str] = {}

    def access_token(self, provider: str, sentinel_key: str, mint_path: str) -> str:
        """Mint (and cache per sentinel) a short-lived access token.

        ``sentinel_key`` and ``mint_path`` are server-authored (the entry's
        credential routing) — this method holds no provider knowledge. The
        cache keys on ``sentinel_key`` so the N cards sharing one sentinel
        (e.g. every google project) trigger a single mint + rotation.
        """
        if sentinel_key in self._access_cache:
            return self._access_cache[sentinel_key]
        sentinel = self._vault.entry_for(provider, sentinel_key)
        if not sentinel or not sentinel.get("api_key"):
            # Not connected — the user must (re)authorize. 401 → reauth.
            raise OAuthError(
                f"No OAuth refresh-token found for '{provider}'. Reconnect it in the app.",
                status=401,
            )
        refresh_token = sentinel["api_key"]
        try:
            result = self._oauth.mint(mint_path, refresh_token)
        except OAuthError as exc:
            # A refresh-token that rotates on every mint (cloudflare, github
            # user) can be invalidated out from under us by another relay
            # (browser/CLI/macOS) minting against the same shared sentinel.
            # On an upstream credential rejection (409), re-read the sentinel
            # from the server — a concurrent relay may have already persisted
            # the fresher rotated token — and retry the mint once before
            # surfacing reauth. Any other status (caller-auth 401, 429, 5xx)
            # can't be helped by a fresher token, so it propagates.
            fresh = (
                self._reload_sentinel_token(provider, sentinel_key)
                if exc.status == 409
                else None
            )
            if fresh is None or fresh == refresh_token:
                raise
            refresh_token = fresh
            result = self._oauth.mint(mint_path, refresh_token)

        access_token = result.get("access_token")
        if not access_token:
            raise OAuthError(
                f"oauth-broker returned no access_token for '{provider}'", status=502
            )

        rotated = result.get("refresh_token")
        if rotated and rotated != refresh_token:
            self._persist_rotated_sentinel(
                provider, sentinel_key, refresh_token, rotated
            )

        self._access_cache[sentinel_key] = access_token
        return access_token

    def _persist_rotated_sentinel(
        self,
        provider: str,
        sentinel_key: str,
        old_token: str,
        rotated: str,
        *,
        attempts: int = ROTATION_PERSIST_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
        random: Callable[[], float] = _random.random,
    ) -> None:
        """Write a rotated refresh-token back to the vault sentinel.

        The upstream invalidates ``old_token`` the moment it issues
        ``rotated``, so a rotation we fail to save is a dead grant — this must
        surface, never be swallowed.

        The vault write is an optimistic compare-and-set on a revision every
        relay caches independently, so ANY other write — the browser renaming
        a card, this same run persisting a different provider's rotation — can
        bump it underneath us. On a lost race we re-read the server's current
        document and re-apply this one field on top of it. We never re-upload
        our locally-held copy: that would revert whatever the other writer
        just committed.
        """
        for attempt in range(attempts):
            # Re-resolve the sentinel every pass: a reauth-retry or a reload
            # below may have swapped ``self._vault`` for a server-fresh doc,
            # so an entry bound earlier could point into the stale document.
            entry = self._vault.entry_for(provider, sentinel_key)
            if entry is None:
                return
            previous = entry.get("api_key")
            entry["api_key"] = rotated
            persisted = False
            # The restore lives in ``finally``, not in the except clauses: the
            # edit is speculative until the server accepts it, and the document
            # outlives this function. A later write_back in the same run
            # (another provider's rotation, at a revision that still validates)
            # would otherwise carry our unsaved field along and persist a
            # rotation we reported as failed. Restoring on EVERY non-persisted
            # exit — including an exception we do not model — is what keeps that
            # impossible; a per-except restore silently misses the ones it does
            # not name. The next attempt re-applies onto whatever document is
            # current by then.
            try:
                vault_mod.write_back(self._api, self._vault, self._password)
                persisted = True
                return
            except api.VaultRevisionConflict as exc:
                if attempt >= attempts - 1:
                    raise _rotation_persist_failed(provider, exc) from exc
                sleep(_conflict_backoff_secs(attempt, random))
                if not self._reload_vault():
                    # Can't see the server's current document, so there is
                    # nothing to re-apply onto — retrying would just replay
                    # the same stale revision.
                    raise _rotation_persist_failed(provider, exc) from exc
                observed = self._sentinel_token(provider, sentinel_key)
                if observed == rotated:
                    return  # another relay already saved ours
                if observed != old_token:
                    # The sentinel holds neither the token we minted from nor
                    # the one we minted. Whatever wrote it did so with a view
                    # of the grant at least as fresh as ours, so ours is not
                    # the value to keep and overwriting would destroy a
                    # working connection to save a superseded one. Abandon.
                    #
                    # The usual cause is a fresh reconnect (a different grant
                    # entirely), but it is not the only one — a provider that
                    # briefly honours the parent token during a grace window
                    # lets another relay mint a SIBLING rotation that also
                    # lands here. Abandoning is right in both cases, which is
                    # why the condition is "not one of our two known values"
                    # rather than any attempt to identify the writer.
                    #
                    # ASSUMPTION: a value we do not recognise is never STALER
                    # than ours. This holds while rotation is single-use
                    # (cloudflare and github user-App, the providers that
                    # rotate on every mint). A provider that returned a
                    # non-rotating token under a changed value would make us
                    # abandon a save we could have made — costing one refresh
                    # cycle, not the grant.
                    #
                    # ``None`` lands here too — the sentinel is gone from the
                    # server's document, i.e. the user disconnected the
                    # provider. There is nothing left to save.
                    return
            except (api.ApiError, vault_mod.VaultError) as exc:
                raise _rotation_persist_failed(provider, exc) from exc
            finally:
                if not persisted:
                    _restore_api_key(entry, previous)

    def _reload_vault(self) -> bool:
        """Adopt the server's current vault document, returning success.

        Adopting the fresh document (and its revision) is what lets a
        subsequent write-back target the revision the server actually holds
        instead of conflicting on a stale one.
        """
        try:
            self._vault = vault_mod.fetch_and_decrypt(self._api, self._password)
        except (api.ApiError, vault_mod.VaultError):
            return False
        return True

    def _sentinel_token(self, provider: str, sentinel_key: str) -> str | None:
        """The sentinel's current refresh-token, or ``None`` when absent."""
        entry = self._vault.entry_for(provider, sentinel_key)
        if not entry or not entry.get("api_key"):
            return None
        return entry["api_key"]

    def _reload_sentinel_token(self, provider: str, sentinel_key: str) -> str | None:
        """Re-fetch the vault from the server and return the sentinel's current
        refresh-token, or ``None`` if it can't be read.

        Used by the reauth-retry path: a rotating refresh-token may have been
        invalidated by a concurrent relay that already persisted the fresher
        value.
        """
        if not self._reload_vault():
            return None
        return self._sentinel_token(provider, sentinel_key)

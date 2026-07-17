# LOCKSTEP: one of three relay implementations (browser is the
# reference) — semantic changes here must land in the CostCompass
# monorepo, checked out alongside this repo, at
# ../costcompass/frontend/src/lib/refresh/oauth-mint.ts,
# ../costcompass/frontend/src/lib/refresh/oauth-broker-fetch.ts, and
# ../costcompass/cli/macos/CostCompassKit/Sources/CostCompassKit/
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

from typing import Any

import httpx

from .. import api
from .. import vault as vault_mod


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
        return resp.json()


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
            # Persist the rotated refresh-token before the old one is
            # invalidated upstream, else the next refresh fails. A persist
            # failure must surface (not be swallowed) but only as a per-card
            # error — never abort the whole run. Re-resolve the sentinel from
            # the current vault: a reauth-retry above may have swapped
            # ``self._vault`` for a server-fresh doc, so the ``sentinel`` bound
            # before the mint could point into the stale doc.
            current = self._vault.entry_for(provider, sentinel_key) or sentinel
            current["api_key"] = rotated
            try:
                vault_mod.write_back(self._api, self._vault, self._password)
            except (api.ApiError, vault_mod.VaultError) as exc:
                # Persistence failure (e.g. a vault revision conflict) is an
                # infrastructure problem, not a credential rejection — 502
                # transient rather than reauth.
                raise OAuthError(
                    f"minted a token for '{provider}' but could not persist the "
                    f"rotated refresh-token (next refresh may need a reconnect): {exc}",
                    status=502,
                ) from exc

        self._access_cache[sentinel_key] = access_token
        return access_token

    def _reload_sentinel_token(self, provider: str, sentinel_key: str) -> str | None:
        """Re-fetch the vault from the server and return the sentinel's current
        refresh-token, or ``None`` if it can't be read.

        Used by the reauth-retry path: a rotating refresh-token may have been
        invalidated by a concurrent relay that already persisted the fresher
        value. Adopting the server-fresh document (and its revision) also keeps
        a subsequent rotation write-back from conflicting on a stale revision.
        """
        try:
            fresh = vault_mod.fetch_and_decrypt(self._api, self._password)
        except (api.ApiError, vault_mod.VaultError):
            return None
        self._vault = fresh
        entry = fresh.entry_for(provider, sentinel_key)
        if not entry or not entry.get("api_key"):
            return None
        return entry["api_key"]

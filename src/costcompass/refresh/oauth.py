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

import copy
import random as _random
import threading
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any

import httpx

from .. import api, user_agent
from .. import vault as vault_mod

# Total write-back attempts (the first try plus retries) before a rotated
# token is declared unsaveable. Small on purpose: each attempt re-reads the
# server's current blob, so a value that keeps losing the race is contended
# enough that a fourth attempt is unlikely to change the outcome.
ROTATION_PERSIST_ATTEMPTS = 3


class RotationPersistOutcome(StrEnum):
    """What ``_persist_rotated_sentinel`` did — why it can't just return None.

    ``SENTINEL_VANISHED`` and ``SUPERSEDED`` are ABANDONED writes, and abandoning
    is correct (see the branch that returns them). What was wrong was reporting
    them as success: the caller then fetched provider data with an access token
    minted from a grant the vault no longer denotes — ingesting usage for a card
    the user just disconnected, or attributing another account's usage to this one.
    """

    #: The write landed, OR the server already held exactly the value we minted
    #: (another relay saved ours).
    PERSISTED = "persisted"
    #: The sentinel is gone from the vault — the user disconnected mid-run.
    SENTINEL_VANISHED = "sentinel_vanished"
    #: The sentinel holds a grant we don't recognise — a reconnect mid-run, or a
    #: sibling relay's rotation through a provider grace window.
    SUPERSEDED = "superseded"

    @property
    def aborted(self) -> bool:
        """Whether the grant we minted from is no longer the one the vault
        denotes, so the caller must discard its access token rather than fetch.

        Defined as "has a row in ``ROTATION_ABORT_VERB``" rather than "is not
        PERSISTED", so classification and phrasing are one fact: a new abort
        outcome cannot be classified as an abort until its verb exists, and the
        lookup that phrases it raises ``KeyError`` rather than silently
        mislabelling it.
        """
        return self in ROTATION_ABORT_VERB


# The verb describing each abort to the user — and the DEFINITION of which
# outcomes abort (see ``RotationPersistOutcome.aborted``). This replaced a binary
# conditional whose else-branch would silently swallow a third abort outcome and
# mislabel it as a supersede; the browser and macOS ports carry the same table,
# where the type system makes the omission a compile error.
ROTATION_ABORT_VERB: dict[RotationPersistOutcome, str] = {
    RotationPersistOutcome.SENTINEL_VANISHED: "removed",
    RotationPersistOutcome.SUPERSEDED: "renewed",
}


class RotationAborted(Exception):
    """A mint's rotation write-back was abandoned: the vault's grant changed
    underneath the run.

    Deliberately NOT an ``OAuthError``: the orchestrator turns an ``OAuthError``
    into a taxonomy-bearing provider error (409 there marks the card
    ``reauth_required``), and the whole point of this outcome is that the
    connection is HEALTHY — a replacement grant is sitting in the vault. The
    entry skips badge-free and the next refresh picks the new grant up.
    """

    def __init__(self, message: str, *, outcome: RotationPersistOutcome) -> None:
        super().__init__(message)
        self.outcome = outcome


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
            "User-Agent": user_agent(),
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
        # LOCKSTEP: one vault owner per run, holding committed state only. This
        # is the caller's object, not a copy — ``run`` passes the same ``Vault``
        # to the orchestrator's per-entry loop, whose direct ``vault_key``
        # lookups read it. ``_adopt`` keeps it that way by mutating in place, so
        # a mid-run reload is visible to every card resolved after it; the
        # rotation write-back never applies an edit the server has not accepted,
        # so no card can read a value the vault does not hold. The browser
        # reaches the same end through a ``VaultEntryReader`` handle refreshed
        # behind it, macOS through an actor-isolated accessor on the resolver.
        # See invariant 11 in frontend/src/lib/refresh/CLAUDE.md.
        self._vault = vault
        self._password = password
        self._access_cache: dict[str, str] = {}
        # Sentinels whose OAuth grant changed underneath the run in flight,
        # keyed by ``_abort_key``. Every card of a provider shares one
        # sentinel, so once any entry detects the change the rest are decided
        # too — they skip without reading the sentinel and without minting.
        #
        # Per-run lifetime comes free: this resolver is built inside ``run``
        # and discarded when it returns.
        #
        # All three relays fan entries out, so a sibling CAN be mid-resolution
        # when this is written and a plain memo is not enough on its own. Each
        # relay reaches the same invariant — one grant, one verdict per run —
        # the way its concurrency model allows: the browser shares the in-flight
        # resolution PROMISE per grant (``OAuthResolutions`` in
        # ``credential.ts``), macOS memoizes the in-flight ``Task``, and this
        # port holds ``_sentinel_lock`` across the whole of ``access_token`` so
        # a sibling blocks until the first resolution settles and then reads
        # this memo. Don't port the promise shape here to "match".
        #
        # Stores the raised error, not a flag, so a sibling raises exactly what
        # the entry that noticed raised and the orchestrator's existing
        # ``RotationAborted`` mapping produces the identical marker and message.
        self._aborted_sentinels: dict[str, RotationAborted] = {}
        # One lock per grant, so ``access_token`` resolves each sentinel exactly
        # once per run even under fan-out. Without it two cards sharing a
        # sentinel both miss ``_access_cache``, both mint, and race the rotation
        # write-back — which on a rotating-token provider (cloudflare, github
        # user) means each invalidates the other's token.
        #
        # Guarded by ``_sentinel_locks_guard`` because the dict itself is grown
        # concurrently; the per-key locks it hands out are held far longer (a
        # whole mint round-trip) than this one ever is.
        self._sentinel_locks: dict[str, threading.Lock] = {}
        self._sentinel_locks_guard = threading.Lock()
        # Serializes every read, adopt and write-back of ``self._vault``. The
        # document is shared by all sentinels AND by the orchestrator, and a
        # persist is a read-modify-write of it spanning a network round-trip;
        # without this, two concurrent rotations each build a candidate from the
        # same document and the second to land silently drops the first.
        # Re-entrant because the persist loop calls ``_reload_vault`` /
        # ``_sentinel_token`` / ``_adopt``, which take it too.
        #
        # LOCK ORDER is always sentinel lock → vault lock, never the reverse.
        # Nothing takes a sentinel lock while holding this one.
        self._vault_lock = threading.RLock()
        # Collapses redundant re-syncs, the counterpart of the browser's shared
        # in-flight promise in ``resyncFromServer``. ``_vault_lock`` already
        # serializes reloads, so the waste it leaves is a caller re-fetching a
        # document another thread installed while it waited — a whole GET
        # /vault plus a whole PBKDF2 derivation, at the one moment the run is
        # already slow. A counter rather than a shared promise because there is
        # no promise to hand out here: the second caller is blocked on a lock,
        # not awaiting a future, so it can only ask afterwards whether its own
        # fetch is still needed.
        #
        # ``_reload_seq`` counts reloads BEGUN, and bumping at the start is what
        # makes the freshness claim in ``_reload_vault`` true rather than merely
        # plausible — see the sampling comment there.
        self._reload_seq = 0
        # The ``_reload_seq`` of the last reload that SUCCEEDED. Separate so a
        # caller never rides a fetch that errored: a failed reload leaves this
        # behind and the next caller does its own.
        self._reload_done_seq = 0

    def _sentinel_lock(self, provider: str, sentinel_key: str) -> threading.Lock:
        """The resolution lock for one grant, created on first use."""
        key = self._abort_key(provider, sentinel_key)
        with self._sentinel_locks_guard:
            lock = self._sentinel_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._sentinel_locks[key] = lock
            return lock

    @staticmethod
    def _abort_key(provider: str, sentinel_key: str) -> str:
        """Key for ``_aborted_sentinels``.

        Includes the provider id, unlike ``_access_cache`` above, which keys on
        the sentinel alone. That gets away with it because sentinel keys are
        provider-namespaced by construction (``__cloudflare_oauth__``); a
        concat costs nothing and leaves no invariant to rely on — a shared
        sentinel string would otherwise let one provider's abort skip another's
        cards.
        """
        return f"{provider}\0{sentinel_key}"

    def access_token(self, provider: str, sentinel_key: str, mint_path: str) -> str:
        """Mint (and cache per sentinel) a short-lived access token.

        ``sentinel_key`` and ``mint_path`` are server-authored (the entry's
        credential routing) — this method holds no provider knowledge. The
        cache keys on ``sentinel_key`` so the N cards sharing one sentinel
        (e.g. every google project) trigger a single mint + rotation.

        Held under the grant's lock end to end, so under entry fan-out a
        sibling card on the same sentinel waits for the first resolution to
        settle and then reads its verdict — one mint, one rotation, one abort
        — rather than racing its own.
        """
        with self._sentinel_lock(provider, sentinel_key):
            return self._resolve_access_token(provider, sentinel_key, mint_path)

    def _resolve_access_token(
        self, provider: str, sentinel_key: str, mint_path: str
    ) -> str:
        """``access_token`` minus the locking. Callers must hold the grant's
        lock; every cache read and write below assumes that exclusion."""
        if sentinel_key in self._access_cache:
            return self._access_cache[sentinel_key]

        # An earlier entry in this run already found this sentinel's grant
        # replaced or gone. Checked BEFORE the sentinel lookup below, not
        # merely as an optimisation: on a mid-run disconnect the sentinel is
        # already gone, so that lookup would raise a 401 and badge the card
        # ``reauth_required`` — for a provider the user themselves just
        # removed, and the opposite of the badge-free skip the entry that
        # noticed got.
        aborted = self._aborted_sentinels.get(self._abort_key(provider, sentinel_key))
        if aborted is not None:
            raise aborted

        with self._vault_lock:
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
            outcome = self._persist_rotated_sentinel(
                provider, sentinel_key, refresh_token, rotated
            )
            if outcome.aborted:
                # The vault's grant changed underneath this run, so the access
                # token we just minted no longer speaks for the connection the
                # card denotes. Raising here — BEFORE the cache write below —
                # is what discards it, so neither this entry nor any sibling
                # card sharing the sentinel can fetch with it.
                error = RotationAborted(
                    f"the '{provider}' connection was {ROTATION_ABORT_VERB[outcome]}"
                    f" while this refresh was running",
                    outcome=outcome,
                )
                # Record it for the rest of the run: this entry is the only one
                # that gets to learn the grant changed. Siblings would each
                # reach their own, DIFFERENT conclusion otherwise — an auth
                # failure on a disconnect, a successful fetch under the
                # replacement grant on a reconnect.
                self._aborted_sentinels[self._abort_key(provider, sentinel_key)] = error
                raise error

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
    ) -> RotationPersistOutcome:
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

        Returns which ``RotationPersistOutcome`` happened rather than None,
        because two of them are abandoned writes the CALLER has to act on. It
        deliberately does not raise for those — the abandon itself is correct,
        and only the caller knows whether a stale grant matters (it does for a
        fetch; it doesn't for an off-refresh probe).

        Serialized across the whole run by ``_vault_lock``: a persist is a
        read-modify-write of the run's one vault document spanning a network
        round-trip, so a concurrent rotation of a DIFFERENT provider —
        reachable now that entries fan out — would otherwise build its
        candidate from a document this one is about to replace, and upload at a
        revision this one is about to bump. One of the two rotations would be
        silently dropped.
        """
        with self._vault_lock:
            return self._persist_rotated_sentinel_locked(
                provider,
                sentinel_key,
                old_token,
                rotated,
                attempts=attempts,
                sleep=sleep,
                random=random,
            )

    def _persist_rotated_sentinel_locked(
        self,
        provider: str,
        sentinel_key: str,
        old_token: str,
        rotated: str,
        *,
        attempts: int,
        sleep: Callable[[float], None],
        random: Callable[[], float],
    ) -> RotationPersistOutcome:
        """``_persist_rotated_sentinel`` minus the locking; callers hold
        ``_vault_lock``."""
        for attempt in range(attempts):
            # Apply the rotation to a CANDIDATE and adopt it only once the
            # server has accepted it. ``self._vault`` is the run's ONE vault —
            # the orchestrator's direct ``vault_key`` lookups read the same
            # object, and it outlives this function — so an edit applied ahead
            # of the write would be readable as a credential, and a later
            # write_back in the same run (another provider's rotation, at a
            # revision that still validates) would carry the unsaved field along
            # and persist a rotation we reported as failed. A copy leaves no
            # window to undo rather than an undo that must cover every exit.
            #
            # Rebuilt every pass rather than hoisted: a reauth-retry or a reload
            # below may have replaced the document with a server-fresh one, and
            # the candidate must be taken from whichever is current now.
            candidate = vault_mod.Vault(
                doc=copy.deepcopy(self._vault.doc),
                p2s=self._vault.p2s,
                p2c=self._vault.p2c,
                revision=self._vault.revision,
            )
            entry = candidate.entry_for(provider, sentinel_key)
            if entry is None:
                # Gone before we could write. The mint read this sentinel
                # moments ago, so it disappearing means the user disconnected
                # the provider mid-run.
                return RotationPersistOutcome.SENTINEL_VANISHED
            entry["api_key"] = rotated
            try:
                vault_mod.write_back(self._api, candidate, self._password)
                self._adopt(candidate)
                return RotationPersistOutcome.PERSISTED
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
                    # Another relay already saved the exact value we minted —
                    # the grant the vault denotes IS ours, so this is a success,
                    # not an abandon.
                    return RotationPersistOutcome.PERSISTED
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
                    # provider. There is nothing left to save. It reports its
                    # own outcome because the two cases mean different things to
                    # the user (a disconnect vs a reconnect) and the App Server
                    # records which one happened.
                    return (
                        RotationPersistOutcome.SENTINEL_VANISHED
                        if observed is None
                        else RotationPersistOutcome.SUPERSEDED
                    )
            except (api.ApiError, vault_mod.VaultError) as exc:
                raise _rotation_persist_failed(provider, exc) from exc
        # Unreachable for any sane budget: the final attempt either returns or
        # raises. Only a caller passing attempts <= 0 falls through, and an
        # unsaved rotation must never look like a successful one.
        raise _rotation_persist_failed(
            provider, ValueError(f"no write-back attempts made (attempts={attempts})")
        )

    def _adopt(self, fresh: vault_mod.Vault) -> None:
        """Make ``fresh`` the run's vault, mutating IN PLACE.

        Never ``self._vault = fresh``. The orchestrator holds the same object
        and reads it for every card's direct ``vault_key`` lookup, so a rebind
        would leave it on the run-start document forever while this resolver
        moved on — one run resolving two cards against two different vaults.
        Assigning the fields keeps one vault per run, which is the shape the
        browser gets from its ``VaultEntryReader`` handle for free.
        """
        with self._vault_lock:
            v = self._vault
            v.doc, v.p2s, v.p2c, v.revision = (
                fresh.doc,
                fresh.p2s,
                fresh.p2c,
                fresh.revision,
            )

    def _reload_vault(self) -> bool:
        """Adopt the server's current vault document, returning success.

        Adopting the fresh document (and its revision) is what lets a
        subsequent write-back target the revision the server actually holds
        instead of conflicting on a stale one.

        Deduped: a caller whose reload was overtaken by a newer one skips its
        own fetch. It rides that document only when the reload BEGAN after this
        caller asked, so the document provably reflects everything up to the
        conflict or mint failure that sent us here — riding an older in-flight
        fetch could miss a write made in between, burn one of the three
        write-back attempts, and turn a savable rotation into a reported
        failure.
        """
        # Sampled BEFORE the lock, so it is the count of reloads begun as of the
        # moment this caller asked. A reload already in flight has bumped the
        # counter at its start, i.e. before this read, so it cannot inflate the
        # comparison below; only a reload that began afterwards can. That is the
        # whole reason the bump is at the start and not on completion.
        seq = self._reload_seq
        with self._vault_lock:
            if self._reload_done_seq > seq:
                return True
            self._reload_seq += 1
            mine = self._reload_seq
            try:
                fresh = vault_mod.fetch_and_decrypt(self._api, self._password)
            except (api.ApiError, vault_mod.VaultError):
                return False
            self._adopt(fresh)
            self._reload_done_seq = mine
            return True

    def _sentinel_token(self, provider: str, sentinel_key: str) -> str | None:
        """The sentinel's current refresh-token, or ``None`` when absent."""
        with self._vault_lock:
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

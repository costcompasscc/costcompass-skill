# LOCKSTEP: one of three relay implementations (browser is the
# reference) — semantic changes here must land in the CostCompass
# monorepo, checked out alongside this repo, at
# ../costcompass/frontend/src/lib/refresh/orchestrator.ts,
# ../costcompass/frontend/src/lib/refresh/credential.ts, and
# ../costcompass/client/macos/CostCompassKit/Sources/CostCompassKit/
#   Refresh/RefreshOrchestrator.swift.
# See "Three relay implementations" in that repo's root CLAUDE.md;
# `make lockstep` there enumerates the whole set across both repos.

"""Drive a fetch run end to end: the CLI as the broker-relay client.

For each (provider, instance) entry the App Server plans, the
CLI resolves the vault credential, forwards each request through the
broker, and relays the broker-signed responses back for ingest.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import os
import sys
import threading
import time
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Self, TypeVar

from .. import api, config, services
from .. import vault as vault_mod
from ..render import money, safe_text
from . import oauth, program, signers
from .broker import (
    BrokerClient,
    BrokerError,
    BrokerForwardCapError,
    BrokerRunDeadlineError,
    broker_url_from_api,
    build_forward_request,
    provider_error_response,
    relay_status,
    to_raw_response_payload,
)

Echo = Callable[[str], None]
ProgressWrite = Callable[[str], None]

T = TypeVar("T")
R = TypeVar("R")


def _stdout_write(text: str) -> None:
    """Write progress output unbuffered (no trailing newline, flush now)."""
    sys.stdout.write(text)
    sys.stdout.flush()


class _ProgressTicker:
    """Emit a ``.`` every ``interval`` seconds on a background thread while a
    refresh is in flight, so a multi-second fetch doesn't look hung. Disabled
    (no thread, no output) when ``enabled`` is False (``--quiet``, a non-TTY,
    or any programmatic caller). On stop it emits a single trailing newline
    iff at least one dot was printed, so the results start on a clean line."""

    def __init__(
        self, enabled: bool, write: ProgressWrite, interval: float = 1.0
    ) -> None:
        self._enabled = enabled
        self._write = write
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ticked = False

    def __enter__(self) -> Self:
        if self._enabled:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def _loop(self) -> None:
        # Event.wait returns True the instant stop() fires, so a pending tick
        # never delays shutdown — no sleep-based race.
        while not self._stop.wait(self._interval):
            self._write(".")
            self._ticked = True

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if self._ticked:
            self._write("\n")


class RefreshError(Exception):
    """A refresh run could not be completed."""


class RefreshDeadlineExceeded(RefreshError):
    """A run exhausted ``RUN_BUDGET_S`` and gave up.

    Subclasses ``RefreshError`` so ``main.py``'s existing handler catches it
    with no change there.
    """


class RefreshAlreadyRunning(RefreshError):
    """Another refresh already holds the run lock.

    Subclasses ``RefreshError`` for the same reason as above: ``main.py``
    reports it through ``_fail``, so the user gets the sentence and a non-zero
    exit rather than a silent no-op.
    """


# The sentence the other two relays show for the same refusal (macOS
# ``ErrorText``, the browser hook's ``ALREADY_RUNNING``), so one refusal reads
# identically wherever the user meets it.
ALREADY_RUNNING_MESSAGE = "A refresh is already running."


@contextlib.contextmanager
def _acquire_run_lock() -> Generator[None]:
    """Hold the relay's refresh mutex for the duration of one run.

    LOCKSTEP INVARIANT — one refresh run at a time per relay, whatever the
    requested scope, and a refused request is always visible.

    The CLI is the one relay where concurrency is between *processes*, so no
    in-memory flag can see it: the browser guards with a ref in its refresh
    hook, macOS with ``AppState.isRefreshing``, and neither has an equivalent
    here. An advisory ``flock`` is the process-scoped equivalent.

    Correctness rests on the kernel releasing the lock when the descriptor
    closes, which covers a normal exit, a traceback, and a hard kill alike.
    That is the whole reason not to use a pid file: a pid file outlives the
    process that wrote it, so it needs staleness rules, and those rules are
    where pid files go wrong. Nothing here has to reason about a stale lock.

    The file itself is created once and never unlinked — removing it would race
    a process that already holds the descriptor, and an empty file costs
    nothing.
    """
    path = config.config_path().parent / "refresh.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RefreshAlreadyRunning(ALREADY_RUNNING_MESSAGE) from exc
        yield
    finally:
        os.close(fd)


# Wall-clock budget for one whole run. Bounding a single request does not bound
# a run: an entry may hold up to the broker's per-entry forward cap in requests,
# each retried up to ``RETRY_ATTEMPTS`` times honouring a Retry-After the broker
# caps at ten minutes, so every per-request timeout can be respected while the
# run lasts hours. That product is what this bounds. All three relays hold the
# same figure, and scripts/lib/conn_limits.py in the monorepo pins both it and
# the two retry constants equal across the three — the retry pair multiplies
# into the overrun, so a relay that drifts to a larger product re-opens it.
#
# Three seams consult it, all at points where no forward is in flight: before a
# worker claims its next entry, before a flat request is issued, and — since a
# deadline-blind backoff was worth ~40 minutes on its own — before each retry
# sleep (``BrokerClient.forward_with_retry``'s ``can_wait``). Nothing already
# issued is interrupted.
#
# WALL clock (``time.time``), not monotonic, and deliberately so: what this
# budget bounds keeps elapsing while the machine sleeps. The relay's refresh
# lock stays held, the run row stays ``running`` server-side, and the retention
# purge that is currently the only thing which clears it is itself wall-clock.
# A monotonic clock stops during sleep, so a laptop closed for two hours would
# wake still believing it was inside its budget and resume feeding a run that
# has, by every clock that matters to the server, been open for hours. Retry
# backoff and the interpreter's poll deadlines keep their own monotonic clocks
# — those measure pure durations, a different job.
RUN_BUDGET_S = 15 * 60


# Entries processed at once within one run, matching the browser reference's
# DEFAULT_CONCURRENCY. All three relays hold the same width on purpose — the
# broker's per-user gate and the proxy's per-IP budget were sized against one
# client fan-out figure, and scripts/lib/conn_limits.py in the monorepo asserts
# the three constants are equal so a per-port tune cannot drift past it.
# Intra-plan request fan-out stays at 1 (``_run_flat`` below is sequential),
# so a run's in-flight ceiling is this number.
DEFAULT_CONCURRENCY = 5


def _run_with_concurrency(
    items: list[T],
    limit: int,
    worker: Callable[[T], R],
    should_stop: Callable[[], bool] | None = None,
) -> tuple[list[R], bool]:
    """Run ``worker`` over ``items`` with an in-flight cap, preserving order.

    Long-lived threads pulling from a shared index — not chunked batches, so a
    slow item never stalls the others, and not ``ThreadPoolExecutor.map``,
    which queues every task up front and would leave the run-budget hook below
    nothing to stop feeding.

    Results are written by index into a pre-sized list, so the output order is
    the input order regardless of completion order.

    ``should_stop`` is consulted WHILE HOLDING the index lock, before a thread
    claims its next item, and never mid-item. Checking inside the lock is what
    makes the returned ``stopped`` agree with what was actually claimed —
    checked outside it, two threads can both observe "not yet expired" and
    claim past the deadline. Items already claimed always run to completion:
    a check only at a seam where nothing is pending is what lets a run end
    without manufacturing a half-submitted entry.

    Returns ``(results, stopped)``. Claimed indices are a monotonic prefix, so
    the filled region is exactly ``results[:claimed]`` and the caller never
    sees a ``None`` hole; ``stopped`` says whether any item was left unclaimed.

    ``worker`` must not raise: callers model every failure class as a returned
    outcome. An unmodelled exception is still not swallowed — a bare
    ``threading.Thread`` would drop it on the floor, so the first one is
    captured and re-raised here, and the remaining items are abandoned rather
    than dispatched.

    DEVIATION from the browser, deliberately: there a worker rejection rejects
    ``Promise.all`` while sibling runners keep draining ``items`` unawaited.
    Here every thread is joined first, so the exception surfaces with no
    orphaned in-flight work behind it.
    """
    results: list[Any] = [None] * len(items)
    next_index = 0
    # A list, not a plain `nonlocal` binding: the assignment happens in a
    # worker thread, and reading the name back here is what a type checker
    # narrows to "always None" — a mutation it cannot see.
    failures: list[BaseException] = []
    index_lock = threading.Lock()

    def runner() -> None:
        nonlocal next_index
        while True:
            with index_lock:
                if failures or next_index >= len(items):
                    return
                if should_stop is not None and should_stop():
                    return
                idx = next_index
                next_index += 1
            try:
                results[idx] = worker(items[idx])
            except BaseException as exc:  # noqa: BLE001 — re-raised below
                with index_lock:
                    failures.append(exc)
                return

    cap = max(1, min(limit, len(items) or 1))
    threads = [threading.Thread(target=runner) for _ in range(cap)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if failures:
        raise failures[0]
    # ``next_index`` is the claim count once every thread has joined.
    return results[:next_index], next_index < len(items)


# Wire markers for "this entry fetched nothing", mirroring the App Server's
# ``_SYNTHETIC_SKIP_REASONS`` table (backend/app/services/fetch_service.py).
# Every one records the benign ``skipped`` terminal state — no reauth badge —
# and the server stores the reason for triage. A cross-implementation test pins
# these literals against the backend table and the other two relays.
SKIP_NO_CREDENTIALS = "no_credentials"
SKIP_SENTINEL_VANISHED = "oauth_sentinel_vanished"
SKIP_ROTATION_SUPERSEDED = "oauth_rotation_superseded"
SKIP_UNSUPPORTED_KIND = "unsupported_credential_kind"

# Which marker each aborted rotation reports. Keyed lookup, not a conditional:
# an unmapped abort outcome raises ``KeyError`` here instead of being silently
# swallowed by an else-branch and mislabelled as a supersede. Mirrors
# ``SKIP_FOR_ABORT`` in the browser's credential.ts, where it is compile-checked.
SKIP_FOR_ABORT: dict[oauth.RotationPersistOutcome, str] = {
    oauth.RotationPersistOutcome.SENTINEL_VANISHED: SKIP_SENTINEL_VANISHED,
    oauth.RotationPersistOutcome.SUPERSEDED: SKIP_ROTATION_SUPERSEDED,
}

# Reserved ``public_config`` key the App Server sets on a keyless-by-design
# (subscription-only) card at creation. Mirrors the backend's
# ``SUBSCRIPTION_ONLY_CONFIG_KEY`` (backend/app/api/routes/providers.py), the
# browser's copy in frontend/src/lib/refresh/credential.ts, and the macOS
# ``RefreshOrchestrator.subscriptionOnlyConfigKey`` — keep the string in sync.
# Its presence is what lets this resolver tell a legitimate keyless card from a
# card whose vault key is merely missing.
SUBSCRIPTION_ONLY_CONFIG_KEY = "subscription_only"


class CredentialSkip(Exception):
    """Nothing to fetch for this card — *skip* it, don't fail it.

    Covers a keyless / subscription-only card (no vault entry on the flat
    path), a row whose credential shape the CLI can't satisfy (an OAuth
    App-installation row, whose token needs an App-Server-issued grant rather
    than a vault refresh-token sentinel), and an OAuth grant that changed
    underneath the run. Mirrors the browser's benign skip — recorded
    server-side as ``skipped`` rather than an auth failure.

    ``reason`` is the server-recognised wire marker saying WHICH of those it
    was; the App Server persists a matching sentence for triage.
    """

    def __init__(self, message: str, *, reason: str = SKIP_NO_CREDENTIALS) -> None:
        super().__init__(message)
        self.reason = reason


class CredentialMissing(Exception):
    """A credential this card was EXPECTED to have could not be obtained.

    Distinct from ``CredentialSkip``, which means there was never one to get:
    this records a *failed* card the user can act on, where a skip records a
    benign one they cannot. ``_process_entry`` turns it into a synthetic 401 so
    the App Server files the card under the auth taxonomy — mirroring the
    browser's ``submitMissingCredentials`` and the macOS ``.missingCredentials``
    resolution case.
    """


@dataclass
class EntryOutcome:
    provider_id: str
    instance_key: str
    state: str
    events_ingested: int = 0
    error_message: str | None = None
    # Human-facing card label (e.g. a google project-set name). Shown instead
    # of the raw ``instance_key`` UUID, which is an internal id of no use to
    # the user. ``None`` for single-instance / unlabeled cards.
    instance_label: str | None = None


@dataclass
class RefreshResult:
    """Outcome of a whole refresh run: the per-entry results plus the closing
    month-to-date (scoped to the refreshed service, else the whole account).
    ``mtd_usd`` is returned so a JSON caller need not re-fetch the summary."""

    outcomes: list[EntryOutcome]
    mtd_usd: float


def _resolve_credential(
    entry: dict[str, Any],
    vault: vault_mod.Vault,
    resolver: oauth.OAuthResolver | None,
) -> str:
    """Return the plaintext credential for an entry, or raise.

    Generic: the untrusted relay always tries a direct vault entry first (a pasted
    key). For minted credentials the App Server authors the routing on the
    entry's ``credential`` field; the CLI executes the kinds it implements
    (``oauth_mint``). No provider knowledge lives here — it's all server-authored.

    The two ways this raises are not interchangeable. ``CredentialSkip`` says
    there was never a credential to get — a keyless subscription-only card, or an
    ``oauth_installation_grant`` row this client is documented not to serve.
    ``CredentialMissing`` says one was expected and isn't here, which is a failed
    card the user needs to see.

    ``vault`` is the run's ONE vault document, the same object the resolver
    holds — see the ownership note on ``OAuthResolver.__init__``. That is what
    makes the direct lookup below observe a mid-run reload, and what guarantees
    it can never observe a rotation the server has not accepted. Reading it
    here without ``_vault_lock`` is safe for that second reason: the only
    mutation is ``_adopt`` assigning fields of an already-committed document.
    """
    provider = entry["provider_id"]
    instance = entry.get("instance_key", "")
    direct = vault.entry_for(provider, instance)
    if direct and direct.get("api_key"):
        return direct["api_key"]

    # An absent ``credential`` object means ``vault_key`` — the same default the
    # browser applies (``entry.credential ?? { kind: "vault_key" }``), so it
    # reaches the keyless-vs-missing decision at the bottom rather than
    # short-circuiting into an unconditional skip.
    routing = entry.get("credential") or {}
    kind = routing.get("kind")
    if kind == "oauth_mint":
        sentinel_key = routing.get("sentinel_key")
        mint_path = routing.get("mint_path")
        if not (sentinel_key and mint_path):
            # A plugin's ``credential_routing()`` is malformed — a defect, not a
            # card without a credential. Failed rather than skipped so it is
            # visible; a skip reads as healthy and hid the misconfiguration.
            raise CredentialMissing(
                f"malformed oauth_mint routing for '{provider}' — missing "
                "sentinel_key or mint_path; check the plugin's "
                "credential_routing() override"
            )
        if resolver is None:
            # Mint-routed entry in a run with no OAuth resolver: this client
            # cannot obtain the credential the server says exists. Wiring defect,
            # so fail rather than fall through to the keyless decision below,
            # where the card would be judged on a marker that says nothing about
            # it. (``run_refresh`` always builds a resolver; None is a caller
            # error, not a card state.)
            raise CredentialMissing(
                f"oauth_mint routing for '{provider}' but this run has no OAuth "
                "resolver — the credential cannot be minted"
            )
        try:
            return resolver.access_token(provider, sentinel_key, mint_path)
        except oauth.RotationAborted as exc:
            # The vault's grant changed mid-run (the user disconnected or
            # reconnected while this refresh was in flight), so the minted
            # access token has been discarded and there is nothing to fetch
            # with. Re-raised as a benign skip rather than left to fall
            # through to the OAuthError handler: that path would badge the
            # card ``reauth_required``, and the replacement grant is
            # healthy. Telling the user to reconnect a working connection is
            # its own bug. Mapping it HERE keeps the one skip-shaped exit
            # in one place, and keeps oauth.py from importing this module.
            raise CredentialSkip(str(exc), reason=SKIP_FOR_ABORT[exc.outcome]) from exc

    if kind == "oauth_installation_grant":
        # A documented scope limit of this client, not a vault problem: the
        # grant is App-Server-issued and only the web app can mint it. Decided
        # BEFORE the marker read below for exactly that reason — the card is
        # fine, this relay simply cannot serve it. Same marker as any other
        # kind this relay doesn't implement: one concept, one reason.
        raise CredentialSkip(
            "this card needs an App-Server-issued installation grant the CLI "
            "can't mint — refresh it from the web app",
            reason=SKIP_UNSUPPORTED_KIND,
        )

    if kind is not None and kind != "vault_key":
        # A kind the server shipped before this client learned it — the normal
        # rollout order, and the CLI is versioned independently of the server so
        # it can lag for months. Benign skip naming the reason, decided by the
        # kind ALONE: the ``subscription_only`` marker below answers "is this
        # keyless-by-design vault_key card?" and says nothing about a mechanism
        # this relay never implemented. Judging the second by the first failed
        # every affected card with a false "no credential configured".
        raise CredentialSkip(
            f"credential kind '{kind}' is not supported by this client — update it",
            reason=SKIP_UNSUPPORTED_KIND,
        )

    # ``vault_key`` with no direct entry. The App
    # Server emits a plan optimistically for every enabled card — it cannot see
    # the vault — so a bare miss is ambiguous: either a legitimate keyless
    # subscription-only card (anthropic/openai, which fold a subscription) or a
    # keyed card whose vault entry is missing on THIS machine (a host the user
    # never pasted the key on, a non-merged vault conflict). Don't guess from the
    # miss — that is a shadow state, and guessing "keyless" is what let a missing
    # key report as healthy here. The server persists a ``subscription_only``
    # marker on keyless-by-design cards; read the real flag. Marked → benign
    # skip. Unmarked → a key was expected and is gone, so fail the card and let
    # the user see it.
    public_config = entry.get("public_config") or {}
    if public_config.get(SUBSCRIPTION_ONLY_CONFIG_KEY) == "true":
        raise CredentialSkip("subscription-only card — no credential to fetch")
    raise CredentialMissing(f"no credential configured for '{provider}'")


def _synthetic_cap(url: str, purpose: str) -> dict[str, Any]:
    """Synthetic stub for the per-entry forward-cap tripwire (mirrors the
    browser): the App Server short-circuits to ``forward_cap_exceeded``."""
    return {
        "request_url": url,
        "request_purpose": purpose,
        "status": 429,
        "headers": {},
        "body_b64": base64.b64encode(b"").decode("ascii"),
        "synthetic": True,
        "synthetic_reason": "forward_cap_exceeded",
    }


def _synthetic_for_request(
    req: dict[str, Any], status: int, explain: str
) -> dict[str, Any]:
    """Synthetic stub standing in for one flat sub-request that produced no
    upstream response.

    Two properties of this shape are load-bearing, and both are why every flat
    early exit uses it rather than the unsigned provider-error some of them
    used to emit:

    - **Flagged synthetic**, so the App Server strips it before
      ``plugin.process()``. The siblings that DID come back still ingest, and
      one dead sub-request no longer discards a whole month of fetched usage.
      The entry is not silently reported whole either — the server records the
      window as incomplete and shortens the card's debounce so the gap gets
      another chance on the next refresh.
    - **The real request URL**, not a ``cc-internal://`` sentinel. For a per-day
      fan-out that URL is how the server knows WHICH day is missing; a sentinel
      matches no planned request and the gap becomes unattributable. It is also
      what writes the day's ``failed`` state row, so the next refresh re-plans
      exactly that day — a request that simply goes missing writes no row.

    ``explain`` is carried verbatim into the card's error text and must stay
    secret-free.
    """
    return {
        "request_url": req.get("url", ""),
        "request_purpose": req.get("purpose", ""),
        "status": status,
        "headers": {},
        "body_b64": base64.b64encode(explain.encode()).decode("ascii"),
        "synthetic": True,
    }


def _synthetic_broker_failure(req: dict[str, Any], err: BrokerError) -> dict[str, Any]:
    """Synthetic stub for a flat sub-request whose broker forward failed
    (mirrors the browser's ``brokerFailureToResponse``).

    The body format matches the browser's byte for byte — the shared relay
    vectors compare it, so this string is wire contract, not a log line.
    """
    explain = f"{err.code}{f':{err.request_id}' if err.request_id else ''}: {err}"
    return _synthetic_for_request(req, relay_status(err), explain)


def _synthetic_deadline(req: dict[str, Any]) -> dict[str, Any]:
    """Synthetic stub for a flat sub-request the run budget left unissued.

    No ``synthetic_reason``: the server's marker short-circuits fire only when
    the synthetic is an entry's SOLE response, and a truncated fan-out submits
    these alongside the siblings that did arrive. The generic strip-and-record
    path is the one that should handle them.
    """
    return _synthetic_for_request(req, 504, "run deadline exceeded")


def _entry_purpose(plan: dict[str, Any]) -> str:
    """Generic relay purpose for a synthetic/error response: the program's
    purpose, else the first request's, mirroring the browser. The purpose is
    a required wire field, so callers use a hard fallback when this is ``""``.
    """
    program = plan.get("program") or {}
    if program.get("purpose"):
        return str(program["purpose"])
    requests = plan.get("requests") or []
    if requests and requests[0].get("purpose"):
        return str(requests[0]["purpose"])
    return ""


def _entry_first_url(plan: dict[str, Any]) -> str | None:
    requests = plan.get("requests") or []
    return requests[0].get("url") if requests else None


def _synthetic_skip(plan: dict[str, Any], reason: str, detail: str) -> dict[str, Any]:
    """Synthetic stub mirroring the browser's benign skip: one response flagged
    with a server-recognised ``synthetic_reason`` so the App Server records the
    entry as ``skipped`` (it reads ``synthetic_reason``, not the body, and never
    runs ``plugin.process()`` on it).

    ``reason`` distinguishes WHY nothing was fetched — a keyless
    subscription-only card, or an OAuth grant that changed mid-run. All of them
    stay badge-free."""
    return {
        "request_url": _entry_first_url(plan) or f"synthetic://{reason}",
        "request_purpose": _entry_purpose(plan) or reason,
        "status": 204,
        "headers": {},
        "body_b64": base64.b64encode(detail.encode()).decode("ascii"),
        "synthetic": True,
        "synthetic_reason": reason,
    }


def _submit_outcome(
    client: api.Client,
    run_id: str,
    provider: str,
    instance: str,
    responses: list[dict[str, Any]],
    *,
    fallback_state: str,
    local_message: str | None = None,
    instance_label: str | None = None,
) -> EntryOutcome:
    """POST one entry's responses and fold the server's verdict into an
    ``EntryOutcome``. A submit failure can't abort the whole run — it
    degrades to ``fallback_state``. Always attach a reason: the normal
    fetch→submit path passes no ``local_message``, and a failed card with a
    blank caption is undiagnosable in the UI.

    The reason carries the failure's own detail. This client has no logger and
    no telemetry — the per-card line printed by ``run`` is the only channel a
    submit failure has, so discarding the status here left a card reporting a
    problem with nothing to diagnose it by."""
    try:
        result = client.submit_responses(
            run_id,
            {"provider_id": provider, "instance_key": instance, "responses": responses},
        )
    except api.ApiError as exc:
        detail = f"HTTP {exc.status}: {exc}" if exc.status is not None else str(exc)
        return EntryOutcome(
            provider,
            instance,
            fallback_state,
            error_message=local_message
            or f"Could not submit results ({detail}) — try refreshing again.",
            instance_label=instance_label,
        )
    return EntryOutcome(
        provider,
        instance,
        result.get("state", fallback_state),
        events_ingested=result.get("events_ingested", 0),
        # The server authors the user-visible sentence and we render it; ours is
        # only for the ApiError branch above, where no verdict arrived. ``or``,
        # not a None-check: the server sends "" for a skip with nothing to
        # explain (``SYNTHETIC_SKIP_REASONS`` in backend/app/schemas/fetch.py),
        # which means "nothing to say" and must fall back rather than print an
        # empty note. The sibling relays normalise the empty string explicitly
        # because ``??`` would not — do not "simplify" this to match them.
        error_message=result.get("error_message") or local_message,
        instance_label=instance_label,
    )


def _run_flat(
    plan: dict[str, Any],
    auth_headers: dict[str, str],
    signing_token: str | None,
    broker: BrokerClient,
    budget_allows: Callable[[float], bool] | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    requests = list(plan.get("requests", []))
    for i, req in enumerate(requests):
        # The run budget's second seam. An entry may hold many requests, so
        # checking only between entries would honour the budget on paper while
        # one entry stayed wedged for hours. Checked before the request is
        # issued, never mid-flight. ``0.0`` asks only whether any budget is
        # left; the third seam, inside the retry loop below, asks whether a
        # specific backoff fits.
        if budget_allows is not None and not budget_allows(0.0):
            # Stop issuing, but bank what arrived: a synthetic stub per request
            # the budget cut off, so the App Server strips them, ingests the
            # siblings, and records the window as incomplete rather than
            # discarding a nearly-whole month. One stub PER abandoned request,
            # not one for the entry, because each carries its own URL — that is
            # what makes the individual missing days attributable and re-planned
            # on the next refresh.
            #
            # This used to emit an unsigned NON-synthetic provider error, to
            # stop a short response set reading as a clean success. The App
            # Server records window completeness now, so the honesty that
            # bought no longer costs the entry's fetched siblings — the same
            # reasoning that moved the broker-failure branch below.
            #
            # All three of this loop's early exits now bank siblings alike.
            out.extend(_synthetic_deadline(r) for r in requests[i:])
            return out
        try:
            resp = broker.forward_with_retry(
                build_forward_request(req, auth_headers, signing_token),
                can_wait=budget_allows,
            )
        except BrokerForwardCapError:
            out.append(_synthetic_cap(req.get("url", ""), req.get("purpose", "")))
            return out
        except BrokerRunDeadlineError:
            # A backoff that did not fit the budget. Bank this request as the
            # same stub the seam above emits for one never issued — from the
            # App Server's side they are one event, a planned segment nobody
            # fetched. Continue rather than stubbing the tail: one provider
            # asking for a ten-minute wait has not spent the run's budget, and
            # the seam above ends the loop once it truly is spent.
            out.append(_synthetic_deadline(req))
            continue
        except BrokerError as err:
            # Continue: one failed sub-request must not poison the others, and
            # the synthetic stub is what makes that true rather than aspirational
            # — the server strips it, ingests the siblings, and records the
            # window as incomplete so the gap is reported and retried.
            out.append(_synthetic_broker_failure(req, err))
            continue
        out.append(to_raw_response_payload(req, resp))
    return out


def _process_entry(
    entry: dict[str, Any],
    vault: vault_mod.Vault,
    broker: BrokerClient,
    client: api.Client,
    run_id: str,
    resolver: oauth.OAuthResolver | None,
    budget_allows: Callable[[float], bool] | None = None,
) -> EntryOutcome:
    provider = entry["provider_id"]
    instance = entry.get("instance_key", "")
    label = entry.get("instance_label")
    plan = entry.get("plan")
    if plan is None:
        return EntryOutcome(
            provider,
            instance,
            entry.get("state", "skipped"),
            error_message=entry.get("skip_reason") or entry.get("error_message"),
            instance_label=label,
        )

    # A credential failure for one card must not abort the whole run, AND the
    # server must be told so the row's state updates instead of showing the
    # previous attempt. The shape of the failure decides the response (mirrors
    # the browser): a keyless/unsupported card is a benign skip; an OAuth mint
    # failure carries a taxonomy status the server should classify.
    try:
        credential = _resolve_credential(entry, vault, resolver)
    except CredentialSkip as exc:
        return _submit_outcome(
            client,
            run_id,
            provider,
            instance,
            [_synthetic_skip(plan, exc.reason, str(exc))],
            fallback_state="skipped",
            local_message=str(exc),
            instance_label=label,
        )
    except CredentialMissing as exc:
        # Synthesize a 401 so the server's plugin.process() raises
        # ProviderAuthError and the card records ``failed`` under the auth
        # taxonomy — mirroring the browser's ``submitMissingCredentials``. The
        # user has no working credential for this card, which is accurate and
        # actionable, where a skip was neither.
        return _submit_outcome(
            client,
            run_id,
            provider,
            instance,
            [
                provider_error_response(
                    401,
                    _entry_purpose(plan) or "missing_credentials",
                    str(exc),
                )
            ],
            fallback_state="failed",
            local_message=str(exc),
            instance_label=label,
        )
    except oauth.OAuthError as exc:
        # A 409 is the oauth-broker's reauth_required (dead OAuth grant). Mark
        # the body with the code so the App Server's shared reauth classifier
        # keys on it — a message-only body would be misfiled as a generic
        # failure. Other statuses (401 caller-auth, 429, 5xx) classify by status.
        error_code = "reauth_required" if exc.status == 409 else None
        return _submit_outcome(
            client,
            run_id,
            provider,
            instance,
            [
                provider_error_response(
                    exc.status,
                    # Hard fallback: request_purpose is a required wire field
                    # (mirrors the browser's `?? "missing_credentials"`).
                    _entry_purpose(plan) or "missing_credentials",
                    str(exc),
                    error_code=error_code,
                )
            ],
            fallback_state="failed",
            local_message=str(exc),
            instance_label=label,
        )

    auth_headers = signers.build_plan_headers(plan, credential)
    signing_token = entry.get("signing_token")

    if plan.get("program"):
        # The interpreter maps provider/transport failures to provider-error
        # responses itself; the ONE thing it re-throws is the per-entry
        # forward cap, which routes to the dedicated ``forward_cap_exceeded``
        # state via a single synthetic stub (mislabelling a runaway poll loop
        # as an auth failure would hide it, and letting it propagate would
        # abort the whole run unfinalized). Mirrors the browser's
        # submitForwardCapExceeded and the macOS runPlan.
        try:
            responses = program.run_program(
                plan["program"], auth_headers, signing_token, broker
            )
        except BrokerForwardCapError:
            responses = [
                _synthetic_cap(
                    _entry_first_url(plan) or "synthetic://forward_cap_exceeded",
                    _entry_purpose(plan) or "forward_cap_exceeded",
                )
            ]
    else:
        responses = _run_flat(plan, auth_headers, signing_token, broker, budget_allows)

    return _submit_outcome(
        client,
        run_id,
        provider,
        instance,
        responses,
        fallback_state="failed",
        instance_label=label,
    )


def run(
    cfg: config.Config,
    api_key: str,
    service: str | None,
    password: str,
    *,
    run_lock: Callable[[], AbstractContextManager[None]] = _acquire_run_lock,
    **kwargs: Any,
) -> RefreshResult:
    """Run one refresh, holding the relay's refresh mutex for its whole length.

    Split from ``_run`` so the lock wraps everything a run does — including the
    vault fetch and create-run, which is where a second process would otherwise
    do its damage — without indenting the body under a ``with``.

    ``run_lock`` is injected for the same reason ``now`` and the clients are:
    tests drive concurrency deliberately, and must not contend for a lock in the
    developer's real config directory.
    """
    with run_lock():
        return _run(cfg, api_key, service, password, **kwargs)


def _run(
    cfg: config.Config,
    api_key: str,
    service: str | None,
    password: str,
    *,
    client: api.Client | None = None,
    broker: BrokerClient | None = None,
    oauth_client: oauth.OAuthBrokerClient | None = None,
    echo: Echo = print,
    progress: bool = False,
    progress_write: ProgressWrite = _stdout_write,
    concurrency: int = DEFAULT_CONCURRENCY,
    now: Callable[[], float] = time.time,
    run_budget_s: float = RUN_BUDGET_S,
) -> RefreshResult:
    owns_client = client is None
    if client is None:
        client = api.Client(cfg.api_url, api_key)
    if broker is None:
        broker = BrokerClient(broker_url_from_api(cfg.api_url), api_key)
    if oauth_client is None:
        oauth_client = oauth.OAuthBrokerClient(
            oauth.oauth_url_from_api(cfg.api_url), api_key
        )

    # Computed BEFORE the vault fetch and create-run, so everything a run does
    # counts against its budget — a run that spent ten minutes decrypting has
    # already used most of its time. See RUN_BUDGET_S for why this is wall
    # clock rather than monotonic.
    deadline = now() + run_budget_s

    def budget_allows(seconds: float) -> bool:
        """Is there room in the budget to spend ``seconds`` and still be inside it?

        One predicate for all three seams rather than a bare deadline passed
        around, so "expired" means one thing everywhere. ``0.0`` asks the
        entry-pool and request-loop question ("any budget left at all"); a
        retry's backoff asks whether that particular wait fits.
        """
        return now() + seconds < deadline

    def past_deadline() -> bool:
        return not budget_allows(0.0)

    try:
        try:
            # LOCKSTEP INVARIANT — a run mints from the SERVER's current vault,
            # or it does not start. Here that falls out of the shape (this is a
            # short-lived process, so the fetch is always this run's), which is
            # exactly why it is written down: a refactor that hoisted `vault`
            # out to a longer-lived object would silently take it away.
            #
            # It matters because an OAuth refresh_token that rotates on every
            # mint is single-use and SHARED with the browser and the macOS app.
            # Presenting one another relay already consumed is a replay, and a
            # provider with reuse detection (cloudflare) answers a replay by
            # revoking the whole token family — the correct current token dies
            # with it. The 409 retry in `oauth.py` cannot undo that; it only
            # covers the seconds-wide race. See invariant 9 in
            # frontend/src/lib/refresh/CLAUDE.md.
            vault = vault_mod.fetch_and_decrypt(client, password)
        except vault_mod.VaultError as exc:
            # Surface a clean message (wrong password / no vault) instead of a
            # raw traceback; main only catches RefreshError from this path.
            raise RefreshError(str(exc)) from exc
        resolver = oauth.OAuthResolver(oauth_client, client, vault, password)

        providers: list[str] | None = None
        scoped_provider: str | None = None
        if service:
            prov = services.resolve(service, client.providers())
            scoped_provider = prov["id"]
            providers = [scoped_provider]

        # Tick a dot per second across the network-heavy span (plan → forward
        # → submit → finalize); the ticker stops and emits a newline before the
        # result lines, so dots and results never interleave.
        outcomes: list[EntryOutcome]
        with _ProgressTicker(progress, progress_write):
            run_data = client.create_fetch_run(providers)
            run_id = run_data["run_id"]
            outcomes, stopped = _run_with_concurrency(
                list(run_data.get("fetches", [])),
                concurrency,
                lambda entry: _process_entry(
                    entry, vault, broker, client, run_id, resolver, budget_allows
                ),
                past_deadline,
            )
            if stopped:
                # Entries left unclaimed: the budget ran out. Reached only after
                # every thread has joined, so no card is still submitting and
                # the run is not finalized out from under one.
                #
                # Finalize as cancelled so the row leaves `running` —
                # ``finalize_run`` is its only writer, so an unfinalized run
                # stays running until the retention horizon deletes it.
                stranded = ""
                try:
                    client.finalize_run(run_id, cancelled=True)
                except api.ApiError as exc:
                    # The THROW is swallowed: the deadline is the story the
                    # caller needs, and raising the finalize error instead would
                    # report the wrong cause. The FAILURE is not — it rides
                    # along on that same message.
                    #
                    # NOT "the server's lease reaps the row either way", which is
                    # what stood here and was false — there is no server-side
                    # reaper, and invariant 10 in the browser's refresh
                    # CLAUDE.md says not to write that sentence until the lease
                    # exists. While the row sits `running` the App Server's four
                    # ``run.status != "running"`` guards never fire, so a late
                    # submit into it still writes. That is what makes this worth
                    # reporting: nothing else anywhere records that the run was
                    # abandoned mid-flight.
                    #
                    # The exception message rather than ``echo`` because this has
                    # to reach the user in every mode: ``echo`` is a no-op under
                    # ``--json`` (see main), which is the scripted use where a
                    # stranded run is least likely to be noticed, and the call
                    # site sits inside the progress ticker, whose dots an echoed
                    # line would land in the middle of. Raising instead reaches
                    # main's single error sink, which writes stderr and sanitizes.
                    #
                    # Status only, never the error text — the same rule the two
                    # sibling relays follow, and the status is also the part
                    # worth having: a 502/504 is the gateway being down, while a
                    # 404 means this relay finalized against an id the server
                    # does not have, which is a defect.
                    #
                    # A 409 no longer appears here. Re-finalizing is idempotent
                    # — an already-terminal run replays its recorded outcome —
                    # so a second finalize is a successful no-op rather than an
                    # error. ``Client.finalize_run`` relies on that to retry
                    # once when the outcome is unknown, which is why reaching
                    # this branch now means the run really is unfinalized.
                    # No newline before it: the sink strips control characters,
                    # ``\n`` among them, so the sentences run together anyway.
                    detail = (
                        f"HTTP {exc.status}"
                        if exc.status is not None
                        else "the server could not be reached"
                    )
                    stranded = (
                        f" The server was not told this run was cancelled "
                        f"({detail}), so it may show as still running for a while."
                    )
                raise RefreshDeadlineExceeded(
                    "Refresh took too long and was stopped. Some providers may "
                    "not have been updated — try again." + stranded
                )
            client.finalize_run(run_id)

        for o in outcomes:
            # Prefer the human label; the raw instance_key UUID is an internal
            # id and only noise to the user (no bracket when there's no label).
            label = safe_text(o.provider_id) + (
                f" [{safe_text(o.instance_label)}]" if o.instance_label else ""
            )
            suffix = f" ({o.events_ingested} events)" if o.events_ingested else ""
            note = f" — {safe_text(o.error_message)}" if o.error_message else ""
            echo(f"  {label}: {safe_text(o.state)}{suffix}{note}")

        # Scope the closing MTD to the refreshed service, else the whole-account
        # total — so ``mtd google refresh`` reports google's number, not the sum.
        summary = client.summary(provider=scoped_provider)
        mtd_usd = float(summary.get("mtd_usd") or 0.0)
        echo(f"\nMonth-to-date: {money(mtd_usd)}")
        return RefreshResult(outcomes=outcomes, mtd_usd=mtd_usd)
    finally:
        if owns_client:
            client.close()

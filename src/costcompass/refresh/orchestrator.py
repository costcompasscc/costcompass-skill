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
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from .. import api, config, services
from .. import vault as vault_mod
from ..render import money, safe_text
from . import oauth, program, signers
from .broker import (
    BrokerClient,
    BrokerError,
    BrokerForwardCapError,
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

    def __enter__(self) -> "_ProgressTicker":
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


# Wall-clock budget for one whole run, checked only at seams where nothing is
# in flight. Bounding a single request does not bound a run: an entry may hold
# up to the broker's per-entry forward cap in requests, each retried up to
# ``DEFAULT_RETRY_ATTEMPTS`` times honouring a Retry-After the broker caps at
# ten minutes, so every per-request timeout can be respected while the run
# lasts hours. All three relays hold the same figure.
#
# WALL clock (``time.time``), not monotonic, and deliberately so: this budget
# has to agree with the server's wall-clock lease on an abandoned run. A
# monotonic clock stops while the machine sleeps, so a laptop closed for two
# hours would wake still believing it was inside its budget and submit into a
# run the server had long since reaped. Retry backoff and the interpreter's
# poll deadlines keep their own monotonic clocks — those measure pure
# durations, a different job.
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

# Which marker each aborted rotation reports. Keyed lookup, not a conditional:
# an unmapped abort outcome raises ``KeyError`` here instead of being silently
# swallowed by an else-branch and mislabelled as a supersede. Mirrors
# ``SKIP_FOR_ABORT`` in the browser's credential.ts, where it is compile-checked.
SKIP_FOR_ABORT: dict[oauth.RotationPersistOutcome, str] = {
    oauth.RotationPersistOutcome.SENTINEL_VANISHED: SKIP_SENTINEL_VANISHED,
    oauth.RotationPersistOutcome.SUPERSEDED: SKIP_ROTATION_SUPERSEDED,
}


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


def _skip_reason(kind: str | None, provider: str) -> str:
    """User-facing reason for skipping a card whose credential the CLI can't
    obtain — derived from the server-authored routing kind, no provider id."""
    if kind == "oauth_installation_grant":
        return (
            "this card needs an App-Server-issued installation grant the CLI "
            "can't mint — refresh it from the web app"
        )
    # ``vault_key`` with no direct entry (keyless / subscription-only card), or
    # any kind this client doesn't implement.
    return f"no credential configured for '{provider}'"


def _resolve_credential(
    entry: dict[str, Any],
    vault: vault_mod.Vault,
    resolver: oauth.OAuthResolver | None,
) -> str:
    """Return the plaintext credential for an entry, or raise.

    Generic: the untrusted relay always tries a direct vault entry first (a pasted
    key). For minted credentials the App Server authors the routing on the
    entry's ``credential`` field; the CLI executes the kinds it implements
    (``oauth_mint``) and skips the rest via ``CredentialSkip`` (a keyless
    ``vault_key`` row, an ``oauth_installation_grant`` it can't mint, or any
    unknown kind). No provider knowledge lives here — it's all server-authored.

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

    routing = entry.get("credential") or {}
    kind = routing.get("kind")
    if kind == "oauth_mint":
        sentinel_key = routing.get("sentinel_key")
        mint_path = routing.get("mint_path")
        if not (sentinel_key and mint_path):
            raise CredentialSkip(
                f"malformed oauth_mint routing for '{provider}' — missing "
                "sentinel_key or mint_path; check the plugin's "
                "credential_routing() override"
            )
        if resolver is not None:
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
                raise CredentialSkip(
                    str(exc), reason=SKIP_FOR_ABORT[exc.outcome]
                ) from exc
    raise CredentialSkip(_skip_reason(kind, provider))


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
        error_message=result.get("error_message") or local_message,
        instance_label=instance_label,
    )


def _run_flat(
    plan: dict[str, Any],
    auth_headers: dict[str, str],
    signing_token: str | None,
    broker: BrokerClient,
    past_deadline: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for req in plan.get("requests", []):
        # The run budget's second seam. An entry may hold many requests, so
        # checking only between entries would honour the budget on paper while
        # one entry stayed wedged for hours. Checked before the request is
        # issued, never mid-flight.
        if past_deadline is not None and past_deadline():
            # Append an unsigned, NON-synthetic provider error so the entry is
            # classified failed by the App Server's status taxonomy. Without it
            # the short response set reads as a clean success and silently
            # under-counts spend — the one outcome worse than a failed card.
            #
            # Cost, accepted deliberately: the plugin contract requires
            # process() to raise on a 5xx, and that raise discards THIS entry's
            # already-fetched siblings in the same submit. A partial usage
            # window ingested as if complete would under-count; the next
            # refresh re-pulls the whole window anyway. Other entries submitted
            # separately and are unaffected.
            #
            # Mirrors the forward-cap early return below exactly.
            out.append(
                provider_error_response(
                    504, req.get("purpose", ""), "run deadline exceeded"
                )
            )
            return out
        try:
            resp = broker.forward_with_retry(
                build_forward_request(req, auth_headers, signing_token)
            )
        except BrokerForwardCapError:
            out.append(_synthetic_cap(req.get("url", ""), req.get("purpose", "")))
            return out
        except BrokerError as err:
            # Relay a provider-error with the status the broker reported (so
            # the App Server classifies a rate-limit/timeout correctly rather
            # than as a generic 502) and continue — one failed sub-request must
            # not poison the others.
            out.append(
                provider_error_response(
                    relay_status(err),
                    req.get("purpose", ""),
                    f"{err.code}: {err}",
                )
            )
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
    past_deadline: Callable[[], bool] | None = None,
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
        responses = _run_flat(plan, auth_headers, signing_token, broker, past_deadline)

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

    def past_deadline() -> bool:
        return now() >= deadline

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
                    entry, vault, broker, client, run_id, resolver, past_deadline
                ),
                past_deadline,
            )
            if stopped:
                # Entries left unclaimed: the budget ran out. Reached only after
                # every thread has joined, so no card is still submitting and
                # the run is not finalized out from under one.
                #
                # Finalize as cancelled so the row leaves `running` — an
                # unfinalized run stays running until the server's lease reaps
                # it, and a run stuck running is precisely what makes the next
                # refresh appear to do nothing.
                try:
                    client.finalize_run(run_id, cancelled=True)
                except api.ApiError:
                    # Swallow: the deadline is the story the caller needs, and
                    # the server's lease reaps the row either way. Raising the
                    # finalize error instead would report the wrong cause.
                    pass
                raise RefreshDeadlineExceeded(
                    "Refresh took too long and was stopped. Some providers may "
                    "not have been updated — try again."
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

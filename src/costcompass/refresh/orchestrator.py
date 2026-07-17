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
from dataclasses import dataclass
from typing import Any, Callable

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
    status_for_code,
    to_raw_response_payload,
)

Echo = Callable[[str], None]
ProgressWrite = Callable[[str], None]


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


class CredentialSkip(Exception):
    """No usable credential for a card the CLI should *skip*, not fail.

    Covers a keyless / subscription-only card (no vault entry on the flat
    path) and a row whose credential shape the CLI can't satisfy (an OAuth
    App-installation row, whose token needs an App-Server-issued grant rather
    than a vault refresh-token sentinel). Mirrors the browser's
    subscription-only ``no_credentials`` skip — recorded server-side as a
    benign ``skipped`` rather than an auth failure.
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
            return resolver.access_token(provider, sentinel_key, mint_path)
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


def _synthetic_no_credentials(plan: dict[str, Any], detail: str) -> dict[str, Any]:
    """Synthetic stub mirroring the browser's subscription-only skip: one
    response flagged ``no_credentials`` so the App Server records the entry
    as a benign ``skipped`` (it reads ``synthetic_reason``, not the body, and
    never runs ``plugin.process()`` on it)."""
    return {
        "request_url": _entry_first_url(plan) or "synthetic://no_credentials",
        "request_purpose": _entry_purpose(plan) or "no_credentials",
        "status": 204,
        "headers": {},
        "body_b64": base64.b64encode(detail.encode()).decode("ascii"),
        "synthetic": True,
        "synthetic_reason": "no_credentials",
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
    blank caption is undiagnosable in the UI."""
    try:
        result = client.submit_responses(
            run_id,
            {"provider_id": provider, "instance_key": instance, "responses": responses},
        )
    except api.ApiError:
        return EntryOutcome(
            provider,
            instance,
            fallback_state,
            error_message=local_message
            or "Could not submit results — try refreshing again.",
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
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for req in plan.get("requests", []):
        try:
            resp = broker.forward_with_retry(
                build_forward_request(req, auth_headers, signing_token)
            )
        except BrokerForwardCapError:
            out.append(_synthetic_cap(req.get("url", ""), req.get("purpose", "")))
            return out
        except BrokerError as err:
            # Relay a provider-error with the broker's mapped status (so the
            # App Server classifies a rate-limit/timeout correctly rather than
            # as a generic 502) and continue — one failed sub-request must not
            # poison the others.
            out.append(
                provider_error_response(
                    status_for_code(err.code),
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
            [_synthetic_no_credentials(plan, str(exc))],
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
                    exc.status, _entry_purpose(plan), str(exc), error_code=error_code
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
        responses = _run_flat(plan, auth_headers, signing_token, broker)

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

    try:
        try:
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
        outcomes: list[EntryOutcome] = []
        with _ProgressTicker(progress, progress_write):
            run_data = client.create_fetch_run(providers)
            run_id = run_data["run_id"]
            for entry in run_data.get("fetches", []):
                outcomes.append(
                    _process_entry(entry, vault, broker, client, run_id, resolver)
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

# LOCKSTEP: one of three relay implementations (browser is the
# reference) — semantic changes here must land in the CostCompass
# monorepo, checked out alongside this repo, at
# ../costcompass/frontend/src/lib/broker/client.ts,
# ../costcompass/frontend/src/lib/refresh/plan-forward.ts, and
# ../costcompass/client/macos/CostCompassKit/Sources/CostCompassKit/
#   Refresh/BrokerClient.swift.
# See "Three relay implementations" in that repo's root CLAUDE.md;
# `make lockstep` there enumerates the whole set across both repos.

"""Broker forward client — the CLI's relay to `POST /broker/v1/forward`.

The CLI authenticates to the broker with its programmatic API key
(Authorization: Bearer); the broker validates it via the App Server's
whoami and never forwards it upstream (the provider auth comes only
from the plan's auth overlay).

Body fields on request and response are base64-encoded raw bytes; the
broker's response signature is relayed verbatim and never inspected.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from .. import user_agent

FORWARD_CAP = 100

# Bounded retry on transient failures, mirroring the browser orchestrator's
# fetchOneWithRetry (DEFAULT_RETRY_ATTEMPTS / _BASE_MS / _FACTOR in
# ../costcompass/frontend/src/lib/refresh/orchestrator.ts).
RETRY_ATTEMPTS = 5
RETRY_BASE_S = 0.5
RETRY_FACTOR = 2.0
MAX_RETRY_AFTER_S = 600.0

_TRANSIENT = {
    "rate_limited",
    "upstream_unreachable",
    "upstream_timeout",
    "worker_pool_exhausted",
    "internal_error",
    "network_error",
    # Same retry disposition the statuses they replace already had.
    "broker_unreachable",
    "service_unavailable",
    "broker_timeout",
}
# Codes the broker itself can send. The three synthesized 5xx codes are
# deliberately absent: if one ever arrived in an envelope it would fall
# through to _STATUS_TO_CODE, which is where it belongs.
_KNOWN_CODES = {
    "invalid_request",
    "host_not_allowed",
    "method_not_allowed",
    "header_not_allowed",
    "unauthenticated",
    "forbidden",
    "payload_too_large",
    "rate_limited",
    "upstream_unreachable",
    "upstream_timeout",
    "internal_error",
    "worker_pool_exhausted",
}
# Guess a code for an error response that carried no envelope. Only the 4xx
# rows can name a broker code: those mean the same thing whoever wrote them.
# A 5xx cannot — reaching here means the broker did not answer, so the status
# came from a hop in between (proxy, load balancer, edge) and says nothing
# about the broker's internals or the provider.
_STATUS_TO_CODE = {
    400: "invalid_request",
    401: "unauthenticated",
    403: "forbidden",
    413: "payload_too_large",
    429: "rate_limited",
    502: "broker_unreachable",
    503: "service_unavailable",
    504: "broker_timeout",
}


def relay_status(err: BrokerError) -> int:
    """The status to relay to the App Server for a failed forward, so its
    taxonomy classifies a rate-limit/timeout/bad-request correctly instead of
    collapsing every broker failure to a generic 502. Observed status wins,
    guess only for a client-synthesized error — service-broker-spec.md §4.
    """
    if err.http_status >= 400:
        return err.http_status
    if err.code == "rate_limited":
        return 429
    if err.code == "upstream_timeout":
        return 504
    if err.code in ("upstream_unreachable", "network_error"):
        return 502
    return 500


def _is_transient_upstream_status(status: Any) -> bool:
    """A 200-from-broker envelope whose UPSTREAM status is itself transient
    (429 or 5xx) is worth retrying — mirrors the browser's
    isTransientUpstreamStatus."""
    return isinstance(status, int) and (status == 429 or 500 <= status <= 599)


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header. The broker/spec send seconds; the
    HTTP-date form is ignored (the CLI keeps the simpler delta-seconds path).
    Capped so a hostile header can't park the CLI for hours."""
    if not value:
        return None
    v = value.strip()
    if v.isdigit():
        return min(float(int(v)), MAX_RETRY_AFTER_S)
    return None


def _header_ci(headers: dict[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def provider_error_response(
    status: int, purpose: str, detail: str, *, error_code: str | None = None
) -> dict[str, Any]:
    """An UNSIGNED, NON-SYNTHETIC provider-error response the App Server's
    status taxonomy can classify (429 → rate_limited, 5xx → transient, …). No
    signature (no broker round-trip) and no ``synthetic`` flag, so it is not
    stripped before plugin.process().

    ``error_code`` embeds a machine-readable code in the body envelope. The
    App Server's shared reauth classifier keys on ``reauth_required`` in the
    body, so an OAuth mint rejection (409) must pass ``error_code`` — otherwise
    a message-only body is misfiled as a generic failure. This mirrors the
    ``{"error":{"code":…}}`` envelope the browser relays verbatim from the
    oauth-broker."""
    payload: dict[str, Any] = (
        {"error": {"code": error_code, "message": detail}}
        if error_code
        else {"error": detail}
    )
    body = base64.b64encode(json.dumps(payload).encode()).decode("ascii")
    return {
        "request_url": "cc-internal://cli-refresh",
        "request_purpose": purpose,
        "status": status,
        "headers": {"content-type": "application/json"},
        "body_b64": body,
    }


class BrokerError(Exception):
    def __init__(
        self,
        code: str,
        http_status: int,
        message: str,
        retry_after_s: float | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retry_after_s = retry_after_s
        # The broker's own correlation id for this forward, when it sent one.
        # Relayed into the failure stub's body so a user-visible error can be
        # matched to the broker log line that produced it — the browser has
        # carried this since the stub was introduced.
        self.request_id = request_id

    def is_transient(self) -> bool:
        return self.code in _TRANSIENT


class BrokerRunDeadlineError(BrokerError):
    """A retry was abandoned because its backoff did not fit the run budget.

    Not a transport failure at all — the last attempt's error is beside the
    point, which is why this carries none of it. The caller banks the request as
    a synthetic deadline stub, the same one it emits for a request the budget
    never let it issue; from the App Server's side those are one event.

    A ``BrokerError`` subclass so a caller that only knows the base class still
    handles it, but deliberately non-transient: retrying the thing that ran out
    of time is the bug.
    """

    def __init__(self, wait_s: float) -> None:
        super().__init__(
            "run_deadline_exceeded",
            504,
            f"retry backoff of {wait_s:.0f}s would exceed the run budget",
        )
        self.wait_s = wait_s


class BrokerForwardCapError(BrokerError):
    def __init__(self, cap: int, issued: int) -> None:
        super().__init__(
            "forward_cap_exceeded",
            429,
            f"entry exceeded per-entry broker forward cap (cap={cap})",
        )
        self.cap = cap
        self.issued = issued


def parse_broker_target(url: str, method: str) -> dict[str, str]:
    parts = urlsplit(url)
    # Lower-case the host: hostnames are case-insensitive, the browser's
    # new URL().host lower-cases, and the App Server signs the request canonical
    # over the lower-cased host — so the host this relay forwards must match, or
    # the broker's cc-broker-req.v1 verification fails on a body-bearing request.
    target: dict[str, str] = {
        "host": parts.netloc.lower(),
        "path": parts.path or "/",
        "method": method,
    }
    if parts.query:
        target["query"] = parts.query
    return target


def build_forward_request(
    req: dict[str, Any],
    auth_headers: dict[str, str],
    signing_token: str | None,
) -> dict[str, Any]:
    return {
        "target": parse_broker_target(req["url"], req.get("method", "GET")),
        "headers": {**(req.get("headers") or {}), **auth_headers},
        "body": req.get("body"),
        "signing_token": signing_token,
        "req_sig": req.get("req_sig"),
    }


def to_raw_response_payload(
    req: dict[str, Any], response: dict[str, Any]
) -> dict[str, Any]:
    return {
        "request_url": req["url"],
        "request_purpose": req.get("purpose", ""),
        "status": response["status"],
        "headers": response.get("headers", {}),
        "body_b64": response["body"],
        "signature": response.get("signature"),
    }


def broker_url_from_api(api_url: str) -> str:
    """Derive the broker base URL from the App Server base URL.

    `https://host/api/v1` -> `https://host/broker/v1`.
    """
    base = api_url.rstrip("/")
    base = base.removesuffix("/api/v1")
    return f"{base}/broker/v1"


class BrokerClient:
    def __init__(
        self,
        broker_url: str,
        api_key: str,
        *,
        http: httpx.Client | None = None,
        forward_cap: int = FORWARD_CAP,
        retry_attempts: int = RETRY_ATTEMPTS,
        retry_base_s: float = RETRY_BASE_S,
        retry_factor: float = RETRY_FACTOR,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.broker_url = broker_url.rstrip("/")
        self._http = http or httpx.Client(timeout=120.0)
        # These are the headers of the CLI's own request to the broker, which is
        # the only hop the relay's identity belongs on. The forwarded request's
        # headers are built separately (``build_forward_request``) and the
        # broker rejects a caller-supplied user-agent there, setting its own for
        # the provider hop.
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": user_agent(),
        }
        self._forward_cap = forward_cap
        self._entry_count: dict[str, int] = {}
        # One client is shared by every entry in a run, and entries fan out, so
        # the read-modify-write in ``_check_cap`` needs to be atomic. Keys are
        # per-entry signing tokens, so contention is nil — but "nil contention"
        # is not "atomic", and a lost increment is a cap that admits more
        # forwards than it advertises.
        self._entry_count_lock = threading.Lock()
        self._retry_attempts = max(1, retry_attempts)
        self._retry_base_s = retry_base_s
        self._retry_factor = retry_factor
        self._sleep = sleep

    def _backoff_s(self, attempt: int, retry_after_s: float | None) -> float:
        """Exponential backoff for the given attempt (1-based), floored by an
        upstream-supplied Retry-After when present (mirrors the browser)."""
        backoff = self._retry_base_s * (self._retry_factor ** (attempt - 1))
        if retry_after_s is not None:
            return max(retry_after_s, backoff)
        return backoff

    def _sleep_within_budget(
        self, seconds: float, can_wait: Callable[[float], bool] | None
    ) -> None:
        """Sleep between two attempts, unless the caller's budget can't cover it.

        The predicate is asked TWICE around one sleep, and both calls carry
        weight:

        - Before, because a wait ending at or past the deadline buys an attempt
          the caller's own seam would refuse anyway.
        - After, because a sleep duration is a floor, not a promise. The host
          can suspend mid-sleep and stop the clock, turning a wait that
          comfortably fit into one landing well past the deadline; forwarding on
          the strength of the earlier prediction is how a retry escapes the
          budget it was just checked against.

        The second call passes ``0.0``: the question is no longer "does a wait
        this long fit" but "is there any budget left at all".
        """
        if can_wait is not None and not can_wait(seconds):
            raise BrokerRunDeadlineError(seconds)
        self._sleep(seconds)
        if can_wait is not None and not can_wait(0.0):
            raise BrokerRunDeadlineError(seconds)

    def forward_with_retry(
        self,
        request: dict[str, Any],
        *,
        can_wait: Callable[[float], bool] | None = None,
    ) -> dict[str, Any]:
        """Forward with bounded retry on transient broker failures and
        transient *upstream* statuses (429/5xx), honoring ``Retry-After``.

        Mirrors the browser's ``fetchOneWithRetry``: retries carry
        ``is_retry`` so they don't count against the per-entry forward cap
        (the cap targets runaway poll loops, not transient recovery). A
        non-transient result returns immediately; the per-entry forward cap
        propagates without retry. Raises the final ``BrokerError`` once
        retries are exhausted — the caller maps it to a provider-error.

        ``can_wait(seconds)`` answers "does a sleep this long still fit inside
        the run budget?", and is consulted before every sleep — the seam the run
        budget could not otherwise reach. ``RETRY_ATTEMPTS`` sleeps of a
        ``Retry-After`` capped at ``MAX_RETRY_AFTER_S`` is ~40 minutes of
        forwards issued after the run was meant to be over, holding the refresh
        lock throughout. Asked BEFORE the sleep on purpose: waiting right up to
        the deadline and only then giving up spends the budget to learn nothing.
        Refused, this raises ``BrokerRunDeadlineError`` and the caller banks a
        deadline stub for this request alone.

        A clock is deliberately NOT injected here to compute that itself: the
        orchestrator owns the run's wall clock (see ``RUN_BUDGET_S``), and a
        second clock in this client is a second thing to keep in agreement with
        it. Default ``None`` keeps the pre-budget behaviour exactly."""
        attempt = 0
        last_error: BrokerError | None = None
        while attempt < self._retry_attempts:
            req = request if attempt == 0 else {**request, "is_retry": True}
            try:
                envelope = self.forward(req)
            except BrokerForwardCapError:
                raise
            except BrokerError as err:
                last_error = err
                if not err.is_transient() or attempt + 1 >= self._retry_attempts:
                    raise
                attempt += 1
                self._sleep_within_budget(
                    self._backoff_s(attempt, err.retry_after_s), can_wait
                )
                continue
            # 200 from the broker, but the upstream status is itself transient.
            if attempt + 1 < self._retry_attempts and _is_transient_upstream_status(
                envelope.get("status")
            ):
                attempt += 1
                retry_after = parse_retry_after(
                    _header_ci(envelope.get("headers") or {}, "retry-after")
                )
                self._sleep_within_budget(
                    self._backoff_s(attempt, retry_after), can_wait
                )
                continue
            return envelope
        # Loop only exits via return/raise above; this satisfies the type
        # checker and guards a degenerate retry_attempts.
        if last_error is not None:
            raise last_error
        return self.forward(request)

    def _check_cap(self, request: dict[str, Any]) -> None:
        token = request.get("signing_token")
        if not token or request.get("is_retry"):
            return
        with self._entry_count_lock:
            issued = self._entry_count.get(token, 0)
            if issued >= self._forward_cap:
                raise BrokerForwardCapError(self._forward_cap, issued)
            self._entry_count[token] = issued + 1

    def _build_body(self, request: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "target": request["target"],
            "headers": request["headers"],
        }
        if request.get("body") is not None:
            body["body"] = request["body"]
        token = request.get("signing_token")
        if token:
            body["signing_token"] = token
        req_sig = request.get("req_sig")
        if req_sig:
            body["req_sig"] = req_sig
        return body

    def forward(self, request: dict[str, Any]) -> dict[str, Any]:
        self._check_cap(request)
        payload = self._build_body(request)
        try:
            resp = self._http.post(
                f"{self.broker_url}/forward", headers=self._headers, json=payload
            )
        except httpx.RequestError as exc:
            raise BrokerError("network_error", 0, str(exc)) from exc

        if resp.is_success:
            return self._parse_success(resp)
        raise self._parse_error(resp)

    @staticmethod
    def _parse_success(resp: httpx.Response) -> dict[str, Any]:
        try:
            parsed = resp.json()
        except ValueError as exc:
            raise BrokerError(
                "malformed_response", resp.status_code, "non-JSON envelope"
            ) from exc
        if (
            not isinstance(parsed.get("status"), int)
            or not isinstance(parsed.get("body"), str)
            or not isinstance(parsed.get("headers"), dict)
        ):
            raise BrokerError(
                "malformed_response", resp.status_code, "envelope missing fields"
            )
        return parsed

    @staticmethod
    def _parse_error(resp: httpx.Response) -> BrokerError:
        envelope: dict[str, Any] = {}
        try:
            envelope = resp.json()
        except ValueError:
            envelope = {}
        err = envelope.get("error") or {}
        raw_code = err.get("code")
        code = (
            raw_code
            if raw_code in _KNOWN_CODES
            else _STATUS_TO_CODE.get(resp.status_code, "internal_error")
        )
        message = err.get("message") or f"broker {resp.status_code}"
        retry_after = parse_retry_after(resp.headers.get("retry-after"))
        request_id = err.get("request_id")
        return BrokerError(
            code,
            resp.status_code,
            message,
            retry_after,
            request_id if isinstance(request_id, str) else None,
        )

# LOCKSTEP: one of three relay implementations (browser is the
# reference) — semantic changes here must land in the CostCompass
# monorepo, checked out alongside this repo, at
# ../costcompass/frontend/src/lib/refresh/signers.ts and
# ../costcompass/client/macos/CostCompassKit/Sources/CostCompassKit/
#   Refresh/Signers.swift.
# See "Three relay implementations" in that repo's root CLAUDE.md;
# `make lockstep` there enumerates the whole set across both repos.

"""Request signers for a FetchPlan.

``build_plan_signer`` turns a plan into a signer that every forward calls
with the broker target and body it is about to send. When ``signed_auth``
is present the named signer owns every auth header (bearer path skipped);
otherwise the plan's ``auth_header`` carries the raw key (with an optional
``auth_scheme`` prefix).

Bearer and ``qc_hmac_v1`` don't depend on the request, so their headers
are computed once per plan. ``ovh_v1`` signs the method, URL and body, so
it signs every request — and every retry, so a retry issued minutes later
still carries a fresh timestamp.
"""

from __future__ import annotations

import base64
import hashlib
import time
from collections.abc import Callable
from typing import Any

#: ``(target, body_b64) -> headers`` for one outbound request.
RequestSigner = Callable[[dict[str, Any], "str | None"], dict[str, str]]

_OVH_CREDENTIAL_ERROR = "ovh_v1 credential must be 'application_secret:consumer_key'"


def build_auth_header_value(scheme: str | None, api_key: str) -> str:
    return f"{scheme} {api_key}" if scheme else api_key


def fixed_headers_signer(headers: dict[str, str]) -> RequestSigner:
    """A signer whose headers don't depend on the request."""
    return lambda _target, _body: headers


def _qc_hmac_v1_signer(
    signed: dict[str, Any], api_key: str, _plan_received_at_sec: int | None
) -> RequestSigner:
    # One timestamp covers the whole plan (v1's ``_headers()`` behaviour).
    return fixed_headers_signer(sign_quantconnect_hmac_v1(signed, api_key))


def _ovh_v1_signer(
    signed: dict[str, Any], api_key: str, plan_received_at_sec: int | None
) -> RequestSigner:
    # Reject a malformed credential now, before any request is built.
    _split_ovh_credential(api_key)
    received = plan_received_at_sec if plan_received_at_sec is not None else _now_sec()
    offset = int(signed["server_now"]) - received
    return lambda target, body: sign_ovh_v1(
        signed, api_key, target, body, _now_sec() + offset
    )


#: Every ``signed_auth.kind`` this relay can sign with, keyed to its builder.
#: ``build_plan_signer`` dispatches through this table, so
#: ``IMPLEMENTED_SIGNER_KINDS`` cannot drift from what actually signs — Python
#: has no compile-time exhaustiveness check like the browser's
#: ``Record<SignedAuth["kind"], true>``, so the set is derived, not listed twice.
_SIGNER_BUILDERS: dict[
    str, Callable[[dict[str, Any], str, int | None], RequestSigner]
] = {
    "qc_hmac_v1": _qc_hmac_v1_signer,
    "ovh_v1": _ovh_v1_signer,
}
IMPLEMENTED_SIGNER_KINDS: frozenset[str] = frozenset(_SIGNER_BUILDERS)


def supports_signer_kind(kind: object) -> bool:
    """Can this relay sign with ``kind``? A missing or non-string kind cannot.

    The orchestrator reads it to skip a plan whose signer this client has not
    learned BEFORE resolving any credential — the same version lag
    ``SKIP_UNSUPPORTED_KIND`` covers one level up, for credential routing."""
    return isinstance(kind, str) and kind in IMPLEMENTED_SIGNER_KINDS


def build_plan_signer(
    plan: dict[str, Any], api_key: str, plan_received_at_sec: int | None = None
) -> RequestSigner:
    """``plan_received_at_sec`` is the local unix time the plan arrived from
    the App Server; ``ovh_v1`` turns the plan's ``server_now`` into a clock
    offset against it. Defaults to now, right for a caller that signs as soon
    as it has the plan."""
    signed = plan.get("signed_auth")
    if not signed:
        return fixed_headers_signer(
            {
                plan["auth_header"]: build_auth_header_value(
                    plan.get("auth_scheme"), api_key
                )
            }
        )
    kind = signed.get("kind")
    if not supports_signer_kind(kind):
        # The orchestrator's pre-flight skips such a plan before a credential is
        # resolved; this stays as the backstop for any other caller.
        raise ValueError(f"unsupported signed_auth kind: {kind!r}")
    return _SIGNER_BUILDERS[kind](signed, api_key, plan_received_at_sec)


def _now_sec() -> int:
    return int(time.time())


def sign_quantconnect_hmac_v1(
    spec: dict[str, Any], api_token: str, now_sec: int | None = None
) -> dict[str, str]:
    """QuantConnect HMAC-v1: base64(user_id ':' sha256hex(token ':' timestamp))."""
    timestamp = str(now_sec if now_sec is not None else int(time.time()))
    hashed_hex = hashlib.sha256(f"{api_token}:{timestamp}".encode()).hexdigest()
    auth = base64.b64encode(f"{spec['user_id']}:{hashed_hex}".encode()).decode("ascii")
    auth_header = spec.get("authorization_header", "Authorization")
    scheme = spec.get("authorization_scheme", "Basic")
    ts_header = spec.get("timestamp_header", "Timestamp")
    return {auth_header: f"{scheme} {auth}", ts_header: timestamp}


def sign_ovh_v1(
    spec: dict[str, Any],
    credential: str,
    target: dict[str, Any],
    body: str | None,
    timestamp_sec: int,
) -> dict[str, str]:
    """OVHcloud: ``"$1$" + sha1hex(AS+CK+METHOD+URL+BODY+ts)``, ``+``-joined.

    ``URL`` is rebuilt from the broker target exactly as the broker rebuilds
    the upstream URL; ``BODY`` is the raw request bytes. ``credential`` is
    the vault secret ``AS:CK``, split on the first ``:``."""
    app_secret, consumer_key = _split_ovh_credential(credential)
    timestamp = str(timestamp_sec)
    query = target.get("query")
    url = f"https://{target['host']}{target['path']}" + (f"?{query}" if query else "")
    head = "+".join([app_secret, consumer_key, target["method"], url, ""]).encode()
    material = (
        head + (base64.b64decode(body) if body else b"") + f"+{timestamp}".encode()
    )
    return {
        "X-Ovh-Application": spec["application_key"],
        "X-Ovh-Consumer": consumer_key,
        "X-Ovh-Timestamp": timestamp,
        "X-Ovh-Signature": "$1$" + hashlib.sha1(material).hexdigest(),
    }


def _split_ovh_credential(credential: str) -> tuple[str, str]:
    app_secret, sep, consumer_key = credential.partition(":")
    if not sep or not app_secret or not consumer_key:
        # Never echo the credential: the message names the shape only.
        raise ValueError(_OVH_CREDENTIAL_ERROR)
    return app_secret, consumer_key

# LOCKSTEP: one of three relay implementations (browser is the
# reference) — semantic changes here must land in the CostCompass
# monorepo, checked out alongside this repo, at
# ../costcompass/frontend/src/lib/refresh/signers.ts and
# ../costcompass/cli/macos/CostCompassKit/Sources/CostCompassKit/
#   Refresh/Signers.swift.
# See "Three relay implementations" in that repo's root CLAUDE.md;
# `make lockstep` there enumerates the whole set across both repos.

"""Per-request auth-header overlay for a FetchPlan.

When ``signed_auth`` is present the named signer owns every auth
header (bearer path skipped); otherwise the plan's ``auth_header``
carries the raw key (with an optional ``auth_scheme`` prefix).
"""

from __future__ import annotations

import base64
import hashlib
import time
from typing import Any


def build_auth_header_value(scheme: str | None, api_key: str) -> str:
    return f"{scheme} {api_key}" if scheme else api_key


def build_plan_headers(plan: dict[str, Any], api_key: str, now_sec: int | None = None) -> dict[str, str]:
    signed = plan.get("signed_auth")
    if signed:
        return _sign_signed_auth(signed, api_key, now_sec)
    return {plan["auth_header"]: build_auth_header_value(plan.get("auth_scheme"), api_key)}


def _sign_signed_auth(spec: dict[str, Any], api_key: str, now_sec: int | None) -> dict[str, str]:
    kind = spec.get("kind")
    if kind == "qc_hmac_v1":
        return sign_quantconnect_hmac_v1(spec, api_key, now_sec)
    raise ValueError(f"unsupported signed_auth kind: {kind!r}")


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

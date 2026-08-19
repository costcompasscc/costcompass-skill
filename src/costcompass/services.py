"""Resolve a user-typed service name to a canonical provider.

Matching is dynamic against the provider list returned by the App
Server (`id`, `short_name`, `display_name`) — there is no hardcoded
name map, so "claude" resolves to the `anthropic` provider via its
short name without baking plugin ids into the CLI.
"""

from __future__ import annotations

from typing import Any


class ResolveError(Exception):
    """Raised when a service name matches zero or multiple providers."""


def _candidates(provider: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for field in ("id", "short_name", "display_name"):
        value = (provider.get(field) or "").strip().lower()
        if value:
            out.add(value)
            out.add(value.replace(" ", ""))
    return out


def _available(providers: list[dict[str, Any]]) -> str:
    seen: dict[str, str] = {}
    for p in providers:
        pid = p.get("id")
        if pid and pid not in seen:
            short = p.get("short_name") or p.get("display_name") or pid
            seen[pid] = short
    return ", ".join(
        f"{pid} ({short})" if short and short != pid else pid
        for pid, short in sorted(seen.items())
    )


def match(name: str, cards: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the single card matching ``name`` (id/short/display), or ``None``
    when nothing matches. Raises ``ResolveError`` only when the name is
    ambiguous (matches more than one distinct ``id``). Works for both the
    ``/providers`` list and the breakdown's subscription cards, as long as each
    card carries an ``id``."""
    key = name.strip().lower()
    matches = [c for c in cards if key in _candidates(c)]
    if not matches:
        return None
    ids = {m.get("id") for m in matches}
    if len(ids) > 1:
        joined = ", ".join(sorted(str(i) for i in ids))
        raise ResolveError(f"Service '{name}' is ambiguous — matches {joined}.")
    enabled = [m for m in matches if m.get("enabled")]
    return (enabled or matches)[0]


def resolve(name: str, providers: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the provider dict matching ``name``; raise if none match. Used
    where only a metered provider is valid (e.g. refresh)."""
    found = match(name, providers)
    if found is None:
        raise ResolveError(
            f"Unknown service '{name}'. Available services: {_available(providers)}"
        )
    return found


def available_names(
    providers: list[dict[str, Any]], subscriptions: list[dict[str, Any]]
) -> str:
    """Human list of every addressable name — metered providers plus any
    standalone subscription cards — for an 'unknown service' error."""
    parts = [_available(providers)]
    subs = sorted(
        {
            (s.get("display_name") or "").strip()
            for s in subscriptions
            if (s.get("display_name") or "").strip()
        }
    )
    if subs:
        parts.append("subscriptions: " + ", ".join(subs))
    return "; ".join(parts)

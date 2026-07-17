"""Plain-text rendering for the CLI (no rich dependency).

Functions return strings so they are trivially unit-testable; the
command layer is responsible for printing them.
"""

from __future__ import annotations

import re
from typing import Any

# C0 + C1 control characters (includes ESC 0x1b, which starts every ANSI/CSI/OSC
# sequence). Field values rendered here are single-line, so we drop control
# characters outright. Provider/model/display strings originate from upstream API
# responses — without this an attacker-controlled name could smuggle an escape
# sequence that manipulates the user's terminal (cursor, title, OSC 8 hyperlinks,
# OSC 52 clipboard). ``--json`` output is unaffected (JSON-encoded).
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def safe_text(value: Any) -> str:
    """Strip terminal control sequences from externally-sourced text."""
    return _CONTROL_CHARS.sub("", str(value))


# Mirror of ../costcompass/backend/app/plugins/_base/surfaces.py
# (label, order). The
# /dashboard/breakdown payload carries the surface KEY but not its label or
# order, so this small table is synced by hand — keep it in step with the
# canonical SURFACES map there. Unknown surfaces sort last.
_SURFACES: dict[str, tuple[str, int]] = {
    "ai_usage": ("Models", 10),
    "ai_subscription": ("Subscription", 20),
    "service_usage": ("Products", 30),
    "subscription": ("Plan", 40),
    "cloud_infra": ("Services", 50),
}
_UNKNOWN_SURFACE_ORDER = 999


def money(value: float) -> str:
    return f"${value:,.2f}"


def format_amount(summary: dict[str, Any]) -> str:
    """The headline 'big number' for the portfolio or one service."""
    return money(summary.get("mtd_usd", 0.0))


def format_subscription(display_name: str, cost: float) -> str:
    """A standalone subscription card has no metered usage — just a flat fee,
    so there is no burn/forecast/per-model detail to show."""
    return (
        f"{safe_text(display_name)} — {money(cost)} month-to-date\n"
        f"  (subscription — flat fee, no metered usage)"
    )


def format_breakdown(cards: list[dict[str, Any]]) -> str:
    """Every card (metered providers AND standalone subscriptions) ranked by
    cost, with a reconciling total. ``cards`` is the /dashboard/breakdown
    payload, where a folded plan fee already sits inside its provider's
    ``cost_usd`` and a standalone subscription is its own row."""
    rows = sorted(cards, key=lambda c: -(c.get("cost_usd") or 0.0))
    total = sum(c.get("cost_usd") or 0.0 for c in cards)
    width = max(
        [len(money(c.get("cost_usd") or 0.0)) for c in rows] + [len(money(total))]
    )
    lines: list[str] = []
    for c in rows:
        amount = money(c.get("cost_usd") or 0.0)
        name = safe_text(c.get("display_name") or c.get("provider_id") or "")
        kind = c.get("kind") or "provider"
        tag = "" if kind == "provider" else f"  ({safe_text(kind)})"
        lines.append(f"  {amount:>{width}}  {name}{tag}")
    lines.append(f"  {'-' * width}")
    lines.append(f"  {money(total):>{width}}  Total")
    return "\n".join(lines)


def _model_value(row: dict[str, Any]) -> str:
    """Cost for a model row, or its display_value when unpriced."""
    cost = row.get("cost_usd") or 0.0
    if cost == 0 and row.get("display_value"):
        return safe_text(row["display_value"])
    return money(cost)


def _surface_sort_key(surface: str | None) -> int:
    entry = _SURFACES.get(surface or "")
    return entry[1] if entry else _UNKNOWN_SURFACE_ORDER


def format_details(
    display_name: str,
    summary: dict[str, Any],
    models: list[dict[str, Any]],
) -> str:
    """Headline metrics + per-model breakout grouped by surface."""
    lines: list[str] = [
        f"{safe_text(display_name)} — {money(summary.get('mtd_usd', 0.0))} month-to-date",
        f"  7-day daily burn : {money(summary.get('burn_rate_7day', 0.0))}",
        f"  forecast (next)  : {money(summary.get('forecast_usd', 0.0))}",
        f"  days remaining   : {summary.get('days_remaining', 0)}",
        f"  previous month   : {money(summary.get('previous_month_usd', 0.0))}",
    ]
    if summary.get("mtd_as_of"):
        lines.append(f"  data as of       : {safe_text(summary['mtd_as_of'])}")

    if not models:
        lines.append("")
        lines.append("  (no per-model breakdown)")
        return "\n".join(lines)

    ordered = sorted(
        models,
        key=lambda r: (
            _surface_sort_key(r.get("surface")),
            -(r.get("cost_usd") or 0.0),
        ),
    )
    lines.append("")
    current_surface: str | None = "__unset__"
    for row in ordered:
        surface = row.get("surface")
        if surface != current_surface:
            current_surface = surface
            if surface:
                label = _SURFACES.get(surface, (surface, 0))[0]
                lines.append(f"  {safe_text(label)}:")
        indent = "    " if current_surface else "  "
        name = safe_text(row.get("display_name") or row.get("model", ""))
        lines.append(f"{indent}{name:<32} {_model_value(row)}")
    return "\n".join(lines)

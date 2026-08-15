"""Plain-text rendering for the CLI (no rich dependency).

Functions return strings so they are trivially unit-testable; the
command layer is responsible for printing them.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
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

# Refresh is a MODE of `mtd`, not its own command, and --vault is mandatory
# there — naming anything else would print an instruction that errors out.
_REFRESH_COMMAND = "costcompass mtd refresh --vault"


#: Quantum for every money figure the CLI prints.
_CENT = Decimal("0.01")


def money(value: float) -> str:
    """Money for display, rounded to the nearest cent.

    One of four implementations of a single rule — the web page
    (``frontend/src/lib/format.ts``), the server-rendered PDF
    (``backend/app/services/report_service.py``), the macOS menu bar
    (``Formatters.formatUSDDisplay``) and this. A user running
    ``costcompass mtd`` beside the dashboard is looking at two renderings of
    one number, so any disagreement reads as a bug in the data.

    Two details are load-bearing for that agreement, both about matching
    JavaScript's ``toLocaleString`` on the web side:

    * ``Decimal(str(value))`` rather than a bare ``f"{value:,.2f}"``. The
      format spec rounds half-to-EVEN on the binary double, so it renders
      ``$1.00`` for ``1.005`` and ``$2.67`` for ``2.675`` where every other
      surface says ``$1.01`` and ``$2.68``.
    * ``ROUND_HALF_UP``, ties away from zero, which is the rule the other
      three use.

    A positive amount that rounds to nothing reads "< $0.01" rather than
    "$0.00" — printing a charge as no charge is the reading this exists to
    prevent, and the sub-cent per-model rows on a metered card are exactly
    where it bites. A negative that rounds to nothing drops its sign, and the
    sign otherwise sits outside the "$", so a credit reads "-$1.50" and never
    the "$-0.00" the old format spec could produce.

    ASSUMPTION: the shared case table lives in
    ``frontend/src/lib/money-display-cases.ts`` in the main repo, and
    ``test_render.py`` carries a copy by hand. This CLI ships from its own
    repo on its own cadence, so nothing mechanically pins the two — if that
    table moves and this copy is not carried over, the suites stay green and
    the CLI silently drifts from the dashboard again.
    """
    magnitude = Decimal(str(value)).copy_abs().quantize(_CENT, rounding=ROUND_HALF_UP)
    if magnitude == 0:
        return "< $0.01" if value > 0 else "$0.00"
    return f"{'-' if value < 0 else ''}${magnitude:,.2f}"


def incomplete_window_note(summary: dict[str, Any]) -> str | None:
    """Caveat for a total that is a floor rather than a final figure, or None.

    A card can fetch successfully and still miss part of the month — one
    sub-request fails at the broker while its siblings ingest. The server counts
    those cards per scope; without this the CLI would print an under-reported
    number as if it were settled.

    Deliberately no count of missing days: the server's per-card verdict is
    exact, but a day whose response never arrived leaves no record at all, so
    any day count would be a lower bound printed as a fact. It also has to read
    sensibly for providers with no per-day notion at all.
    """
    count = summary.get("incomplete_card_count") or 0
    if count <= 0:
        return None
    subject = "1 service hasn't" if count == 1 else f"{count} services haven't"
    return f"({subject} finished loading this month's data yet — this total may be low)"


def staleness_note(summary: dict[str, Any]) -> str | None:
    """Which services are missing from these figures, or None when none are.

    Same shape and purpose as ``incomplete_window_note`` above: a caveat string
    or nothing. This one answers "which service is out of date", which the CLI
    cannot otherwise tell a user — nothing here refreshes on its own, so a
    figure can be months stale and still print as if it were today's.

    Every judgement is the server's: which cards are behind, how far, and
    whether Refresh can even fix them (``stale_cards`` on the MTD summary). The
    threshold arithmetic that used to live here is gone with it — the dashboard
    and this renderer each stated the same three days, and a rule written twice
    in two languages only stays honest for as long as someone keeps mirroring
    the tests.

    **Every card is listed, never truncated.** The dashboard trims to three
    names because it renders beside a figure in a fixed row; this output is read
    by a person or by the skill's model, either of which can take the whole list
    and neither of which is served by a hidden remainder. What this must NOT do
    is re-decide *whether* a card is a problem — that judgement is the server's,
    or the two surfaces drift apart again.
    """
    if (summary.get("enabled_provider_count") or 0) <= 0:
        # No enabled card in scope, or a server too old to say. Nothing here
        # refreshes, so a nudge to run refresh would be a lie.
        return None
    cards = summary.get("stale_cards") or []
    if not cards:
        # Every card current, or a server predating the field — both silent.
        return None
    listed = ", ".join(_stale_card_label(card) for card in cards)
    # Blocked is resolved BEFORE never-fetched, because the two overlap: a card
    # whose very first fetch failed on a bad credential has no age AND cannot be
    # collected, so the first-refresh nudge below would contradict the server
    # that just said refreshing cannot succeed. Only when EVERY card is blocked
    # is refreshing the wrong advice — with one collectable card the command
    # still helps, and the blocked card is already marked in the list.
    if all(card.get("blocked") for card in cards):
        return f"Not updated: {listed}. These can't connect — check their credentials."
    if all(card.get("days") is None for card in cards):
        return f"No usage fetched yet. Run '{_REFRESH_COMMAND}' to pull your data."
    return f"Not updated: {listed}. Run '{_REFRESH_COMMAND}' for the latest numbers."


def _stale_card_label(card: dict[str, Any]) -> str:
    """``Anthropic (40 days)``, or ``AWS (never fetched, can't connect)``.

    Spelled out rather than abbreviated: this line is read in a terminal or by a
    model, neither of which is short of room, and "40d" invites a misread.
    ``days`` of None means the card has never fetched — distinct from 0, which
    would claim it fetched today.
    """
    days = card.get("days")
    facts = ["never fetched" if days is None else f"{days} days"]
    if card.get("blocked"):
        facts.append("can't connect")
    name = safe_text(card.get("display_name") or card.get("provider_id") or "")
    return f"{name} ({', '.join(facts)})"


def format_amount(summary: dict[str, Any]) -> str:
    """The headline 'big number' for the portfolio or one service.

    Carries the incomplete-window caveat on a second line when there is one:
    this is the whole output of ``costcompass mtd``, so a figure printed bare
    reads as settled even when the server knows part of the month is missing.
    """
    amount = money(summary.get("mtd_usd", 0.0))
    note = incomplete_window_note(summary)
    return f"{amount}\n{note}" if note else amount


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
    if summary.get("newest_fetched_at"):
        lines.append(f"  data as of       : {safe_text(summary['newest_fetched_at'])}")
    note = incomplete_window_note(summary)
    if note:
        lines.append(f"  {note}")

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

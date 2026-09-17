"""Plain-text rendering for the CLI (no rich dependency).

Functions return strings so they are trivially unit-testable; the
command layer is responsible for printing them.
"""

from __future__ import annotations

import math
import re
from typing import Any

from . import currency_totals as ct
from .api import ApiError, required_totals
from .currency_format import format_money, known_locales

#: The rendering a reader gets before they have chosen a locale (design §6.2).
#: The page, the PDF and the macOS app all resolve the same way.
DEFAULT_LOCALE = "en-US"

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


def money(value: float, currency: str = "USD", locale: str = DEFAULT_LOCALE) -> str:
    """Money for display, spelled the way the web page and the PDF spell it.

    One of four renderers of a single rule — the web page
    (``frontend/src/lib/format.ts``), the server-rendered PDF
    (``backend/app/core/currency_format.py``), the macOS menu bar
    (``Formatters.moneyDisplay``) and this. A user running ``costcompass mtd``
    beside the dashboard is looking at two renderings of one number, so any
    disagreement reads as a bug in the data.

    This is not a second implementation of the rule: it reads the same probed
    ICU table the PDF reads (``currency_format_data.json``, copied from
    ``backend/app/core/``) and is asserted against the same shared corpus
    (``test-vectors/currency-format/cases.json``). Rounding, the currency's own
    minor unit, digit shapes, separators, the currency space and the sign
    placement all arrive as data, so there is nothing here to drift.

    A positive amount that rounds to nothing reads ``< <minor unit>`` rather
    than ``$0.00`` — printing a charge as no charge is the reading this exists
    to prevent, and the sub-cent per-model rows on a metered card are exactly
    where it bites. The default locale keeps the historical USD/en-US output
    byte-identical.

    The locale is resolved here, the one choke point every money figure passes
    through: a locale the vendored table does not carry falls back to the
    default rather than raising. The CLI ships on its own cadence, so a server
    can add a supported locale before this build is re-vendored, and an
    uncaught `ValueError` there reaches the user as a traceback.
    """
    if locale not in known_locales():
        locale = DEFAULT_LOCALE
    return format_money(value, currency, locale)


def locale_of(preferences: dict[str, Any] | None) -> str:
    """The reader's display locale, or the product default when unset — the
    same resolution the web page, the PDF and the macOS app use. An
    unsupported locale resolves to the default (`money` also guards, since it
    is the choke point every figure passes through).
    """
    locale = (preferences or {}).get("display_locale") or DEFAULT_LOCALE
    return locale if locale in known_locales() else DEFAULT_LOCALE


def primary_currency_of(preferences: dict[str, Any] | None) -> str | None:
    """The currency that leads (§4.2), or None when the user never set one."""
    return (preferences or {}).get("primary_currency")


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


def format_amount(
    summary: dict[str, Any],
    *,
    locale: str = DEFAULT_LOCALE,
    primary_currency: str | None = None,
) -> str:
    """The headline 'big number' for the portfolio or one service.

    One line per currency, in §4.2 order, and never a figure summed across them
    (design §4.4). A single-currency account reads exactly as it always did.

    Carries the incomplete-window caveat on a second line when there is one:
    this is the whole output of ``costcompass mtd``, so a figure printed bare
    reads as settled even when the server knows part of the month is missing.
    """
    totals = required_totals(summary, "mtd")
    codes = ct.ordered(totals, primary_currency) or [primary_currency or "USD"]
    body = "\n".join(money(ct.amount(totals, code), code, locale) for code in codes)
    note = incomplete_window_note(summary)
    return f"{body}\n{note}" if note else body


def format_subscription(
    display_name: str,
    totals: dict[str, Any],
    *,
    locale: str = DEFAULT_LOCALE,
    primary_currency: str | None = None,
) -> str:
    """A standalone subscription card has no metered usage — just a flat fee,
    so there is no burn/forecast/per-model detail to show. One line per
    currency; a plan billed in LKR must not print as a dollar figure."""
    codes = ct.ordered(totals, primary_currency) or [primary_currency or "USD"]
    figures = " + ".join(money(ct.amount(totals, code), code, locale) for code in codes)
    return (
        f"{safe_text(display_name)} — {figures} month-to-date\n"
        f"  (subscription — flat fee, no metered usage)"
    )


def format_breakdown(
    cards: list[dict[str, Any]],
    *,
    locale: str = DEFAULT_LOCALE,
    primary_currency: str | None = None,
) -> str:
    """Every card (metered providers AND standalone subscriptions) ranked by
    cost, with a reconciling total.

    ``cards`` is the /dashboard/breakdown payload, whose rows carry ``totals`` —
    a map, because one card can hold money in two currencies (§7.2). One block
    per currency in §4.2 order: the amounts in a block are all one denominator,
    so the column and its total are the same-currency sums they always were. A
    single-currency account gets one unlabelled block, byte-identical to the
    output before this change.
    """
    currencies = ct.ordered_union(
        [c.get("totals") for c in cards], primary_currency
    ) or [primary_currency or "USD"]
    blocks = [
        _breakdown_block(cards, currency, locale, labelled=len(currencies) > 1)
        for currency in currencies
    ]
    return "\n\n".join(blocks)


def _breakdown_block(
    cards: list[dict[str, Any]], currency: str, locale: str, *, labelled: bool
) -> str:
    # Validate every card's map even for a currency it has none of: a malformed
    # row is an incompatible response, not a card worth $0.
    totals = [required_totals(c, "totals") for c in cards]
    rows = sorted(zip(cards, totals), key=lambda pair: -ct.amount(pair[1], currency))
    total = sum(ct.amount(t, currency) for t in totals)
    width = max(
        [len(money(ct.amount(t, currency), currency, locale)) for _, t in rows]
        + [len(money(total, currency, locale))]
        or [0]
    )
    lines: list[str] = []
    if labelled:
        lines.append(f"{currency}:")
    for c, card_totals in rows:
        amount = money(ct.amount(card_totals, currency), currency, locale)
        name = safe_text(c.get("display_name") or c.get("provider_id") or "")
        kind = c.get("kind") or "provider"
        tag = "" if kind == "provider" else f"  ({safe_text(kind)})"
        lines.append(f"  {amount:>{width}}  {name}{tag}")
    lines.append(f"  {'-' * width}")
    lines.append(f"  {money(total, currency, locale):>{width}}  Total")
    return "\n".join(lines)


def _row_amount(row: dict[str, Any]) -> float:
    """A row's amount for SORTING only; validation happens in `_model_value`."""
    value = row.get("amount")
    return float(value) if type(value) in (int, float) else 0.0


def _missing_model_field(field: str) -> ApiError:
    return ApiError(
        f"Incompatible API response: required money field '{field}' is missing "
        "or invalid. Update the CostCompass CLI/plugin and try again."
    )


def _model_currency(row: dict[str, Any]) -> str:
    """A model row's denomination, required — one row is one currency (§5).

    ``ModelBreakdownOut`` is ``amount`` + ``currency``, so a row that names no
    currency is an incompatible response rather than a dollar figure. It fails
    the way a missing ``amount`` does: an ``ApiError`` the command layer prints.
    Defaulting to USD instead would relabel foreign spend as dollars, which is
    the failure this migration exists to prevent — and it is what the grouping
    and filtering below used to do before this guard rejected the row.
    """
    currency = row.get("currency")
    if not isinstance(currency, str) or not currency:
        raise _missing_model_field("currency")
    return currency


def _model_value(row: dict[str, Any], locale: str, currency: str) -> str:
    """Cost for a model row in *currency*, or its display_value when unpriced.

    A row that names another currency is a rendering bug, not a zero: the caller
    groups through ``_model_currency``, so ``_model_lines`` only ever passes
    matching rows.
    """
    if row.get("currency") != currency:
        raise ValueError(
            f"model row for {row.get('currency')!r} rendered as {currency!r}"
        )
    value = row.get("amount")
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise _missing_model_field("amount")
    cost = float(value)
    if cost == 0 and row.get("display_value"):
        return safe_text(row["display_value"])
    return money(cost, currency, locale)


def _surface_sort_key(surface: str | None) -> int:
    entry = _SURFACES.get(surface or "")
    return entry[1] if entry else _UNKNOWN_SURFACE_ORDER


def format_details(
    display_name: str,
    summary: dict[str, Any],
    models: list[dict[str, Any]],
    *,
    locale: str = DEFAULT_LOCALE,
    primary_currency: str | None = None,
) -> str:
    """Headline metrics + per-model breakout grouped by currency, then surface.

    Every money metric is a map in the new contract, so a card holding money in
    two currencies reports both rather than folding one into the other (§4.4).
    Rows carry their own denomination and are grouped by it (§7.3): the amounts
    inside a group share a currency, so ordering and summing them is meaningful.
    """
    validated = {
        field: required_totals(summary, field)
        for field in ("mtd", "burn_rate_7day", "forecast", "previous_month")
    }
    summary_currencies = ct.ordered_union(
        [
            validated["mtd"],
            validated["burn_rate_7day"],
            validated["forecast"],
            validated["previous_month"],
        ],
        primary_currency,
    ) or [primary_currency or "USD"]

    lines: list[str] = []
    for index, currency in enumerate(summary_currencies):
        if index:
            lines.append("")
        lines.extend(
            _details_headline(display_name, summary, validated, currency, locale)
        )

    model_currencies = ct.ordered_union(
        [{_model_currency(row): row.get("amount")} for row in models],
        primary_currency,
    )
    if not models:
        lines.append("")
        lines.append("  (no per-model breakdown)")
        return "\n".join(lines)

    for index, currency in enumerate(model_currencies):
        if index or summary_currencies:
            lines.append("")
        if len(model_currencies) > 1:
            lines.append(f"  {currency}:")
        lines.extend(_model_lines(models, currency, locale, indent_extra="  "))
    return "\n".join(lines)


def _details_headline(
    display_name: str,
    summary: dict[str, Any],
    metrics: dict[str, dict[str, float]],
    currency: str,
    locale: str,
) -> list[str]:
    lines = [
        f"{safe_text(display_name)} — "
        f"{money(ct.amount(metrics['mtd'], currency), currency, locale)} month-to-date",
        f"  7-day daily burn : "
        f"{money(ct.amount(metrics['burn_rate_7day'], currency), currency, locale)}",
        f"  forecast (next)  : "
        f"{money(ct.amount(metrics['forecast'], currency), currency, locale)}",
        f"  days remaining   : {summary.get('days_remaining', 0)}",
        f"  previous month   : "
        f"{money(ct.amount(metrics['previous_month'], currency), currency, locale)}",
    ]
    if summary.get("newest_fetched_at"):
        lines.append(f"  data as of       : {safe_text(summary['newest_fetched_at'])}")
    note = incomplete_window_note(summary)
    if note:
        lines.append(f"  {note}")
    return lines


def _model_lines(
    models: list[dict[str, Any]], currency: str, locale: str, *, indent_extra: str = ""
) -> list[str]:
    ordered = sorted(
        (row for row in models if _model_currency(row) == currency),
        key=lambda r: (
            _surface_sort_key(r.get("surface")),
            -_row_amount(r),
        ),
    )
    lines: list[str] = []
    current_surface: str | None = "__unset__"
    for row in ordered:
        surface = row.get("surface")
        if surface != current_surface:
            current_surface = surface
            if surface:
                label = _SURFACES.get(surface, (surface, 0))[0]
                lines.append(f"{indent_extra}{safe_text(label)}:")
        indent = f"  {indent_extra}" if current_surface else indent_extra
        name = safe_text(row.get("display_name") or row.get("model", ""))
        lines.append(f"{indent}{name:<32} {_model_value(row, locale, currency)}")
    return lines

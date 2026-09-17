"""Money keyed by ISO 4217 code, and the one rule for ordering it.

A port of ``frontend/src/lib/currency-totals.ts``. Money in two currencies has
no natural order — ``LKR 2,900`` cannot be ranked against ``$5.32`` — so ordering
is by the user's ``primary_currency`` first, then alphabetically by ISO code
(design §4.2). The dashboard, the report and this CLI must not disagree about it.
"""

from __future__ import annotations

from functools import cmp_to_key
from typing import Any, Iterable, Mapping

#: One denomination's amounts, keyed by ISO 4217 code.
CurrencyTotals = dict[str, float]


def codes(totals: Mapping[str, Any] | None) -> list[str]:
    """The currencies present, alphabetical. All keys, including an explicit
    zero, exactly as ``orderedCurrencies`` reads ``Object.keys`` on the web."""
    return sorted((totals or {}).keys())


def amount(totals: Mapping[str, Any] | None, currency: str) -> float:
    """*currency*'s amount, ``0.0`` when the map holds none."""
    return float((totals or {}).get(currency, 0.0))


def ordered(totals: Mapping[str, Any] | None, primary: str | None) -> list[str]:
    """§4.2 order: the primary currency first when it is present, then the rest
    alphabetically. A primary the user holds no money in does not lead — there
    is nothing to put at the front."""
    present = codes(totals)
    if not primary or primary not in present:
        return present
    return [primary, *[code for code in present if code != primary]]


def ordered_union(
    totals_list: Iterable[Mapping[str, Any] | None], primary: str | None
) -> list[str]:
    """Every currency any input mentions, once, in §4.2 order."""
    union: dict[str, float] = {}
    for totals in totals_list:
        for code in totals or {}:
            union[code] = 0.0
    return ordered(union, primary)


def merge(left: Mapping[str, Any], right: Mapping[str, Any]) -> CurrencyTotals:
    """Add two maps, keeping every denomination separate. Never a cross-currency
    sum (design §4.4)."""
    merged = {code: float(value) for code, value in left.items()}
    for code, value in right.items():
        merged[code] = merged.get(code, 0.0) + float(value)
    return merged


def compare(
    left: dict[str, float], right: dict[str, float], primary: str | None
) -> int:
    """Compare two maps without comparing unlike amounts, mirroring
    ``compareCurrencyTotals``: cards holding the primary currency come first,
    then by primary-currency spend; then by the set of codes; then by each
    remaining denomination's amount. An empty map sorts last.
    """
    if primary:
        left_has = primary in left
        right_has = primary in right
        if left_has != right_has:
            return -1 if left_has else 1
        if left_has and right_has:
            delta = right.get(primary, 0.0) - left.get(primary, 0.0)
            if delta != 0:
                return -1 if delta < 0 else 1
    left_codes = sorted(left)
    right_codes = sorted(right)
    if not left_codes or not right_codes:
        if len(left_codes) != len(right_codes):
            return 1 if not left_codes else -1
        return 0
    left_signature = "\0".join(left_codes)
    right_signature = "\0".join(right_codes)
    if left_signature != right_signature:
        return -1 if left_signature < right_signature else 1
    for code in left_codes:
        if code == primary:
            continue
        delta = right.get(code, 0.0) - left.get(code, 0.0)
        if delta != 0:
            return -1 if delta < 0 else 1
    return 0


def comparator(primary: str | None):
    """A ``sorted(key=…)`` built from :func:`compare`, with *primary* bound."""

    def compare_with_primary(left: dict[str, float], right: dict[str, float]) -> int:
        return compare(left, right, primary)

    return cmp_to_key(compare_with_primary)

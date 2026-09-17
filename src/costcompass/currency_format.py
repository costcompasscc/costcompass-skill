"""Money formatting for the server-side renderers, driven by probed ICU data.

The web page formats money with ``Intl.NumberFormat``
(``frontend/src/lib/format.ts``). The PDF is rendered here, and
``doc/design/multi-currency.md`` §6.2 requires the two to print the same
figure the same way: a page and the PDF of the same month must not look like
two documents, because people keep PDFs.

Python has no ICU, so this module reads ``currency_format_data.json`` — the
locale rules and per-currency affixes *probed from the engine the page runs on*
by ``scripts/gen-currency-format-data.mjs`` — and decides nothing itself. That
is the whole design: the formatter executes a table instead of re-deriving a
specification.

Why not a CLDR library (Babel): it is a different specification, not the same
one. Measured against this runtime's ICU it disagreed on 16 of the 53 supported
locales — ``ZAR1,234.50`` where the page says ``ZAR 1,234.50`` (CLDR
currencySpacing, which Babel does not implement), ``1.234,50`` where the page
says ``1234,50`` (minimumGroupingDigits, absent from its data), Arabic-Indic and
Bengali digit shapes it cannot produce at all, and separators that follow its
own bundled CLDR rather than the browser's. See
``test-vectors/currency-format/README.md`` for the measurement and the policy
for the table.

What is deliberately NOT reimplemented here: digit shapes, separators, grouping
sizes, symbol placement, the U+00A0 currency space, bidi marks, and the
currencies whose minus trails the code. All of that arrives as data, so there is
no second implementation to drift. What this module owns is the arithmetic the
frontend owns — rounding and the tiny-spend floor — because those are decisions,
not locale data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import lru_cache
from math import isfinite
from pathlib import Path
from typing import Any

#: The rendering a user gets before they have chosen a locale (§6.2). The page
#: resolves the same way (``frontend/src/components/DisplayLocaleProvider.tsx``),
#: so an account that never set one renders identically on both surfaces.
DEFAULT_LOCALE = "en-US"

#: Assumed fraction digits for a well-formed code ICU does not know. Matches
#: ICU's own default for an unlisted currency, and the two agree by
#: construction: the frontend asks ICU, and this is that answer.
DEFAULT_FRACTION_DIGITS = 2

_DATA = Path(__file__).with_name("currency_format_data.json")


@dataclass(frozen=True, slots=True)
class _Affixes:
    """Text before and after the number, per sign.

    ``("", "\\u00a0€")`` for de-DE; ``("$-", "")`` for the de-CH shape whose
    minus trails the symbol. Both halves arrive from the probe, so nothing here
    decides where a minus goes.
    """

    pos: tuple[str, str]
    neg: tuple[str, str]

    def for_sign(self, positive: bool) -> tuple[str, str]:
        return self.pos if positive else self.neg


@dataclass(frozen=True, slots=True)
class _LocaleRules:
    """One locale's numerals, separators and grouping, as ICU renders them."""

    #: Ten glyphs, index 0 first: ``"0123456789"`` or ``"٠١٢٣٤٥٦٧٨٩"``.
    digits: str
    decimal: str
    group: str
    #: Primary then secondary group size; ``(3, 2)`` is the lakh/crore pattern.
    grouping: tuple[int, int]
    minimum_grouping_digits: int
    #: Affixes for a currency with no symbol in this locale; ``{code}`` is
    #: replaced with the ISO code.
    code_affix: _Affixes
    symbols: dict[str, _Affixes]


@dataclass(frozen=True, slots=True)
class _Table:
    locales: dict[str, _LocaleRules]
    currency_digits: dict[str, int]


def _affixes(raw: dict[str, Any]) -> _Affixes:
    return _Affixes(pos=tuple(raw["pos"]), neg=tuple(raw["neg"]))


def _grouping(raw: Any) -> tuple[int, int]:
    """The primary and secondary group sizes.

    Unpacked HERE rather than in the renderer so a reshaped array fails as the
    file-naming parse error below. ``_group`` used to do this unpacking, outside
    the guard, so a ``grouping`` array with the wrong number of entries raised a
    bare ``ValueError: not enough values to unpack`` from inside a rendering
    pass — no file name, no hint that the artifact was the cause, which is the
    failure the error below exists to replace.
    """
    primary, secondary = raw
    return int(primary), int(secondary)


def _parse(raw: dict[str, Any]) -> _Table:
    """Parse the probed artifact into types, once.

    The file is generated, so this is not defending against hand-edited drift —
    it is making the access below typed, and turning a reshaped or truncated
    artifact into one loud error naming the file instead of a ``KeyError``
    raised from inside a rendering pass.
    """
    try:
        locales = {
            code: _LocaleRules(
                digits=entry["digits"],
                decimal=entry["decimal"],
                group=entry["group"],
                grouping=_grouping(entry["grouping"]),
                minimum_grouping_digits=entry["minimumGroupingDigits"],
                code_affix=_affixes(entry["codeAffix"]),
                symbols={cur: _affixes(sym) for cur, sym in entry["symbols"].items()},
            )
            for code, entry in raw["locales"].items()
        }
        return _Table(
            locales=locales,
            currency_digits=dict(raw["currencyDigits"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            f"{_DATA.name} is not the shape this module reads ({error!r}); it is "
            f"generated by scripts/gen-currency-format-data.mjs — regenerate it "
            f"rather than editing it"
        ) from error


@lru_cache(maxsize=1)
def _table() -> _Table:
    """The probed table, parsed once.

    Lazy rather than module-level so importing this module costs nothing until
    something formats money, and cached rather than re-read so the PDF does not
    reparse it per figure.
    """
    raw = json.loads(_DATA.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):  # pragma: no cover — generated shape
        raise ValueError(f"{_DATA.name} must contain a JSON object")
    return _parse(raw)


def known_locales() -> frozenset[str]:
    """The locales this table can spell. Callers that read a locale from a
    server should confirm it is here before formatting: the table is copied
    into clients that ship on their own cadence, so a server can name a locale
    an older client does not carry."""
    return frozenset(_table().locales)


def _rules(locale: str | None) -> _LocaleRules:
    rules = _table().locales.get(locale or DEFAULT_LOCALE)
    if rules is None:
        raise ValueError(
            f"no currency format rules for locale {locale!r}; display_locale is "
            f"validated against the probed locale set before it is stored, so "
            f"this means a caller passed one that was never validated"
        )
    return rules


def _fraction_digits(currency: str) -> int:
    return _table().currency_digits.get(currency, DEFAULT_FRACTION_DIGITS)


def _affixes_for(currency: str, rules: _LocaleRules, positive: bool) -> tuple[str, str]:
    """The text around the number, symbol resolved.

    A currency ICU has a symbol for in this locale was captured with it; every
    other code renders through the locale's code template, so an unlisted
    currency still lands where that locale puts a currency and keeps its
    spacing rather than falling back to a US shape.
    """
    symbol = rules.symbols.get(currency)
    if symbol is not None:
        return symbol.for_sign(positive)
    head, tail = rules.code_affix.for_sign(positive)
    return (
        head.replace("{code}", currency),
        tail.replace("{code}", currency),
    )


def _group(
    integer: str, sizes: tuple[int, int], minimum_grouping: int, separator: str
) -> str:
    """*integer* with this locale's group separators inserted.

    ``minimum_grouping`` is why ``1234`` prints ungrouped in ``es-ES`` while
    ``12345`` does not: a group appears only when at least that many digits
    precede it. ICU's own value for this differs between the currency and plain
    decimal patterns, which is why the table probes it in the currency style.
    """
    primary, secondary = sizes
    if len(integer) - primary < minimum_grouping:
        return integer
    head, tail = integer[:-primary], integer[-primary:]
    chunks: list[str] = []
    while len(head) > secondary:
        chunks.insert(0, head[-secondary:])
        head = head[:-secondary]
    if head:
        chunks.insert(0, head)
    return separator.join([*chunks, tail])


def _localize(text: str, digits: str) -> str:
    """ASCII digits to the locale's own numeral shapes, one glyph per value."""
    return text.translate(
        {ord(str(value)): glyph for value, glyph in enumerate(digits)}
    )


def _render(amount: Decimal, currency: str, locale: str | None) -> str:
    rules = _rules(locale)
    integer, _, fraction = f"{abs(amount):.{_fraction_digits(currency)}f}".partition(
        "."
    )
    number = _localize(
        _group(integer, rules.grouping, rules.minimum_grouping_digits, rules.group),
        rules.digits,
    )
    if fraction:
        number += rules.decimal + _localize(fraction, rules.digits)
    prefix, suffix = _affixes_for(currency, rules, amount >= 0)
    return prefix + number + suffix


def format_money(
    amount: float,
    currency: str,
    locale: str | None = None,
    *,
    show_tiny: bool = True,
) -> str:
    """*amount* in *currency*, spelled the way the web page spells it.

    The two rules this owns, both mirroring ``formatMoneyToParts`` exactly:

    * **Round half away from zero.** ``Intl`` breaks a tie by taking the larger
      candidate; Python's built-in :func:`round` is half-to-even and would print
      ``$0.01`` where the page prints ``$0.02`` for ``0.015``. The sharp edge is
      ``Decimal(str(amount))``, deliberately not ``Decimal(amount)``: ``Intl``
      rounds the number's shortest decimal representation, and ``str`` of a
      float is that same representation. Passing the float straight to
      ``Decimal`` rounds the binary value instead and disagrees on exactly the
      halves.
    * **A positive amount that rounds to nothing reads ``< <minor unit>``.**
      ``$0.004`` is real spend, and ``$0.00`` would say there was none. The
      floor is the currency's own minor unit, never a literal cent: a JPY figure
      below ¥1 reads ``< ¥1``. A negative that rounds to nothing renders as a
      plain positive zero, because ``-¥0`` is not a figure.

    ``show_tiny=False`` is for callers that want the suppressed zero — the
    dashboard's axis labels do the same thing for the same reason. An amount
    that is not finite raises: every money value in this system is a
    :class:`~app.core.money.Money`, which rejects those at construction, so a
    non-finite figure here is a bug rather than a state to render.
    """
    if not isfinite(amount):
        raise ValueError(f"cannot format a non-finite amount: {amount!r}")
    unit = Decimal(1).scaleb(-_fraction_digits(currency))
    value = Decimal(str(amount))
    try:
        rounded = value.quantize(unit, rounding=ROUND_HALF_UP)
    except InvalidOperation as error:  # pragma: no cover — guarded by isfinite above
        raise ValueError(f"cannot format {amount!r} as {currency}") from error
    if rounded == 0:
        if show_tiny and value > 0:
            return f"< {_render(unit, currency, locale)}"
        return _render(Decimal(0), currency, locale)
    return _render(rounded, currency, locale)

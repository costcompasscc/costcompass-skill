"""The CLI's money formatter against the shared currency-format corpus.

``test-vectors/currency-format/cases.json`` is the authored contract the web page
(``Intl.NumberFormat``), the PDF (``app/core/currency_format.py``) and now this
renderer are asserted against. It is vendored by the main repository's
``scripts/sync-test-vectors.sh``; the expectation is owned there, never here.

Because the CLI reads the same probed ICU table the PDF reads, a failure here is
a rendering difference on a surface a user reads — read the diff, do not
re-capture.
"""

from __future__ import annotations

import decimal
import json
from pathlib import Path

import pytest

from costcompass import currency_format, render

_CASES = json.loads(
    (Path(__file__).parent / "vectors" / "currency-format" / "cases.json").read_text(
        encoding="utf-8"
    )
)["cases"]


def _id(case: dict) -> str:
    return f"{case['locale']}-{case['currency']}-{case['amount']!r}"


@pytest.mark.parametrize("case", _CASES, ids=_id)
def test_money_matches_the_shared_corpus(case: dict) -> None:
    assert (
        currency_format.format_money(case["amount"], case["currency"], case["locale"])
        == case["expected"]
    )


@pytest.mark.parametrize("case", _CASES, ids=_id)
def test_render_money_uses_the_same_table(case: dict) -> None:
    """``render.money`` is the CLI's public spelling of the same rule."""
    assert (
        render.money(case["amount"], case["currency"], case["locale"])
        == case["expected"]
    )


def test_unknown_locale_falls_back_to_the_default() -> None:
    """A locale the vendored table does not carry must not reach `format_money`.

    The CLI ships on its own cadence, so a server can add a supported locale
    before this build is re-vendored; a traceback is not an answer.
    """
    assert render.locale_of({"display_locale": "zz-ZZ"}) == render.DEFAULT_LOCALE
    assert render.locale_of({"display_locale": "de-DE"}) == "de-DE"
    assert render.format_amount({"mtd": {"USD": 1.5}}, locale="zz-ZZ") == "$1.50"


def test_format_money_refuses_a_non_finite_amount() -> None:
    """The refusal has a type a caller can branch on. It subclasses
    ``ValueError`` because that is what this raised before the type existed."""
    for amount in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(currency_format.MoneyValueError, match="non-finite"):
            currency_format.format_money(amount, "USD", "en-US")
    assert issubclass(currency_format.MoneyValueError, ValueError)


def test_a_finite_amount_larger_than_the_context_precision_still_renders() -> None:
    """``quantize`` inside the default 28-digit context raised
    ``InvalidOperation`` for these, so the renderer gave up on a finite figure.
    The rounding rule is not context-dependent, so the context is widened —
    the same answer ``Intl`` gives, not a rounded-away figure."""
    assert currency_format.format_money(1e26, "USD", "en-US") == (
        "$100,000,000,000,000,000,000,000,000.00"
    )
    huge = currency_format.format_money(1e308, "USD", "en-US")
    assert huge.startswith("$100,000,") and huge.endswith("000.00")


def test_no_ambient_decimal_context_setting_changes_the_figure() -> None:
    """The renderer renders inside its own context, not the caller's.

    ``quantize`` and ``Decimal.__format__`` both read the ambient context, so a
    caller who set two digits got ``$1,200.00`` for ``$1,234.50``, and one who
    narrowed ``Emax`` or trapped ``Inexact``/``Rounded`` got a
    ``decimal.InvalidOperation`` — an ``ArithmeticError``, which is not what any
    caller of this module catches — for a finite amount the page spells. The
    caller's own context is left exactly as it was.
    """
    with decimal.localcontext() as context:
        context.prec = 2
        assert currency_format.format_money(1234.5, "USD", "en-US") == "$1,234.50"
        assert currency_format.format_money(0.015, "USD", "en-US") == "$0.02"
        assert context.prec == 2

    with decimal.localcontext() as context:
        context.Emax = 100
        context.traps[decimal.Inexact] = True
        context.traps[decimal.Rounded] = True
        huge = currency_format.format_money(1e308, "USD", "en-US")
        assert huge.startswith("$100,000,") and huge.endswith("000.00")
        assert currency_format.format_money(0.015, "USD", "en-US") == "$0.02"
        assert (context.Emax, context.traps[decimal.Inexact]) == (100, True)

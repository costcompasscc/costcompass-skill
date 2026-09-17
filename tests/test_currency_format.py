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

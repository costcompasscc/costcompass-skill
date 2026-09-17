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

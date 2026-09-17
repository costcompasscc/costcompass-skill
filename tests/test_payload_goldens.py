"""Assert the CLI's vendored dashboard-payload render goldens.

The expectation is owned by the CostCompass main repository. Update it there,
then run ``scripts/sync-test-vectors.sh`` from that repository to vendor it.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path

import pytest

from costcompass import render

_CORPUS = Path(__file__).parent / "vectors" / "dashboard-payloads"


def _scenario_names() -> list[str]:
    return [
        scenario["name"]
        for scenario in json.loads((_CORPUS / "coverage.json").read_text())["scenarios"]
    ]


def _numbered(prefix: str, output: str) -> list[str]:
    return [
        f"{prefix}/{index:02d}\t{line}"
        for index, line in enumerate(output.splitlines())
    ]


def _append_note(lines: list[str], prefix: str, summary: dict) -> None:
    note = render.staleness_note(summary)
    if note:
        lines.extend(_numbered(prefix, note))


def _render(scenario: str) -> str:
    directory = _CORPUS / scenario
    summary = json.loads((directory / "summary.json").read_text())
    breakdown = json.loads((directory / "breakdown.json").read_text())
    scenario = json.loads((directory / "scenario.json").read_text())
    request = scenario.get("request", {})
    summary_scope = request.get("summary.json", {})
    # The scenario authors the reader preferences the capture seeded; a
    # single-currency scenario leaves them unset, so the renderer's defaults
    # (en-US, no primary) apply and the golden stays byte-identical.
    locale = scenario.get("display_locale") or render.DEFAULT_LOCALE
    primary = scenario.get("primary_currency")
    lines = _numbered(
        "mtd", render.format_amount(summary, locale=locale, primary_currency=primary)
    )
    _append_note(lines, "mtd-staleness", summary)
    lines.extend(
        _numbered(
            "breakdown",
            render.format_breakdown(breakdown, locale=locale, primary_currency=primary),
        )
    )
    provider_id = summary_scope.get("provider")
    if provider_id:
        card = next(card for card in breakdown if card["provider_id"] == provider_id)
        models = [
            model
            for entry in breakdown
            if entry["provider_id"] == provider_id
            for model in entry.get("model_breakdown", [])
        ]
        lines.extend(
            _numbered(
                "details-00",
                render.format_details(
                    card["display_name"],
                    summary,
                    models,
                    locale=locale,
                    primary_currency=primary,
                ),
            )
        )
        _append_note(lines, "details-00-staleness", summary)

    for index, card in enumerate(
        card for card in breakdown if card.get("kind") == "subscription"
    ):
        lines.extend(
            _numbered(
                f"subscription-{index:02d}",
                render.format_subscription(
                    card["display_name"],
                    card.get("totals") or {},
                    locale=locale,
                    primary_currency=primary,
                ),
            )
        )
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("scenario", _scenario_names())
def test_cli_renderer_matches_vendored_golden(scenario: str) -> None:
    expected = (_CORPUS / scenario / "cli-render.txt").read_text()
    actual = _render(scenario)
    if actual == expected:
        return
    diff = "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=f"vendored/{scenario}/cli-render.txt",
            tofile=f"actual/{scenario}/cli-render.txt",
        )
    )
    raise AssertionError(
        f"{scenario}/cli-render.txt no longer matches the vendored golden.\n\n"
        f"{diff}\nThe expectation is owned by the CostCompass main repository; "
        "run its scripts/sync-test-vectors.sh after an approved canonical update."
    )

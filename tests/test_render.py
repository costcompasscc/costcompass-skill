from __future__ import annotations

import pytest

from costcompass import render


def test_money():
    assert render.money(1234.5) == "$1,234.50"
    assert render.money(0) == "$0.00"


# The cross-implementation table. Its home is
# `frontend/src/lib/money-display-cases.ts` in the main repo, where the web
# suite and the server's `test_report_money_parity.py` both read it directly.
# This CLI ships from its own repo on its own cadence, so the list is carried
# over by hand rather than read — the same call the macOS app makes, and for
# the same reason: a mechanical guard would fail this suite for a drift no
# change here can fix.
#
# The point of the table is that `costcompass mtd` and the dashboard are two
# renderings of one number. Every case below was a real disagreement between
# some pair of the four surfaces.
MONEY_DISPLAY_CASES = [
    (0, "$0.00"),
    # Sub-cent stays visible. The old `f"{v:,.2f}"` printed "$0.00" here,
    # which reads as no charge at all — and per-model rows on a metered card
    # are exactly where sub-cent amounts live.
    (0.004, "< $0.01"),
    (0.0049, "< $0.01"),
    (0.005, "$0.01"),
    (0.01, "$0.01"),
    # Exact-looking halves. Each is a hair below the half as a float, yet all
    # three round UP, because the rule is applied to the decimal
    # representation and not to the binary value. These are the cases that
    # fail if this goes back to a bare format spec, which rounds half-to-even
    # on the binary double: it gives $0.01 / $1.00 / $2.67 for these three.
    (0.015, "$0.02"),
    (1.005, "$1.01"),
    (2.675, "$2.68"),
    (0.0553, "$0.06"),
    # Floats sitting a hair below the exact cent — the truncation bug that
    # made the PDF print $0.28 for $0.29.
    (0.29, "$0.29"),
    (0.57, "$0.57"),
    (1.15, "$1.15"),
    (1.999, "$2.00"),
    (8.61, "$8.61"),
    (57.86, "$57.86"),
    (1234.5, "$1,234.50"),
    (1234567.891, "$1,234,567.89"),
    # Sign outside the "$", and a negative that rounds to nothing loses the
    # sign rather than printing the "$-0.00" the old format spec produced.
    (-1.5, "-$1.50"),
    (-0.001, "$0.00"),
]


@pytest.mark.parametrize(("value", "expected"), MONEY_DISPLAY_CASES)
def test_money_matches_the_other_surfaces(value, expected):
    assert render.money(value) == expected


def test_no_positive_amount_ever_prints_as_zero():
    """Any positive amount renders either a real figure or "< $0.01".

    There is no third outcome. Walking the sub-cent boundary catches a gap
    reopening between the rounding and whatever decides the "< $0.01" case —
    the failure the macOS copy actually had, where a hand-written threshold
    and the rounding disagreed at exactly one value.
    """
    for step in range(1, 201):
        value = step * 0.0001
        assert render.money(value) != "$0.00", f"{value} rendered as no spend"


def test_breakdown_column_widens_for_a_sub_cent_row():
    """A sub-cent token is 7 characters where "$0.00" is 5, and the column is
    sized from the formatted strings — so the alignment must follow it rather
    than letting the token overflow its field and skew the table."""
    out = render.format_breakdown(
        [
            {"display_name": "Anthropic", "cost_usd": 12.5},
            {"display_name": "Atlas Cloud", "cost_usd": 0.004},
        ]
    )
    assert "< $0.01" in out
    lines = out.split("\n")
    # The amount field is right-justified, so leading padding differs per row
    # by design. What must hold is that the field is ONE width for everyone —
    # so every name starts at the same column, and the rule spans that width.
    name_columns = {
        line.index(name) for line, name in zip(lines, ["Anthropic", "Atlas Cloud"])
    }
    assert len(name_columns) == 1, out
    rule = next(line for line in lines if set(line.strip()) == {"-"})
    assert len(rule.strip()) == len("< $0.01"), out


def test_format_amount():
    assert render.format_amount({"mtd_usd": 42.5}) == "$42.50"


def test_format_details_headline_and_models():
    summary = {
        "mtd_usd": 100.0,
        "burn_rate_7day": 3.0,
        "forecast_usd": 90.0,
        "days_remaining": 5,
        "previous_month_usd": 80.0,
    }
    models = [
        {
            "model": "m1",
            "display_name": "Sonnet",
            "cost_usd": 70.0,
            "surface": "ai_usage",
        },
        {
            "model": "m2",
            "display_name": "Opus",
            "cost_usd": 30.0,
            "surface": "ai_usage",
        },
    ]
    out = render.format_details("Anthropic", summary, models)
    assert "Anthropic — $100.00 month-to-date" in out
    assert "Models:" in out
    # higher cost sorts first
    assert out.index("Sonnet") < out.index("Opus")
    assert "$70.00" in out


def test_format_details_display_value_for_unpriced():
    summary = {"mtd_usd": 0.0}
    models = [
        {
            "model": "q",
            "display_name": "Free tier",
            "cost_usd": 0.0,
            "display_value": "4.2K / 10K",
        },
    ]
    out = render.format_details("X", summary, models)
    assert "4.2K / 10K" in out
    assert "$0.00" not in out.split("\n")[-1]


def test_format_details_surface_ordering():
    summary = {"mtd_usd": 10.0}
    models = [
        {
            "model": "c",
            "display_name": "Compute",
            "cost_usd": 5.0,
            "surface": "cloud_infra",
        },
        {
            "model": "a",
            "display_name": "Tokens",
            "cost_usd": 5.0,
            "surface": "ai_usage",
        },
    ]
    out = render.format_details("G", summary, models)
    assert out.index("Models") < out.index("Services")


def test_format_subscription():
    out = render.format_subscription("Higgsfield", 14.5)
    assert "Higgsfield — $14.50 month-to-date" in out
    assert "subscription" in out


def test_format_breakdown_ranks_and_totals():
    cards = [
        {
            "provider_id": "anthropic",
            "display_name": "Anthropic",
            "kind": "provider",
            "cost_usd": 96.71,
        },
        {
            "provider_id": "u1",
            "display_name": "Higgsfield",
            "kind": "subscription",
            "cost_usd": 14.5,
        },
        {
            "provider_id": "openai",
            "display_name": "OpenAI",
            "kind": "provider",
            "cost_usd": 19.39,
        },
    ]
    out = render.format_breakdown(cards)
    # ranked by cost desc
    assert out.index("Anthropic") < out.index("OpenAI") < out.index("Higgsfield")
    # subscription tagged, providers not
    assert "Higgsfield  (subscription)" in out
    assert "Anthropic  (subscription)" not in out
    # reconciling total
    assert "$130.60" in out  # 96.71 + 19.39 + 14.50
    assert "Total" in out


def test_safe_text_strips_terminal_control_sequences():
    # A provider-supplied name carrying an ANSI/OSC escape must not survive into
    # terminal output — the ESC and other C0/C1 controls are dropped.
    assert render.safe_text("\x1b[31mEVIL\x1b[0m") == "[31mEVIL[0m"
    assert render.safe_text("\x1b]0;hijack\x07name") == "]0;hijackname"
    assert "\x1b" not in render.safe_text("a\x1bb\x9cc")
    # Legitimate Unicode (accents, emoji) is preserved.
    assert render.safe_text("Café 🚀") == "Café 🚀"


def test_format_breakdown_neutralizes_malicious_card_name():
    cards = [
        {
            "provider_id": "x",
            "display_name": "\x1b]0;pwned\x07Acme",
            "kind": "provider",
            "cost_usd": 1.0,
        }
    ]
    out = render.format_breakdown(cards)
    assert "\x1b" not in out
    assert "Acme" in out


def test_mtd_total_discloses_an_incomplete_window():
    """A bare figure reads as settled. When the server knows part of the month
    never arrived, the number is a floor and must say so."""
    out = render.format_amount({"mtd_usd": 12.5, "incomplete_card_count": 1})
    assert out.startswith("$12.50\n")
    assert "hasn't finished loading" in out
    assert "may be low" in out


def test_mtd_total_stays_bare_when_every_window_is_whole():
    assert (
        render.format_amount({"mtd_usd": 12.5, "incomplete_card_count": 0}) == "$12.50"
    )
    # An older server omits the field entirely — no caveat, unchanged output.
    assert render.format_amount({"mtd_usd": 12.5}) == "$12.50"


def test_incomplete_note_agrees_in_number():
    assert "1 service hasn't" in (
        render.incomplete_window_note({"incomplete_card_count": 1}) or ""
    )
    assert "3 services haven't" in (
        render.incomplete_window_note({"incomplete_card_count": 3}) or ""
    )


# ---------- Stale-data reminder ----------
#
# Every judgement below is the server's: which cards are behind, how far, and
# whether refreshing can fix them. The threshold and the day arithmetic live in
# `UserProviderRepo.stale_cards`, and its boundary tests are the only ones in
# the system — these cover this renderer's phrasing and nothing else. If you
# came here to change when a card counts as stale, you are in the wrong repo.


def _card(**overrides) -> dict:
    return {
        "provider_id": "anthropic",
        "instance_key": "",
        "display_name": "Anthropic",
        "days": 40,
        "blocked": False,
        **overrides,
    }


def _summary(*cards, enabled=None) -> dict:
    return {
        "enabled_provider_count": len(cards) if enabled is None else enabled,
        "stale_cards": list(cards),
    }


def test_staleness_names_the_service_and_its_age():
    """A name is the answer a wrong-looking total actually needs — "your data is
    old" sends the user hunting through every card."""
    assert render.staleness_note(_summary(_card())) == (
        "Not updated: Anthropic (40 days). "
        "Run 'costcompass mtd refresh --vault' for the latest numbers."
    )


def test_staleness_lists_every_card_without_truncating():
    """The dashboard trims to three names because it renders beside a figure in
    a fixed row. This output is read by a person or by the skill's model, and a
    hidden remainder serves neither."""
    cards = [_card(display_name=f"P{i}", days=40 - i) for i in range(5)]
    note = render.staleness_note(_summary(*cards)) or ""
    for i in range(5):
        assert f"P{i} ({40 - i} days)" in note
    assert "more" not in note


def test_staleness_marks_a_blocked_card_in_the_list():
    """Refresh fans out to every enabled card, so a card weeks behind has
    usually already been tried and failed."""
    note = render.staleness_note(_summary(_card(blocked=True))) or ""
    assert "Anthropic (40 days, can't connect)" in note


def test_staleness_changes_the_advice_when_every_card_is_blocked():
    assert render.staleness_note(_summary(_card(blocked=True))) == (
        "Not updated: Anthropic (40 days, can't connect). "
        "These can't connect — check their credentials."
    )


def test_staleness_still_suggests_refresh_when_one_card_is_collectable():
    """One fixable card means the command still helps; the blocked one is
    already marked in the list."""
    note = render.staleness_note(
        _summary(_card(display_name="Broken", blocked=True), _card(display_name="Old"))
    )
    assert note is not None and note.endswith(
        "Run 'costcompass mtd refresh --vault' for the latest numbers."
    )


def test_staleness_says_cant_connect_when_the_first_fetch_is_what_failed():
    """A mistyped key leaves a card with no age AND no way to get one. The
    first-refresh nudge would contradict the server, which already said
    refreshing cannot succeed."""
    note = render.staleness_note(_summary(_card(days=None, blocked=True)))
    assert note is not None
    assert "can't connect" in note
    assert "to pull your data" not in note


def test_staleness_reports_never_fetched_when_no_card_has_an_age():
    note = render.staleness_note(_summary(_card(days=None), _card(days=None)))
    assert note == (
        "No usage fetched yet. Run 'costcompass mtd refresh --vault' to pull your data."
    )


def test_staleness_spells_out_never_fetched_rather_than_zero_days():
    """`None` days is not `0` — zero would claim the card fetched today."""
    note = render.staleness_note(_summary(_card(days=None), _card(days=12))) or ""
    assert "Anthropic (never fetched)" in note


def test_staleness_falls_back_to_the_provider_id_without_a_display_name():
    """A retired plugin still has ingested spend; an id beats a blank."""
    note = render.staleness_note(_summary(_card(display_name=None))) or ""
    assert "anthropic (40 days)" in note


def test_staleness_strips_control_sequences_from_the_card_name():
    """display_name is server-returned, not hardcoded — same untrusted-origin
    text every other render.py call site runs through ``safe_text`` before
    printing, so a malicious or corrupted catalog entry can't smuggle a
    terminal escape sequence (cursor moves, title changes, OSC 8/52) into this
    line."""
    note = (
        render.staleness_note(_summary(_card(display_name="\x1b[31mEvil\x1b[0m"))) or ""
    )
    assert "\x1b" not in note
    assert "[31mEvil[0m (40 days)" in note


def test_staleness_is_silent_with_no_enabled_cards():
    """Nothing in the CLI refreshes on its own, so telling a user with no cards
    to run refresh would not help them."""
    assert render.staleness_note(_summary(_card(), enabled=0)) is None


def test_staleness_is_silent_against_an_older_server():
    """A server predating the field omits it — unchanged, silent output."""
    assert render.staleness_note({"mtd_usd": 12.5}) is None

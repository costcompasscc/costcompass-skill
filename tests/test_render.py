from __future__ import annotations

from datetime import UTC, datetime, timedelta

from costcompass import render


def test_money():
    assert render.money(1234.5) == "$1,234.50"
    assert render.money(0) == "$0.00"


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
# The dashboard states the same rule in
# frontend/src/lib/dashboard-derived.ts. The three "contract:" cases below are
# the shared boundary and are named to match its tests exactly — grep the name
# across both repos before changing either side.

NOW = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)


def _fetched(**delta) -> dict:
    """A one-card summary whose newest fetch was `delta` ago."""
    return {
        "enabled_provider_count": 1,
        "newest_fetched_at": (NOW - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def test_contract_2d59m_since_the_newest_fetch_is_silent():
    assert render.staleness_note(_fetched(days=2, minutes=59), NOW) is None


def test_contract_3d01m_since_the_newest_fetch_warns():
    note = render.staleness_note(_fetched(days=3, minutes=1), NOW)
    assert note == (
        "Not updated in 3 days. "
        "Run 'costcompass mtd refresh --vault' for the latest numbers."
    )


def test_contract_every_enabled_card_unfetched_reports_never_fetched():
    note = render.staleness_note(
        {"enabled_provider_count": 2, "newest_fetched_at": None}, NOW
    )
    assert note == (
        "No usage fetched yet. Run 'costcompass mtd refresh --vault' to pull your data."
    )


def test_staleness_is_silent_at_exactly_three_days():
    """The floor is "older than", not "at least" — the dashboard pins the same
    boundary, and an off-by-one here makes the two surfaces disagree."""
    assert render.staleness_note(_fetched(days=3), NOW) is None


def test_staleness_floors_the_day_count():
    assert "in 12 days" in (
        render.staleness_note(_fetched(days=12, hours=23), NOW) or ""
    )


def test_staleness_never_counts_below_three():
    """No singular "1 day" case can arise, so the copy is safely always plural."""
    assert "in 3 days" in (
        render.staleness_note(_fetched(days=3, seconds=1), NOW) or ""
    )


def test_staleness_stays_quiet_on_an_unparseable_timestamp():
    """The card HAS fetched, so the never-fetched copy would be false, and no
    age can be computed from it. Say nothing rather than guess."""
    note = render.staleness_note(
        {"enabled_provider_count": 1, "newest_fetched_at": "not-a-date"}, NOW
    )
    assert note is None


def test_staleness_treats_a_future_timestamp_as_fresh():
    """Clock skew, not twelve-days-negative — the count can never go below zero."""
    assert render.staleness_note(_fetched(days=-1), NOW) is None


def test_staleness_is_silent_with_no_enabled_cards():
    """Nothing in the CLI refreshes on its own, so telling a user with no cards
    to run refresh would not help them."""
    assert (
        render.staleness_note(
            {"enabled_provider_count": 0, "newest_fetched_at": None}, NOW
        )
        is None
    )


def test_staleness_is_silent_against_an_older_server():
    """A server predating the fields omits both — unchanged, silent output."""
    assert render.staleness_note({"mtd_usd": 12.5}, NOW) is None

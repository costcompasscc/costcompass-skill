from __future__ import annotations

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
        {"model": "m1", "display_name": "Sonnet", "cost_usd": 70.0, "surface": "ai_usage"},
        {"model": "m2", "display_name": "Opus", "cost_usd": 30.0, "surface": "ai_usage"},
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
        {"model": "q", "display_name": "Free tier", "cost_usd": 0.0, "display_value": "4.2K / 10K"},
    ]
    out = render.format_details("X", summary, models)
    assert "4.2K / 10K" in out
    assert "$0.00" not in out.split("\n")[-1]


def test_format_details_surface_ordering():
    summary = {"mtd_usd": 10.0}
    models = [
        {"model": "c", "display_name": "Compute", "cost_usd": 5.0, "surface": "cloud_infra"},
        {"model": "a", "display_name": "Tokens", "cost_usd": 5.0, "surface": "ai_usage"},
    ]
    out = render.format_details("G", summary, models)
    assert out.index("Models") < out.index("Services")


def test_format_subscription():
    out = render.format_subscription("Higgsfield", 14.5)
    assert "Higgsfield — $14.50 month-to-date" in out
    assert "subscription" in out


def test_format_breakdown_ranks_and_totals():
    cards = [
        {"provider_id": "anthropic", "display_name": "Anthropic", "kind": "provider", "cost_usd": 96.71},
        {"provider_id": "u1", "display_name": "Higgsfield", "kind": "subscription", "cost_usd": 14.5},
        {"provider_id": "openai", "display_name": "OpenAI", "kind": "provider", "cost_usd": 19.39},
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
    cards = [{"provider_id": "x", "display_name": "\x1b]0;pwned\x07Acme", "kind": "provider", "cost_usd": 1.0}]
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
    assert render.format_amount({"mtd_usd": 12.5, "incomplete_card_count": 0}) == "$12.50"
    # An older server omits the field entirely — no caveat, unchanged output.
    assert render.format_amount({"mtd_usd": 12.5}) == "$12.50"


def test_incomplete_note_agrees_in_number():
    assert "1 service hasn't" in (
        render.incomplete_window_note({"incomplete_card_count": 1}) or ""
    )
    assert "3 services haven't" in (
        render.incomplete_window_note({"incomplete_card_count": 3}) or ""
    )

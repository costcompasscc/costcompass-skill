from __future__ import annotations

import pytest

from costcompass import services

PROVIDERS = [
    {
        "id": "anthropic",
        "short_name": "Claude",
        "display_name": "Anthropic",
        "enabled": True,
    },
    {"id": "openai", "short_name": "OpenAI", "display_name": "OpenAI", "enabled": True},
    {
        "id": "google",
        "short_name": "Google",
        "display_name": "Google Cloud",
        "enabled": False,
    },
]


def test_resolve_by_id():
    assert services.resolve("anthropic", PROVIDERS)["id"] == "anthropic"


def test_resolve_by_short_name_claude():
    assert services.resolve("claude", PROVIDERS)["id"] == "anthropic"


def test_resolve_case_insensitive():
    assert services.resolve("CLAUDE", PROVIDERS)["id"] == "anthropic"


def test_resolve_by_display_name_despaced():
    assert services.resolve("googlecloud", PROVIDERS)["id"] == "google"


def test_unknown_lists_available():
    with pytest.raises(services.ResolveError) as exc:
        services.resolve("nope", PROVIDERS)
    msg = str(exc.value)
    assert "Unknown service 'nope'" in msg
    assert "anthropic" in msg


def test_prefers_enabled_instance():
    providers = [
        {"id": "google", "short_name": "Google", "enabled": False, "instance_key": ""},
        {
            "id": "google",
            "short_name": "Google",
            "enabled": True,
            "instance_key": "proj-2",
        },
    ]
    assert services.resolve("google", providers)["instance_key"] == "proj-2"


def test_ambiguous_across_ids():
    providers = [
        {"id": "a", "short_name": "Shared", "enabled": True},
        {"id": "b", "short_name": "Shared", "enabled": True},
    ]
    with pytest.raises(services.ResolveError) as exc:
        services.resolve("shared", providers)
    assert "ambiguous" in str(exc.value)


# --- match() (used for the combined provider + subscription read path) ---

SUBS = [
    {
        "id": "uuid-higgs",
        "display_name": "Higgsfield",
        "kind": "subscription",
        "cost_usd": 14.5,
    },
]


def test_match_returns_none_on_no_match():
    assert services.match("nope", PROVIDERS) is None


def test_match_resolves_subscription_by_display_name():
    card = services.match("higgsfield", SUBS)
    assert card is not None
    assert card["id"] == "uuid-higgs"
    assert card["cost_usd"] == 14.5


def test_match_ambiguous_subscriptions_raise():
    dupes = [
        {"id": "u1", "display_name": "Dup", "kind": "subscription"},
        {"id": "u2", "display_name": "Dup", "kind": "subscription"},
    ]
    with pytest.raises(services.ResolveError, match="ambiguous"):
        services.match("dup", dupes)


def test_available_names_lists_providers_and_subscriptions():
    msg = services.available_names(PROVIDERS, SUBS)
    assert "anthropic" in msg
    assert "subscriptions: Higgsfield" in msg

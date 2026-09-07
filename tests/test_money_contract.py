"""Malformed/renamed money must never become a successful zero-spend result."""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from costcompass import api, config, main, render, vault
from costcompass.refresh import orchestrator

SUMMARY = {
    "mtd_usd": 12.0,
    "burn_rate_7day": 1.0,
    "forecast_usd": 20.0,
    "previous_month_usd": 10.0,
}
MISSING = object()


@pytest.mark.parametrize(
    "value",
    [
        MISSING,
        None,
        False,
        "secret-value",
        {},
        [],
        10**400,
        -(10**400),
        float("nan"),
        float("inf"),
    ],
)
def test_required_money_rejects_invalid_without_echoing_values(value):
    payload = {} if value is MISSING else {"cost_usd": value}
    with pytest.raises(api.ApiError, match="required money field 'cost_usd'") as exc:
        api.required_money(payload, "cost_usd")
    assert "secret-value" not in str(exc.value)


@pytest.mark.parametrize("value", [0, 0.0, -1.25, 0.0001, 42.5])
def test_required_money_preserves_valid_numbers(value):
    assert api.required_money({"cost_usd": value}, "cost_usd") == value


@pytest.mark.parametrize("field", SUMMARY)
@pytest.mark.parametrize("value", [MISSING, None])
def test_details_requires_each_money_metric(field, value):
    summary = dict(SUMMARY)
    if value is MISSING:
        del summary[field]
    else:
        summary[field] = value
    with pytest.raises(api.ApiError, match=field):
        render.format_details("Example", summary, [])


def test_unpriced_display_does_not_hide_missing_cost():
    with pytest.raises(api.ApiError, match="cost_usd"):
        render.format_details(
            "Example", SUMMARY, [{"model": "free", "display_value": "4K tokens"}]
        )


def test_empty_breakdown_is_legitimate_zero():
    assert main._breakdown_payload([]) == {"total_usd": 0, "cards": []}
    assert "$0.00" in render.format_breakdown([])


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("value", [MISSING, None, 10**400])
@pytest.mark.parametrize(
    "surface",
    [
        "total",
        "provider",
        "details",
        "model",
        "breakdown",
        "subscription",
        "subscription_details",
        "refresh",
    ],
)
def test_commands_fail_without_success_output(monkeypatch, as_json, value, surface):
    summary = dict(SUMMARY)
    cards = [
        {
            "provider_id": "example",
            "display_name": "Example",
            "cost_usd": 12.0,
            "model_breakdown": [{"model": "model", "cost_usd": 12.0}],
        },
        {
            "provider_id": "plan",
            "display_name": "Plan",
            "kind": "subscription",
            "cost_usd": 5.0,
        },
    ]
    field = "mtd_usd"
    target = summary
    if surface == "details":
        field = "forecast_usd"
    elif surface == "model":
        target, field = cards[0]["model_breakdown"][0], "cost_usd"
    elif surface == "breakdown":
        target, field = cards[0], "cost_usd"
    elif surface.startswith("subscription"):
        target, field = cards[1], "cost_usd"
    if value is MISSING:
        del target[field]
        # Simulate the forthcoming API rename, not just an empty response.
        target[field.removesuffix("_usd")] = {"USD": 42.5}
    else:
        target[field] = value

    requests = _install_api(monkeypatch, summary, cards)
    args = {
        "total": [],
        "provider": ["example"],
        "details": ["example", "details"],
        "model": ["example", "details"],
        "breakdown": ["breakdown"],
        "subscription": ["plan"],
        "subscription_details": ["plan", "details"],
        "refresh": ["refresh", "--vault"],
    }[surface]
    result = CliRunner().invoke(
        main.app, ["mtd", *args, *(["--json"] if as_json else [])]
    )
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert field in result.stderr
    assert "Update the CostCompass CLI/plugin" in result.stderr
    assert "Traceback" not in result.output
    if surface == "refresh":
        assert any(path.endswith("/finalize") for path in requests)


def _install_api(monkeypatch, summary, cards):
    requests = []

    def handler(request):
        path = request.url.path
        requests.append(path)
        if path.endswith("/dashboard/summary"):
            body = summary
        elif path.endswith("/dashboard/breakdown"):
            body = cards
        elif path.endswith("/providers"):
            body = [{"id": "example", "display_name": "Example", "enabled": True}]
        elif path.endswith("/fetch-runs"):
            body = {"run_id": "run-1", "fetches": []}
        elif path.endswith("/finalize"):
            body = {"status": "success"}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=body)

    client_type = api.Client
    monkeypatch.setattr(
        api,
        "Client",
        lambda *a, **kw: client_type(
            *a, **kw, http=httpx.Client(transport=httpx.MockTransport(handler))
        ),
    )
    monkeypatch.setenv(config.ENV_API_KEY, "test-key")
    monkeypatch.setenv(config.ENV_VAULT_PASSWORD, "test-password")
    monkeypatch.setattr(
        orchestrator.vault_mod,
        "fetch_and_decrypt",
        lambda *_: vault.Vault(
            doc={"schema_version": 1, "entries": []},
            p2s=b"0" * 16,
            p2c=1000,
            revision=1,
        ),
    )
    return requests


@pytest.mark.parametrize("value", [0, 12])
def test_refresh_json_preserves_float_output(monkeypatch, value):
    _install_api(monkeypatch, {"mtd_usd": value}, [])
    result = CliRunner().invoke(main.app, ["mtd", "refresh", "--vault", "--json"])
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert json.loads(result.stdout) == {"mtd_usd": float(value), "providers": []}
    assert f'"mtd_usd": {value}.0' in result.stdout

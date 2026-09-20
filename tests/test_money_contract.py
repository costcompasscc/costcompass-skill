"""Malformed/renamed money must never become a successful zero-spend result.

The currency migration removed the scalar money fields (``mtd_usd`` and friends)
for a per-currency map. The fail-loud contract survives the rename: a missing or
malformed map is an incompatible response, and printing a settled ``$0.00`` for
an account the CLI could not understand is the regression this suite exists to
prevent.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from costcompass import api, config, main, render, vault
from costcompass.refresh import orchestrator

SUMMARY = {
    "mtd": {"USD": 12.0},
    "burn_rate_7day": {"USD": 1.0},
    "forecast": {"USD": 20.0},
    "previous_month": {"USD": 10.0},
}
MISSING = object()

_INVALID_MAPS: list[Any] = [
    MISSING,
    None,
    False,
    "secret-value",
    [],
    10**400,
    -(10**400),
    float("nan"),
    float("inf"),
]


@pytest.mark.parametrize("value", _INVALID_MAPS)
def test_required_totals_rejects_invalid_without_echoing_values(value):
    payload = {} if value is MISSING else {"mtd": value}
    with pytest.raises(api.ApiError, match="required money map 'mtd'") as exc:
        api.required_totals(payload, "mtd")
    assert "secret-value" not in str(exc.value)


@pytest.mark.parametrize(
    "inner", ["secret-value", None, float("nan"), float("inf"), 10**400]
)
def test_required_totals_rejects_an_invalid_entry(inner):
    with pytest.raises(api.ApiError, match="required money map 'mtd'") as exc:
        api.required_totals({"mtd": {"USD": inner}}, "mtd")
    assert "secret-value" not in str(exc.value)


@pytest.mark.parametrize(
    "value",
    [
        {"USD": 0},
        {"USD": 0.0},
        {"USD": -1.25},
        {"USD": 0.0001},
        {"USD": 42.5},
        {"USD": 42.5, "LKR": 2900.0},
    ],
)
def test_required_totals_preserves_valid_maps(value):
    assert api.required_totals({"mtd": value}, "mtd") == {
        code: float(amount) for code, amount in value.items()
    }


@pytest.mark.parametrize("field", SUMMARY)
@pytest.mark.parametrize("value", [MISSING, None])
def test_details_requires_each_money_map(field, value):
    summary = {key: dict(totals) for key, totals in SUMMARY.items()}
    if value is MISSING:
        del summary[field]
    else:
        summary[field] = value
    with pytest.raises(api.ApiError, match=field):
        render.format_details("Example", summary, [])


def test_unpriced_display_does_not_hide_missing_amount():
    with pytest.raises(api.ApiError, match="amount"):
        render.format_details(
            "Example",
            SUMMARY,
            [{"model": "free", "currency": "USD", "display_value": "4K tokens"}],
        )


@pytest.mark.parametrize("value", [MISSING, None, "", 5, [], {}])
def test_details_requires_a_currency_on_every_model_row(value):
    """One model row is one currency (design §5), so a row that names none is an
    incompatible response — the same user-facing failure as a missing amount,
    not a bare ValueError out of the renderer."""
    row: dict[str, Any] = {"model": "model", "currency": "USD", "amount": 1.0}
    if value is MISSING:
        del row["currency"]
    else:
        row["currency"] = value
    with pytest.raises(api.ApiError, match="currency") as exc:
        render.format_details("Example", SUMMARY, [row])
    assert "Update the CostCompass CLI/plugin" in str(exc.value)


def test_empty_breakdown_is_legitimate_zero():
    assert main._breakdown_payload([]) == {"totals": {}, "cards": []}
    assert "$0.00" in render.format_breakdown([])


@pytest.mark.parametrize("as_json", [False, True])
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
def test_commands_fail_without_success_output(monkeypatch, as_json, surface):
    summary = {key: dict(totals) for key, totals in SUMMARY.items()}
    cards: list[dict[str, Any]] = [
        {
            "provider_id": "example",
            "display_name": "Example",
            "totals": {"USD": 12.0},
            "model_breakdown": [{"model": "model", "currency": "USD", "amount": 12.0}],
        },
        {
            "provider_id": "plan",
            "display_name": "Plan",
            "kind": "subscription",
            "totals": {"USD": 5.0},
        },
    ]
    field = "mtd"
    target: dict[str, Any] = summary
    if surface == "details":
        field = "forecast"
    elif surface == "model":
        target, field = cards[0]["model_breakdown"][0], "amount"
    elif surface == "breakdown":
        target, field = cards[0], "totals"
    elif surface.startswith("subscription"):
        target, field = cards[1], "totals"
    del target[field]

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


@pytest.mark.parametrize("as_json", [False, True])
def test_commands_fail_when_a_model_row_names_no_currency(monkeypatch, as_json):
    """The details command must print the incompatible-response message and exit
    1 when a model row names no currency — never a Python traceback. ``--json``
    forces quiet output, so a failure must reach stderr with no success object
    on stdout."""
    summary = {key: dict(totals) for key, totals in SUMMARY.items()}
    cards: list[dict[str, Any]] = [
        {
            "provider_id": "example",
            "display_name": "Example",
            "totals": {"USD": 12.0},
            "model_breakdown": [{"model": "model", "amount": 12.0}],
        }
    ]
    _install_api(monkeypatch, summary, cards)
    result = CliRunner().invoke(
        main.app, ["mtd", "example", "details", *(["--json"] if as_json else [])]
    )
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert "currency" in result.stderr
    assert "Update the CostCompass CLI/plugin" in result.stderr
    assert "Traceback" not in result.output


def _install_api(monkeypatch, summary, cards, *, raw=None):
    requests = []
    raw = raw or {}

    def handler(request):
        path = request.url.path
        requests.append(path)
        for suffix, body in raw.items():
            if path.endswith(suffix):
                # Raw bytes, not ``json=``: httpx encodes a JSON body with
                # ``allow_nan=False``, so a lenient upstream that wrote a bare
                # ``NaN`` can only be reproduced by writing the bytes here.
                return httpx.Response(200, content=body)
        if path.endswith("/dashboard/summary"):
            body = summary
        elif path.endswith("/dashboard/breakdown"):
            body = cards
        elif path.endswith("/providers"):
            body = [{"id": "example", "display_name": "Example", "enabled": True}]
        elif path.endswith("/account/preferences"):
            body = {"display_locale": None, "primary_currency": None}
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
    _install_api(monkeypatch, {"mtd": {"USD": value}}, [])
    result = CliRunner().invoke(main.app, ["mtd", "refresh", "--vault", "--json"])
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "mtd": {"USD": float(value)},
        "providers": [],
    }


# --- A36: non-finite money and strict JSON -------------------------------
#
# ``json.loads`` accepts the bare ``NaN``/``Infinity``/``-Infinity`` literals;
# ``JSON.parse``, ``jq`` and Go's decoder reject them. The CLI took them off the
# wire and handed them straight back under ``--json``, so stdout could be a
# document no strict JSON parser will read. httpx encodes its *own* replies with
# ``allow_nan=False``, which is why the lenient replies below are raw bytes:
# a lenient upstream is the premise of the failure.


def _reject_constant(token: str) -> Any:
    raise ValueError(f"bare {token} is not JSON")


def _load_strict(text: str) -> Any:
    """*text* parsed the way a strict consumer parses it."""
    return json.loads(text, parse_constant=_reject_constant)


def test_json_output_is_strict(monkeypatch):
    """The property is produced here, so it is asserted here: every byte of a
    successful ``--json`` stdout is readable by a strict parser."""
    _install_api(monkeypatch, {key: dict(t) for key, t in SUMMARY.items()}, [])
    result = CliRunner().invoke(main.app, ["mtd", "--json"])
    assert result.exit_code == 0, result.output
    assert _load_strict(result.stdout)["mtd"] == {"USD": 12.0}


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_json_output_refuses_a_bare_literal_it_cannot_re_emit(monkeypatch, token):
    """A number this CLI only passes through must fail the command rather than
    reach the caller as a bare literal. ``burn_rate_7day`` is deliberately a
    field ``mtd`` never reads, so nothing here is a figure the CLI refused to
    *spell* — the document itself is what only JSON mode re-emits."""
    raw = b'{"mtd": {"USD": 12.0}, "burn_rate_7day": {"USD": ' + token.encode() + b"}}"
    _install_api(monkeypatch, None, [], raw={"dashboard/summary": raw})
    result = CliRunner().invoke(main.app, ["mtd", "--json"])
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert "JSON cannot" in result.stderr
    assert token not in result.stdout
    assert "Traceback" not in result.output


def test_human_output_ignores_a_field_only_json_re_emits(monkeypatch):
    """The boundary is what the CLI spells. The same reply renders fine as
    text because no human line reads that field; per-field response validation
    is a separate contract."""
    raw = b'{"mtd": {"USD": 12.0}, "burn_rate_7day": {"USD": NaN}}'
    _install_api(monkeypatch, None, [], raw={"dashboard/summary": raw})
    result = CliRunner().invoke(main.app, ["mtd"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "$12.00"


@pytest.mark.parametrize("as_json", [False, True])
def test_breakdown_refuses_a_total_that_overflows(monkeypatch, as_json):
    """Two finite amounts can sum past the double range, and the total is then
    a number neither output can carry: ``--json`` wrote the bare ``Infinity``
    and the human renderer raised a bare ``ValueError`` out of ``quantize``.
    Both are the one typed failure now."""
    cards = [
        {"provider_id": "a", "display_name": "A", "totals": {"USD": 1e308}},
        {"provider_id": "b", "display_name": "B", "totals": {"USD": 1e308}},
    ]
    _install_api(monkeypatch, {key: dict(t) for key, t in SUMMARY.items()}, cards)
    result = CliRunner().invoke(
        main.app, ["mtd", "breakdown", *(["--json"] if as_json else [])]
    )
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert "Incompatible API response" in result.stderr
    assert "Traceback" not in result.output


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize(
    "amount", [10**400, float("nan"), float("inf"), "12.0", None, [12.0]]
)
def test_details_refuses_a_model_amount_it_cannot_spell(monkeypatch, as_json, amount):
    """A model row's amount is the same boundary as a totals map: anything
    that is not a finite number is one typed failure, not an ``OverflowError``
    from the sort key (``10**400``) or an unpriced row rendered as a display
    value (``None``)."""
    cards = [
        {
            "provider_id": "example",
            "display_name": "Example",
            "totals": {"USD": 12.0},
            "model_breakdown": [
                {"model": "model", "currency": "USD", "amount": amount}
            ],
        }
    ]
    _install_api(
        monkeypatch,
        {key: dict(t) for key, t in SUMMARY.items()},
        None,
        # Written as bytes with ``json.dumps``'s lenient default: httpx's own
        # ``json=`` refuses a non-finite number, and a server that sends one is
        # exactly the upstream under test.
        raw={"dashboard/breakdown": json.dumps(cards).encode()},
    )
    result = CliRunner().invoke(
        main.app, ["mtd", "example", "details", *(["--json"] if as_json else [])]
    )
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert "amount" in result.stderr
    assert "Traceback" not in result.output


@pytest.mark.parametrize("token", [float("nan"), float("inf"), float("-inf")])
def test_json_encoder_refuses_a_number_json_cannot_represent(token):
    """The encoder is the invariant, so a number JSON cannot represent is an
    incompatible response wherever it sits — nested under a money map or not.
    The message is fixed text: nothing from the document is interpolated."""
    with pytest.raises(api.ApiError, match="JSON cannot") as exc:
        main._json_text({"mtd": {"USD": 12.0}, "detail": {"rate": token}})
    assert "Incompatible API response" in str(exc.value)


def test_json_encoder_writes_the_document_shape_consumers_parse():
    assert _load_strict(main._json_text({"mtd": {"USD": 12.0}})) == {
        "mtd": {"USD": 12.0}
    }


def test_refresh_json_refuses_an_outcome_it_cannot_encode(monkeypatch):
    """``events_ingested`` is the broker's count, reported back here, so it is
    another field a lenient upstream can put a bare literal in. The refresh
    sink shares the one encoder."""
    _install_api(monkeypatch, {"mtd": {"USD": 1.0}}, [])
    monkeypatch.setattr(
        orchestrator,
        "run",
        lambda *a, **kw: orchestrator.RefreshResult(
            outcomes=[
                orchestrator.EntryOutcome(
                    provider_id="example",
                    instance_key="",
                    state="success",
                    events_ingested=float("inf"),
                )
            ],
            mtd={"USD": 1.0},
        ),
    )
    result = CliRunner().invoke(main.app, ["mtd", "refresh", "--vault", "--json"])
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert "JSON cannot" in result.stderr
    assert "Traceback" not in result.output

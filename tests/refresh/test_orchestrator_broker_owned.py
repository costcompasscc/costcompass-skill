"""A broker-owned entry (credential kind ``broker_queued``) is run by the
provider's broker from the server's job list. The CLI neither resolves a
credential for it nor submits for it; it finalizes, and when the run answers
``running`` it follows ``GET /fetch-runs/{id}`` until the entry ends.
Mirrors the browser's ``orchestrator.broker-owned.test.ts``.
"""

from __future__ import annotations

import json
import os

import httpx

from costcompass import api, config, vault
from costcompass.refresh import broker, orchestrator

PASSWORD = "pw"

BROKER_ENTRY = {
    "provider_id": "cloudco",
    "instance_key": "",
    "state": "pending",
    "skip_reason": None,
    "error_message": None,
    "plan": {"requests": [], "auth_header": "", "auth_scheme": None},
    "credential": {"kind": "broker_queued"},
}

RELAY_ENTRY = {
    "provider_id": "anthropic",
    "instance_key": "",
    "state": "pending",
    "signing_token": "tok-1",
    "plan": {
        "requests": [
            {
                "url": "https://api.anthropic.com/v1/usage",
                "method": "GET",
                "headers": {},
                "body": None,
                "purpose": "usage",
            }
        ],
        "auth_header": "x-api-key",
        "auth_scheme": None,
    },
}


def _summary(provider_id, state, events=0, error_message=None):
    return {
        "provider_id": provider_id,
        "instance_key": "",
        "state": state,
        "events_ingested": events,
        "error_message": error_message,
    }


def _status(status, broker_state, *extra):
    events = 7 if broker_state == "success" else 0
    return {
        "run_id": "run-1",
        "status": status,
        "providers": [_summary("cloudco", broker_state, events), *extra],
    }


def _blob():
    doc = {
        "schema_version": 1,
        "entries": [
            {
                "id": "1",
                "provider": "anthropic",
                "api_key": "sk-ant",
                "metadata": {"instance_key": ""},
            }
        ],
    }
    return vault.encrypt_jwe(
        json.dumps(doc).encode(), PASSWORD, os.urandom(16), vault.DEFAULT_PBKDF2_ITERS
    )


def _refresh(fetches, finalize, statuses, *, now=None, budget=None, sleeps=None):
    """Run one refresh against a stub App Server. ``statuses`` is consumed one
    per status read; an ``Exception`` in it answers that read with a 404."""
    calls: list[tuple[str, str]] = []
    statuses = list(statuses)
    blob = _blob()

    def api_handler(request: httpx.Request) -> httpx.Response:
        p, m = request.url.path, request.method
        calls.append((m, p))
        if m == "GET" and p.endswith("/vault"):
            return httpx.Response(
                200, json={"jwe": blob, "revision": 1, "updated_at": "x"}
            )
        if m == "POST" and p.endswith("/fetch-runs"):
            return httpx.Response(200, json={"run_id": "run-1", "fetches": fetches})
        if "/responses" in p:
            return httpx.Response(200, json={"state": "success", "events_ingested": 3})
        if "/finalize" in p:
            return httpx.Response(200, json=finalize)
        if m == "GET" and p.endswith("/fetch-runs/run-1"):
            nxt = statuses.pop(0)
            if isinstance(nxt, Exception):
                return httpx.Response(404, json={"detail": "fetch run not found"})
            return httpx.Response(200, json=nxt)
        if p.endswith("/dashboard/summary"):
            return httpx.Response(200, json={"mtd": {"USD": 9.0}})
        if p.endswith("/preferences"):
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": p})

    def broker_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": 200, "headers": {}, "body": "Yg==", "signature": "s"}
        )

    client = api.Client(
        "https://x/api/v1",
        "sk-cli",
        http=httpx.Client(transport=httpx.MockTransport(api_handler)),
    )
    brk = broker.BrokerClient(
        "https://x/broker/v1",
        "sk-cli",
        http=httpx.Client(transport=httpx.MockTransport(broker_handler)),
    )
    slept: list[float] = [] if sleeps is None else sleeps
    kwargs = {}
    if now is not None:
        kwargs["now"] = now
    if budget is not None:
        kwargs["run_budget_s"] = budget
    result = orchestrator.run(
        config.Config(api_key="sk-cli", api_url="https://x/api/v1"),
        "sk-cli",
        None,
        PASSWORD,
        client=client,
        broker=brk,
        echo=lambda *_: None,
        sleep=slept.append,
        **kwargs,
    )
    return result, calls, slept


def _status_reads(calls):
    return [c for c in calls if c == ("GET", "/api/v1/fetch-runs/run-1")]


def test_runs_nothing_for_the_entry_and_follows_the_run_until_it_ends():
    result, calls, slept = _refresh(
        [BROKER_ENTRY],
        _status("running", "pending"),
        [_status("running", "pending"), _status("success", "success")],
    )

    assert not any("/responses" in p for _, p in calls)
    assert len(_status_reads(calls)) == 2
    assert slept == [orchestrator.BROKER_POLL_INTERVAL_S] * 2
    (outcome,) = result.outcomes
    assert (outcome.provider_id, outcome.state, outcome.events_ingested) == (
        "cloudco",
        "success",
        7,
    )


def test_does_not_follow_when_finalize_already_answers_a_terminal_status():
    result, calls, _ = _refresh([BROKER_ENTRY], _status("failed", "failed"), [])

    assert _status_reads(calls) == []
    assert result.outcomes[0].state == "failed"


def test_stops_following_before_a_wait_would_pass_the_run_budget():
    clock = [1000.0]
    sleeps: list[float] = []

    def now():
        return clock[0] + sum(sleeps)

    result, _, slept = _refresh(
        [BROKER_ENTRY],
        _status("running", "pending"),
        [_status("running", "pending")] * 10,
        now=now,
        budget=orchestrator.BROKER_POLL_INTERVAL_S * 3 + 0.001,
        sleeps=sleeps,
    )

    # Three waits fit inside the budget; a fourth would end past it.
    assert len(slept) == 3
    assert result.outcomes[0].state == "refreshing"


def test_stops_following_when_a_status_read_fails_without_failing_the_run():
    result, _, slept = _refresh(
        [BROKER_ENTRY], _status("running", "pending"), [RuntimeError()]
    )

    assert len(slept) == 1
    assert result.outcomes[0].state == "refreshing"


def test_echoes_an_already_refreshing_skip_with_its_sentence():
    sentence = "Already refreshing. Wait for the current refresh to finish."
    skipped = {
        **BROKER_ENTRY,
        "state": "skipped",
        "skip_reason": "already_refreshing",
        "error_message": sentence,
    }
    result, calls, _ = _refresh(
        [skipped],
        {
            "run_id": "run-1",
            "status": "success",
            "providers": [_summary("cloudco", "skipped", error_message=sentence)],
        },
        [],
    )

    # Echoed, not routed through the credential resolver (whose unsupported-kind
    # skip would submit, and the server refuses a submit for this entry).
    assert not any("/responses" in p for _, p in calls)
    assert _status_reads(calls) == []
    assert result.outcomes[0].state == "skipped"
    assert result.outcomes[0].error_message == sentence


def test_runs_relay_entries_normally_beside_a_broker_owned_one():
    result, calls, _ = _refresh(
        [RELAY_ENTRY, BROKER_ENTRY],
        _status("running", "pending", _summary("anthropic", "success", 3)),
        [_status("success", "success", _summary("anthropic", "success", 3))],
    )

    assert [p for _, p in calls if "/responses" in p] == [
        "/api/v1/fetch-runs/run-1/responses"
    ]
    states = {o.provider_id: o.state for o in result.outcomes}
    assert states == {"anthropic": "success", "cloudco": "success"}

from __future__ import annotations

import base64
import json

import httpx
import pytest

import costcompass
from costcompass.refresh import broker


def make_broker(handler, **kw) -> broker.BrokerClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return broker.BrokerClient("https://x/broker/v1", "sk-cli", http=http, **kw)


def test_parse_broker_target():
    t = broker.parse_broker_target("https://api.anthropic.com/v1/usage?a=1&b=2", "GET")
    assert t == {
        "host": "api.anthropic.com",
        "path": "/v1/usage",
        "method": "GET",
        "query": "a=1&b=2",
    }


def test_parse_broker_target_no_query():
    t = broker.parse_broker_target("https://h/p", "POST")
    assert "query" not in t


def test_parse_broker_target_lowercases_host_only():
    # Must match the App Server's lower-cased signed host and the browser's
    # new URL().host (host case-folded; path/query case preserved), or a
    # body-bearing request fails the broker's cc-broker-req.v1 check.
    t = broker.parse_broker_target("https://EU.PostHog.COM/Api/Q?A=b", "POST")
    assert (t["host"], t["path"], t["query"]) == ("eu.posthog.com", "/Api/Q", "A=b")


def test_build_forward_request_merges_auth():
    req = {
        "url": "https://h/p",
        "method": "GET",
        "headers": {"Accept": "json"},
        "purpose": "u",
    }
    fwd = broker.build_forward_request(req, {"x-api-key": "k"}, "tok")
    assert fwd["headers"] == {"Accept": "json", "x-api-key": "k"}
    assert fwd["signing_token"] == "tok"


def test_flat_url_target_vector():
    # Same literals as the App Server signer
    # (backend/tests/unit/test_fetch_service.py::FLAT_VECTOR_*) and the browser
    # parseBrokerTarget — the encoded space (%20) must stay verbatim so the form
    # the untrusted relay hands the broker equals the form the App Server signed.
    t = broker.parse_broker_target(
        "https://api.cloudflare.com/client/v4/graphql?since=2026-06-01%2000%3A00",
        "POST",
    )
    assert (t["host"], t["path"], t["query"]) == (
        "api.cloudflare.com",
        "/client/v4/graphql",
        "since=2026-06-01%2000%3A00",
    )


def test_build_forward_request_relays_flat_req_sig():
    # A server-signed flat request carries req_sig in the plan dict; the CLI
    # relays it verbatim (the flat path needs no special-casing).
    req = {"url": "https://h/p", "method": "POST", "body": "Yg==", "req_sig": "abc123"}
    fwd = broker.build_forward_request(req, {}, "tok")
    assert fwd["req_sig"] == "abc123"
    # ...and omits it when unsigned (body-less flat request).
    bare = broker.build_forward_request(
        {"url": "https://h/p", "method": "GET"}, {}, "tok"
    )
    assert bare["req_sig"] is None


def test_broker_url_from_api():
    assert (
        broker.broker_url_from_api("https://costcompass.cc/api/v1")
        == "https://costcompass.cc/broker/v1"
    )
    assert (
        broker.broker_url_from_api("http://localhost:8080/api/v1/")
        == "http://localhost:8080/broker/v1"
    )


def test_forward_success_relays_signature():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "status": 200,
                "headers": {},
                "body": "Yg==",
                "signature": "deadbeef",
            },
        )

    client = make_broker(handler)
    resp = client.forward({"target": {}, "headers": {}, "signing_token": "t"})
    assert resp["signature"] == "deadbeef"
    assert resp["body"] == "Yg=="
    assert seen["auth"] == "Bearer sk-cli"


def test_forward_sends_cli_user_agent_only_on_its_own_request():
    """The relay identifies itself to the broker; the forwarded request must
    not carry a user-agent, which the broker rejects and sets itself."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("User-Agent")
        seen["forwarded"] = json.loads(request.content)["headers"]
        return httpx.Response(
            200,
            json={"status": 200, "headers": {}, "body": "Yg==", "signature": "s"},
        )

    make_broker(handler).forward(
        {"target": {}, "headers": {"Accept": "json"}, "signing_token": "t"}
    )
    assert seen["ua"] == costcompass.user_agent()
    assert not any(k.lower() == "user-agent" for k in seen["forwarded"])


def test_forward_cap_trips_after_n():
    client = make_broker(
        lambda r: httpx.Response(
            200, json={"status": 200, "headers": {}, "body": "", "signature": "s"}
        ),
        forward_cap=2,
    )
    req = {"target": {}, "headers": {}, "signing_token": "tok"}
    client.forward(dict(req))
    client.forward(dict(req))
    with pytest.raises(broker.BrokerForwardCapError):
        client.forward(dict(req))


def test_forward_cap_exempts_retries():
    client = make_broker(
        lambda r: httpx.Response(
            200, json={"status": 200, "headers": {}, "body": "", "signature": "s"}
        ),
        forward_cap=1,
    )
    req = {"target": {}, "headers": {}, "signing_token": "tok"}
    client.forward(dict(req))
    # a retry of an already-counted request must not trip the cap
    client.forward({**req, "is_retry": True})


def test_error_envelope_maps_code():
    client = make_broker(
        lambda r: httpx.Response(
            429, json={"error": {"code": "rate_limited", "message": "slow down"}}
        )
    )
    with pytest.raises(broker.BrokerError) as exc:
        client.forward({"target": {}, "headers": {}})
    assert exc.value.code == "rate_limited"
    assert exc.value.is_transient()


@pytest.mark.parametrize(
    ("code", "http_status", "expected"),
    [
        # The observed status wins outright, whatever the code says.
        ("rate_limited", 429, 429),
        ("upstream_timeout", 504, 504),
        ("upstream_unreachable", 502, 502),
        ("worker_pool_exhausted", 503, 503),
        ("broker_unreachable", 502, 502),
        ("service_unavailable", 503, 503),
        ("broker_timeout", 504, 504),
        ("something_unknown", 418, 418),
        # Statuses the old inverse map collapsed to a blanket 502 — the whole
        # point of relaying what the broker actually said.
        ("invalid_request", 400, 400),
        ("method_not_allowed", 405, 405),
        ("header_not_allowed", 400, 400),
        ("internal_error", 500, 500),
        # Client-synthesized errors are the only ones without a real status:
        # network_error never reached a server, malformed_response is a 2xx
        # whose envelope did not parse.
        ("network_error", 0, 502),
        ("malformed_response", 200, 500),
    ],
)
def test_relay_status_prefers_the_observed_transport_status(
    code, http_status, expected
):
    assert broker.relay_status(broker.BrokerError(code, http_status, "m")) == expected


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (502, "broker_unreachable"),
        (503, "service_unavailable"),
        (504, "broker_timeout"),
    ],
)
def test_bodyless_gateway_status_does_not_name_broker_internals(status, code):
    # A 5xx with no envelope means the broker never answered — some hop in
    # between wrote it. Naming a broker-internal code there points diagnosis at
    # the wrong service, so these three statuses get their own synthesized
    # codes. They must stay transient: that is the retry behaviour the codes
    # they replaced already had.
    client = make_broker(lambda r: httpx.Response(status, content=b""))
    with pytest.raises(broker.BrokerError) as exc:
        client.forward({"target": {}, "headers": {}})
    assert exc.value.code == code
    assert exc.value.http_status == status
    assert exc.value.is_transient()


def test_envelope_bearing_503_still_names_the_worker_pool():
    client = make_broker(
        lambda r: httpx.Response(
            503,
            json={"error": {"code": "worker_pool_exhausted", "message": "at capacity"}},
        )
    )
    with pytest.raises(broker.BrokerError) as exc:
        client.forward({"target": {}, "headers": {}})
    assert exc.value.code == "worker_pool_exhausted"
    assert exc.value.is_transient()


def test_provider_error_response_is_classifiable():
    r = broker.provider_error_response(429, "usage", "rate_limited: slow")
    assert r["status"] == 429
    assert "synthetic" not in r  # non-synthetic → server classifies, not stripped
    assert r["request_purpose"] == "usage"
    # No error_code → flat message body (backwards-compatible shape).
    body = json.loads(base64.b64decode(r["body_b64"]))
    assert body == {"error": "rate_limited: slow"}


def test_provider_error_response_reauth_code_in_body():
    # A 409 OAuth mint rejection must carry the reauth_required code in the body
    # so the App Server's shared reauth classifier (which keys on the body
    # string) fires — a message-only body would be misfiled as a generic error.
    r = broker.provider_error_response(
        409, "usage", "OAuth credential rejected", error_code="reauth_required"
    )
    assert r["status"] == 409
    body = json.loads(base64.b64decode(r["body_b64"]))
    assert body["error"]["code"] == "reauth_required"
    assert body["error"]["message"] == "OAuth credential rejected"
    assert b"reauth_required" in base64.b64decode(r["body_b64"])


def test_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = make_broker(handler)
    with pytest.raises(broker.BrokerError) as exc:
        client.forward({"target": {}, "headers": {}})
    assert exc.value.code == "network_error"


def test_parse_retry_after():
    assert broker.parse_retry_after("12") == 12.0
    assert broker.parse_retry_after(None) is None
    assert (
        broker.parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT") is None
    )  # HTTP-date ignored
    assert broker.parse_retry_after("999999") == broker.MAX_RETRY_AFTER_S  # capped


def test_retry_recovers_after_transient_broker_error():
    calls = {"n": 0}
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(
                503,
                json={"error": {"code": "worker_pool_exhausted", "message": "busy"}},
            )
        return httpx.Response(
            200, json={"status": 200, "headers": {}, "body": "Yg==", "signature": "s"}
        )

    client = make_broker(handler, sleep=slept.append)
    resp = client.forward_with_retry(
        {"target": {}, "headers": {}, "signing_token": "t"}
    )
    assert resp["status"] == 200
    assert calls["n"] == 3  # two transient failures, then success
    assert len(slept) == 2  # one backoff per retry


def test_retry_recovers_after_transient_upstream_status():
    # 200 from the broker but a transient UPSTREAM status (429) → retry.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        status = 429 if calls["n"] == 1 else 200
        return httpx.Response(
            200,
            json={
                "status": status,
                "headers": {"Retry-After": "0"},
                "body": "Yg==",
                "signature": "s",
            },
        )

    client = make_broker(handler, sleep=lambda *_: None)
    resp = client.forward_with_retry(
        {"target": {}, "headers": {}, "signing_token": "t"}
    )
    assert resp["status"] == 200
    assert calls["n"] == 2


def test_retry_exhausts_and_raises_final_error():
    client = make_broker(
        lambda r: httpx.Response(
            429, json={"error": {"code": "rate_limited", "message": "slow"}}
        ),
        retry_attempts=3,
        sleep=lambda *_: None,
    )
    with pytest.raises(broker.BrokerError) as exc:
        client.forward_with_retry({"target": {}, "headers": {}, "signing_token": "t"})
    assert exc.value.code == "rate_limited"


def test_retry_does_not_retry_non_transient():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            403, json={"error": {"code": "forbidden", "message": "no"}}
        )

    client = make_broker(handler, sleep=lambda *_: None)
    with pytest.raises(broker.BrokerError):
        client.forward_with_retry({"target": {}, "headers": {}, "signing_token": "t"})
    assert calls["n"] == 1  # forbidden is not transient → no retry


def test_retry_refused_when_the_backoff_does_not_fit_the_budget():
    # The run budget's third seam. Without it the loop sleeps the full
    # Retry-After and forwards again with no deadline in sight — RETRY_ATTEMPTS
    # sleeps at MAX_RETRY_AFTER_S is ~40 minutes past a 15-minute budget.
    calls = {"n": 0}
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            503, json={"error": {"code": "internal_error", "message": "x"}}
        )

    client = make_broker(handler, sleep=slept.append)
    with pytest.raises(broker.BrokerRunDeadlineError):
        client.forward_with_retry(
            {"target": {}, "headers": {}, "signing_token": "t"},
            can_wait=lambda _seconds: False,
        )
    # One forward, and no sleep: the wait is refused BEFORE it is taken, since
    # sleeping up to the deadline and only then giving up buys nothing.
    assert calls["n"] == 1
    assert slept == []


def test_retry_proceeds_when_the_backoff_fits_the_budget():
    # The other side of the check — a predicate that says yes must leave the
    # pre-budget behaviour exactly as it was.
    calls = {"n": 0}
    asked: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                503, json={"error": {"code": "internal_error", "message": "x"}}
            )
        return httpx.Response(
            200, json={"status": 200, "headers": {}, "body": "Yg==", "signature": "s"}
        )

    def can_wait(seconds: float) -> bool:
        asked.append(seconds)
        return True

    client = make_broker(handler, sleep=lambda *_: None)
    resp = client.forward_with_retry(
        {"target": {}, "headers": {}, "signing_token": "t"}, can_wait=can_wait
    )
    assert resp["status"] == 200
    assert calls["n"] == 2
    # Asked twice around the one sleep. First about the wait it is being
    # OFFERED, not a bare "is the budget spent" — the whole point is that the
    # answer depends on how long this particular backoff is. Then with 0 on the
    # far side, where the only question left is whether any budget survived a
    # sleep that may have run long.
    assert asked == [broker.RETRY_BASE_S, 0.0]


def test_retry_refused_when_the_sleep_itself_overruns_the_budget():
    # A sleep duration is a floor, not a promise: the host can suspend
    # mid-backoff, so an affordable wait can still end past the deadline. The
    # predicate is therefore asked again on the far side of it — with 0, "is
    # there any budget left at all" — before the next forward goes out.
    calls = {"n": 0}
    answers = [True, False]

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            503, json={"error": {"code": "internal_error", "message": "x"}}
        )

    client = make_broker(handler, sleep=lambda *_: None)
    with pytest.raises(broker.BrokerRunDeadlineError):
        client.forward_with_retry(
            {"target": {}, "headers": {}, "signing_token": "t"},
            can_wait=lambda _seconds: answers.pop(0),
        )
    # The wait was granted and taken; the forward on the far side never happens.
    assert calls["n"] == 1
    assert answers == []


def test_retry_does_not_count_against_forward_cap():
    # Each transient retry sets is_retry, so a long retry streak can't trip the
    # per-entry forward cap (which targets runaway poll loops, not recovery).
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(
                503, json={"error": {"code": "internal_error", "message": "x"}}
            )
        return httpx.Response(
            200, json={"status": 200, "headers": {}, "body": "", "signature": "s"}
        )

    client = make_broker(handler, forward_cap=1, sleep=lambda *_: None)
    resp = client.forward_with_retry(
        {"target": {}, "headers": {}, "signing_token": "tok"}
    )
    assert resp["status"] == 200
    # Only the initial attempt counted against the cap; a second fresh forward
    # for the same token would now trip it.
    with pytest.raises(broker.BrokerForwardCapError):
        client.forward({"target": {}, "headers": {}, "signing_token": "tok"})

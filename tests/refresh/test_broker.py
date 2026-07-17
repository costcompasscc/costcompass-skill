from __future__ import annotations

import base64
import json

import httpx
import pytest

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


def test_status_for_code_maps_transients():
    assert broker.status_for_code("rate_limited") == 429
    assert broker.status_for_code("upstream_timeout") == 504
    assert broker.status_for_code("upstream_unreachable") == 502
    assert broker.status_for_code("something_unknown") == 502


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

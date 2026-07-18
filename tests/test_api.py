from __future__ import annotations

import httpx
import pytest

from costcompass import api


def test_bearer_header_and_summary(make_api):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["path"] = request.url.path
        seen["provider"] = request.url.params.get("provider")
        return httpx.Response(200, json={"mtd_usd": 12.0})

    client = make_api(handler)
    out = client.summary(provider="anthropic")
    assert out["mtd_usd"] == 12.0
    assert seen["auth"] == "Bearer sk-test"
    assert seen["path"] == "/api/v1/dashboard/summary"
    assert seen["provider"] == "anthropic"


def test_summary_no_provider_param(make_api):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["provider"] = request.url.params.get("provider")
        return httpx.Response(200, json={"mtd_usd": 1.0})

    make_api(handler).summary()
    assert seen["provider"] is None


def test_401_maps_to_friendly_error(make_api):
    client = make_api(lambda r: httpx.Response(401, json={"error": "nope"}))
    with pytest.raises(api.ApiError) as exc:
        client.me()
    assert "Invalid or expired API key" in str(exc.value)


def test_connection_error_maps(make_api):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = make_api(handler)
    with pytest.raises(api.ApiError) as exc:
        client.summary()
    assert "Could not reach" in str(exc.value)


def test_redirect_maps_to_clean_error(make_api):
    # httpx does not follow redirects; a 3xx means api_url is not the API base
    # (http→https bounce, trailing-path redirect). Must be an ApiError, not a
    # JSONDecodeError traceback from the empty redirect body.
    client = make_api(
        lambda r: httpx.Response(301, headers={"location": "https://elsewhere/"})
    )
    with pytest.raises(api.ApiError) as exc:
        client.me()
    assert "redirected" in str(exc.value)


def test_non_json_success_body_maps_to_clean_error(make_api):
    # A 200 with an HTML body (captive proxy, wrong URL) must not escape as a
    # raw ValueError — and the body must not be echoed into the message.
    client = make_api(lambda r: httpx.Response(200, text="<html>welcome</html>"))
    with pytest.raises(api.ApiError) as exc:
        client.summary()
    assert "non-JSON" in str(exc.value)
    assert "welcome" not in str(exc.value)


def test_get_vault_non_json_body_maps_to_clean_error(make_api):
    client = make_api(lambda r: httpx.Response(200, text="not json"))
    with pytest.raises(api.ApiError):
        client.get_vault()


def test_get_vault_404_returns_none(make_api):
    client = make_api(lambda r: httpx.Response(404, json={"error": "vault_not_found"}))
    assert client.get_vault() is None


def test_create_fetch_run_body(make_api):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(200, json={"run_id": "r1", "fetches": []})

    client = make_api(handler)
    out = client.create_fetch_run(["anthropic"])
    assert out["run_id"] == "r1"
    assert b"anthropic" in seen["body"]

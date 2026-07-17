"""HTTP client for the CostCompass App Server (`/api/v1`).

The underlying ``httpx.Client`` is injectable so tests can supply an
``httpx.MockTransport`` and assert on requests without a network.
"""

from __future__ import annotations

from typing import Any

import httpx


class ApiError(Exception):
    """User-facing API failure (auth, connectivity, or a 4xx/5xx body)."""


class Client:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        http: httpx.Client | None = None,
    ) -> None:
        # base_url already includes the /api/v1 prefix.
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._http = http or httpx.Client(timeout=30.0)
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> httpx.Response:
        url = f"{self.base_url}{path}"
        try:
            resp = self._http.request(
                method, url, headers=self._headers, params=params, json=json
            )
        except httpx.RequestError as exc:
            raise ApiError(f"Could not reach {self.base_url}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ApiError("Invalid or expired API key.")
        if resp.status_code >= 400:
            # Never echo the response body: these endpoints (notably the
            # vault PUT) sit next to secret material, and an upstream error
            # body could carry sensitive content. The status + endpoint is
            # enough for the user to act on.
            raise ApiError(f"{method} {path} failed ({resp.status_code})")
        return resp

    # --- read endpoints -------------------------------------------------

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/me").json()

    def summary(self, provider: str | None = None) -> dict[str, Any]:
        params = {"provider": provider} if provider else None
        return self._request("GET", "/dashboard/summary", params=params).json()

    def breakdown(self) -> list[dict[str, Any]]:
        return self._request("GET", "/dashboard/breakdown").json()

    def providers(self) -> list[dict[str, Any]]:
        return self._request("GET", "/providers").json()

    # --- vault ----------------------------------------------------------

    def get_vault(self) -> dict[str, Any] | None:
        """Return {jwe, revision, updated_at}, or None if no vault exists."""
        url = f"{self.base_url}/vault"
        try:
            resp = self._http.request("GET", url, headers=self._headers)
        except httpx.RequestError as exc:
            raise ApiError(f"Could not reach {self.base_url}: {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code in (401, 403):
            raise ApiError("Invalid or expired API key.")
        if resp.status_code >= 400:
            # Secret-adjacent endpoint: status only, never the body.
            raise ApiError(f"GET /vault failed ({resp.status_code})")
        return resp.json()

    def put_vault(self, jwe: str, expected_revision: int) -> dict[str, Any]:
        return self._request(
            "PUT",
            "/vault",
            json={"jwe": jwe, "expected_revision": expected_revision},
        ).json()

    # --- fetch runs -----------------------------------------------------

    def create_fetch_run(
        self,
        providers: list[str] | None,
        instance_key: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"providers": providers}
        if instance_key is not None:
            body["instance_key"] = instance_key
        return self._request("POST", "/fetch-runs", json=body).json()

    def submit_responses(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST", f"/fetch-runs/{run_id}/responses", json=payload
        ).json()

    def finalize_run(self, run_id: str, cancelled: bool = False) -> dict[str, Any]:
        return self._request(
            "POST", f"/fetch-runs/{run_id}/finalize", json={"cancelled": cancelled}
        ).json()

"""HTTP client for the CostCompass App Server (`/api/v1`).

The underlying ``httpx.Client`` is injectable so tests can supply an
``httpx.MockTransport`` and assert on requests without a network.
"""

from __future__ import annotations

from typing import Any

import httpx


class ApiError(Exception):
    """User-facing API failure (auth, connectivity, or a 4xx/5xx body).

    ``status`` is the upstream HTTP status when there was one, else ``None``
    (connectivity failures, malformed bodies). Callers that need to branch on
    a specific status — the vault's revision conflict below — read it rather
    than matching on the message text.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class VaultRevisionConflict(ApiError):
    """``PUT /vault`` lost the server's compare-and-set on the revision.

    Another writer (another relay, or this one in a different process)
    committed a vault revision after the document we hold was read. The
    caller must re-read the server's current document and re-apply its
    change on top of it — never re-upload the copy it already holds.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, status=409)


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
        none_on_404: bool = False,
    ) -> httpx.Response | None:
        url = f"{self.base_url}{path}"
        try:
            resp = self._http.request(
                method, url, headers=self._headers, params=params, json=json
            )
        except httpx.RequestError as exc:
            raise ApiError(f"Could not reach {self.base_url}: {exc}") from exc
        if none_on_404 and resp.status_code == 404:
            return None
        if resp.status_code in (401, 403):
            raise ApiError("Invalid or expired API key.", status=resp.status_code)
        if resp.status_code >= 400:
            # Never echo the response body: these endpoints (notably the
            # vault PUT) sit next to secret material, and an upstream error
            # body could carry sensitive content. The status + endpoint is
            # enough for the user to act on.
            raise ApiError(
                f"{method} {path} failed ({resp.status_code})",
                status=resp.status_code,
            )
        if 300 <= resp.status_code < 400:
            # httpx does not follow redirects; a 3xx here means the configured
            # URL is not the API base (e.g. an http→https or trailing-path
            # redirect). Say so instead of choking on the empty body.
            raise ApiError(
                f"{method} {path} was redirected ({resp.status_code}) — "
                "check that the server URL points at the API base "
                "(e.g. https://host/api/v1)."
            )
        return resp

    def _json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> Any:
        resp = self._request(method, path, params=params, json=json)
        assert resp is not None  # none_on_404 not used on this path
        try:
            return resp.json()
        except ValueError as exc:
            # Body stays unechoed for the same reason as error bodies above.
            raise ApiError(f"{method} {path} returned a non-JSON response") from exc

    # --- read endpoints -------------------------------------------------

    def me(self) -> dict[str, Any]:
        return self._json("GET", "/me")

    def summary(self, provider: str | None = None) -> dict[str, Any]:
        params = {"provider": provider} if provider else None
        return self._json("GET", "/dashboard/summary", params=params)

    def breakdown(self) -> list[dict[str, Any]]:
        return self._json("GET", "/dashboard/breakdown")

    def providers(self) -> list[dict[str, Any]]:
        return self._json("GET", "/providers")

    # --- vault ----------------------------------------------------------

    def get_vault(self) -> dict[str, Any] | None:
        """Return {jwe, revision, updated_at}, or None if no vault exists."""
        resp = self._request("GET", "/vault", none_on_404=True)
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise ApiError("GET /vault returned a non-JSON response") from exc

    def put_vault(self, jwe: str, expected_revision: int) -> dict[str, Any]:
        try:
            return self._json(
                "PUT",
                "/vault",
                json={"jwe": jwe, "expected_revision": expected_revision},
            )
        except ApiError as exc:
            # 409 is the server's revision compare-and-set failing, which is
            # recoverable by re-reading and re-applying. Give it its own type
            # so callers can't confuse it with a 500.
            if exc.status == 409:
                raise VaultRevisionConflict(
                    "vault was updated elsewhere (revision conflict)"
                ) from exc
            raise

    # --- fetch runs -----------------------------------------------------

    def create_fetch_run(
        self,
        providers: list[str] | None,
        instance_key: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"providers": providers}
        if instance_key is not None:
            body["instance_key"] = instance_key
        return self._json("POST", "/fetch-runs", json=body)

    def submit_responses(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._json("POST", f"/fetch-runs/{run_id}/responses", json=payload)

    def finalize_run(self, run_id: str, cancelled: bool = False) -> dict[str, Any]:
        return self._json(
            "POST", f"/fetch-runs/{run_id}/finalize", json={"cancelled": cancelled}
        )

from __future__ import annotations

from typing import Any, Callable

import httpx
import pytest

from costcompass import api, config, secrets


class FakeStore:
    """In-memory stand-in for the OS credential store."""

    available = True

    def __init__(self) -> None:
        self._items: dict[secrets.SecretItem, str] = {}

    def read(self, item: secrets.SecretItem) -> str | None:
        return self._items.get(item)

    def write(self, item: secrets.SecretItem, value: str) -> None:
        self._items[item] = value

    def delete(self, item: secrets.SecretItem) -> None:
        self._items.pop(item, None)


@pytest.fixture(autouse=True)
def fake_store(monkeypatch) -> FakeStore:
    """Isolate every test from the real OS credential store.

    Autouse on purpose: a test that reaches the real keychain writes the
    developer's own secrets, and relying on each fixture to remember to opt in
    is exactly how that happens. Request this fixture to assert on what was
    stored.
    """
    store = FakeStore()
    monkeypatch.setattr(secrets, "store", lambda: store)
    return store


@pytest.fixture(autouse=True)
def isolate_config(tmp_path_factory, monkeypatch) -> None:
    """Keep every test's config file out of the developer's real ~/.config.

    Individual tests still point XDG_CONFIG_HOME at their own tmp_path; this is
    the backstop for any that forget.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg-backstop")))
    for var in (config.ENV_API_KEY, config.ENV_API_URL, config.ENV_VAULT_PASSWORD):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _stamp_app_server_replies(monkeypatch) -> None:
    """Every httpx.MockTransport built in a test stamps its responses with
    the App-Server identity header by default, since that's what a real
    reply looks like — mirrors the CostCompassKitTesting helper on macOS so
    no individual test file hand-copies the header name. Applies globally
    (not just to api.Client's transport) because dozens of tests across
    test_vault.py and refresh/test_orchestrator*.py build their own
    MockTransport directly rather than going through make_api below.

    A handler tagged ``_unstamped = True`` (see make_api's stamp=False) opts
    out, for the one test that must look like an intermediary's unstamped
    answer.
    """
    original_init = httpx.MockTransport.__init__

    def patched_init(
        self: httpx.MockTransport, handler, *args: Any, **kwargs: Any
    ) -> None:
        if getattr(handler, "_unstamped", False):
            original_init(self, handler, *args, **kwargs)
            return

        def wrapped(request: httpx.Request) -> httpx.Response:
            response = handler(request)
            response.headers.setdefault(api.SERVER_IDENTITY_HEADER, "test-app-server")
            return response

        original_init(self, wrapped, *args, **kwargs)

    monkeypatch.setattr(httpx.MockTransport, "__init__", patched_init)


@pytest.fixture
def make_api() -> Callable[..., api.Client]:
    """Build an api.Client backed by a MockTransport handler.

    Pass stamp=False to build the one case that must look like an
    intermediary's unstamped answer (see _stamp_app_server_replies above).
    """

    def factory(
        handler: Callable[[httpx.Request], httpx.Response], *, stamp: bool = True
    ) -> api.Client:
        if not stamp:
            handler._unstamped = True  # type: ignore[attr-defined]
        http = httpx.Client(transport=httpx.MockTransport(handler))
        return api.Client("https://example.test/api/v1", "sk-test", http=http)

    return factory


@pytest.fixture
def json_router() -> Callable[
    [dict[str, Any]], Callable[[httpx.Request], httpx.Response]
]:
    """Map '{METHOD} {path}' -> json body (or (status, body))."""

    def factory(routes: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
        def handler(request: httpx.Request) -> httpx.Response:
            key = f"{request.method} {request.url.path}"
            if key not in routes:
                return httpx.Response(404, json={"error": "no route"})
            entry = routes[key]
            if isinstance(entry, tuple):
                status, body = entry
            else:
                status, body = 200, entry
            return httpx.Response(status, json=body)

        return handler

    return factory

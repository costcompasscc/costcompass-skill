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


@pytest.fixture
def make_api() -> Callable[[Callable[[httpx.Request], httpx.Response]], api.Client]:
    """Build an api.Client backed by a MockTransport handler."""

    def factory(handler: Callable[[httpx.Request], httpx.Response]) -> api.Client:
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

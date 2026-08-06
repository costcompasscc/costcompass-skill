"""Entry fan-out: the worker pool, and the OAuth guarantees that only become
load-bearing once siblings can be mid-resolution at the same time.

The httpx MockTransport handlers here are URL-routed rather than scripted in
call order, so they stay meaningful when entries interleave — the same property
the macOS suite gets from its `RoutedTransport`.
"""

from __future__ import annotations

import json
import os
import threading

import httpx
import pytest

from costcompass import api, config, vault
from costcompass.refresh import broker, oauth, orchestrator

PASSWORD = "pw"


def _vault_blob(entries):
    doc = {"schema_version": 1, "entries": entries}
    return vault.encrypt_jwe(
        json.dumps(doc).encode(), PASSWORD, os.urandom(16), vault.DEFAULT_PBKDF2_ITERS
    )


class _InFlightMeter:
    """Tracks concurrent handler invocations, so a test can assert the pool is
    genuinely parallel and stays inside its cap."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current = 0
        self.peak = 0

    def __enter__(self) -> "_InFlightMeter":
        with self._lock:
            self.current += 1
            self.peak = max(self.peak, self.current)
        return self

    def __exit__(self, *_exc: object) -> None:
        with self._lock:
            self.current -= 1


def _flat_run(providers: list[str]) -> dict:
    return {
        "run_id": "run-1",
        "fetches": [
            {
                "provider_id": provider,
                "instance_key": "",
                "state": "pending",
                "signing_token": f"tok-{provider}",
                "plan": {
                    "requests": [
                        {
                            "url": f"https://api.{provider}.test/usage",
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
            for provider in providers
        ],
    }


def _run_flat(providers: list[str], concurrency: int, rendezvous: int = 0):
    """Drive a whole run over `providers`, returning (result, meter).

    ``rendezvous`` makes overlap PROVABLE rather than merely likely: each
    broker forward waits until that many are in flight together before any of
    them returns. A sequential orchestrator can never assemble the group, so it
    fails on the barrier timeout instead of passing on a lucky interleaving —
    no sleeps, and nothing that can flake the other way.
    """
    blob = _vault_blob(
        [
            {
                "id": str(index + 1),
                "provider": provider,
                "api_key": f"sk-{provider}",
                "metadata": {"instance_key": ""},
            }
            for index, provider in enumerate(providers)
        ]
    )
    meter = _InFlightMeter()
    barrier = threading.Barrier(rendezvous) if rendezvous else None

    def api_handler(request: httpx.Request) -> httpx.Response:
        p, m = request.url.path, request.method
        if m == "GET" and p.endswith("/vault"):
            return httpx.Response(
                200, json={"jwe": blob, "revision": 1, "updated_at": "x"}
            )
        if m == "POST" and p.endswith("/fetch-runs"):
            return httpx.Response(200, json=_flat_run(providers))
        if "/responses" in p:
            return httpx.Response(200, json={"state": "success", "events_ingested": 1})
        if "/finalize" in p:
            return httpx.Response(
                200, json={"run_id": "run-1", "status": "success", "providers": []}
            )
        if p.endswith("/dashboard/summary"):
            return httpx.Response(200, json={"mtd_usd": 4.0})
        return httpx.Response(404, json={"error": p})

    def broker_handler(_request: httpx.Request) -> httpx.Response:
        # The forward is the only per-entry hop that can overlap; measuring it
        # keeps the sequential prologue/epilogue out of the reading.
        with meter:
            if barrier is not None:
                barrier.wait(timeout=10)
            return httpx.Response(
                200,
                json={"status": 200, "headers": {}, "body": "Yg==", "signature": "s"},
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
    cfg = config.Config(api_key="sk-cli", api_url="https://x/api/v1")

    result = orchestrator.run(
        cfg,
        "sk-cli",
        None,
        PASSWORD,
        client=client,
        broker=brk,
        echo=lambda *_: None,
        concurrency=concurrency,
    )
    return result, meter


# --------------------------------------------------------------------------
# the pool
# --------------------------------------------------------------------------


def test_outcomes_keep_fetches_order_under_fan_out():
    providers = ["alpha", "bravo", "charlie", "delta", "echo"]
    result, _ = _run_flat(providers, concurrency=5)
    assert [o.provider_id for o in result.outcomes] == providers
    assert all(o.state == "success" for o in result.outcomes)


def test_entries_actually_overlap():
    """All five forwards must be in flight at once — unreachable sequentially,
    so the barrier would time out rather than let this pass by luck."""
    result, meter = _run_flat(
        ["alpha", "bravo", "charlie", "delta", "echo"], concurrency=5, rendezvous=5
    )
    assert len(result.outcomes) == 5
    assert meter.peak == 5


def test_pool_never_exceeds_its_cap():
    """Two-sided: the barrier proves at least 2 overlap, the meter proves never
    a third — six entries at width 2 run as three pairs, not as a free-for-all."""
    providers = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
    result, meter = _run_flat(providers, concurrency=2, rendezvous=2)
    assert [o.provider_id for o in result.outcomes] == providers
    assert meter.peak == 2


def test_width_wider_than_the_work_does_not_over_dispatch():
    result, meter = _run_flat(["alpha"], concurrency=5)
    assert len(result.outcomes) == 1
    assert meter.peak == 1


def test_run_with_concurrency_preserves_order_when_workers_finish_backwards():
    """Directly: the later an item's index, the sooner it finishes."""
    items = list(range(8))
    started = threading.Barrier(4)

    def worker(item: int) -> str:
        if item < 4:
            started.wait(timeout=5)
        return f"r{item}"

    assert orchestrator._run_with_concurrency(items, 4, worker) == [
        f"r{i}" for i in items
    ]


def test_run_with_concurrency_surfaces_an_unmodelled_exception():
    """A worker is contracted not to raise; if one does, it must not vanish
    into a dead thread."""

    def worker(item: int) -> int:
        if item == 2:
            raise RuntimeError("boom")
        return item

    with pytest.raises(RuntimeError, match="boom"):
        orchestrator._run_with_concurrency(list(range(6)), 3, worker)


def test_run_with_concurrency_handles_an_empty_work_list():
    assert orchestrator._run_with_concurrency([], 5, lambda item: item) == []


# --------------------------------------------------------------------------
# one grant, one verdict
# --------------------------------------------------------------------------


def _oauth_run() -> dict:
    def entry(instance: str) -> dict:
        return {
            "provider_id": "cloudflare",
            "instance_key": instance,
            "state": "pending",
            "signing_token": f"tok-{instance}",
            "credential": {
                "kind": "oauth_mint",
                "sentinel_key": "__cloudflare_oauth__",
                "mint_path": "/oauth/v1/cloudflare/mint",
            },
            "plan": {
                "requests": [
                    {
                        "url": f"https://api.cloudflare.test/{instance}",
                        "method": "GET",
                        "headers": {},
                        "body": None,
                        "purpose": "usage",
                    }
                ],
                "auth_header": "authorization",
                "auth_scheme": "Bearer",
            },
        }

    return {"run_id": "run-1", "fetches": [entry("acct-a"), entry("acct-b")]}


def test_two_concurrent_cards_on_one_sentinel_mint_and_rotate_once():
    """Both cards miss the access cache at the same moment. Without the
    per-sentinel lock both mint, and the second mint invalidates the rotating
    refresh-token the first card is about to fetch with."""
    blob = _vault_blob(
        [
            {
                "id": "1",
                "provider": "cloudflare",
                "api_key": "refresh-original",
                "metadata": {"instance_key": "__cloudflare_oauth__"},
            }
        ]
    )
    calls = {"mint": 0, "put_vault": 0}
    lock = threading.Lock()

    def api_handler(request: httpx.Request) -> httpx.Response:
        p, m = request.url.path, request.method
        if p.endswith("/vault") and m == "GET":
            return httpx.Response(
                200, json={"jwe": blob, "revision": 1, "updated_at": "x"}
            )
        if p.endswith("/vault") and m == "PUT":
            with lock:
                calls["put_vault"] += 1
            return httpx.Response(200, json={"revision": 2})
        if m == "POST" and p.endswith("/fetch-runs"):
            return httpx.Response(200, json=_oauth_run())
        if "/responses" in p:
            return httpx.Response(200, json={"state": "success", "events_ingested": 1})
        if "/finalize" in p:
            return httpx.Response(
                200, json={"run_id": "run-1", "status": "success", "providers": []}
            )
        if p.endswith("/dashboard/summary"):
            return httpx.Response(200, json={"mtd_usd": 1.0})
        return httpx.Response(404, json={"error": p})

    def oauth_handler(_request: httpx.Request) -> httpx.Response:
        with lock:
            calls["mint"] += 1
        return httpx.Response(
            200,
            json={
                "access_token": "at-1",
                "refresh_token": "refresh-rotated",
                "expires_in": 3600,
            },
        )

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
    oauth_client = oauth.OAuthBrokerClient(
        "https://x/oauth/v1",
        "sk-cli",
        http=httpx.Client(transport=httpx.MockTransport(oauth_handler)),
    )
    cfg = config.Config(api_key="sk-cli", api_url="https://x/api/v1")

    result = orchestrator.run(
        cfg,
        "sk-cli",
        None,
        PASSWORD,
        client=client,
        broker=brk,
        oauth_client=oauth_client,
        echo=lambda *_: None,
        concurrency=5,
    )

    assert len(result.outcomes) == 2
    assert all(o.state == "success" for o in result.outcomes)
    # The load-bearing assertions.
    assert calls["mint"] == 1
    assert calls["put_vault"] == 1


def test_two_providers_rotating_concurrently_are_serialized():
    """The vault-write lock.

    ``_persist_rotated_sentinel`` applies a SPECULATIVE ``entry["api_key"]`` to
    the shared document and restores it in a ``finally`` if the server refuses.
    Overlapping, one thread's ``write_back`` encrypts the other's uncommitted
    edit and persists a rotation the other is about to report as failed.

    The assertion is that the two persists never overlap, not that each upload
    carries a single rotated value: once the first rotation COMMITS, its value
    is legitimately part of the document the second one uploads. Only the
    uncommitted window is the hazard, and serializing is what closes it.
    """
    blob = _vault_blob(
        [
            {
                "id": "1",
                "provider": "cloudflare",
                "api_key": "cf-original",
                "metadata": {"instance_key": "__cloudflare_oauth__"},
            },
            {
                "id": "2",
                "provider": "github",
                "api_key": "gh-original",
                "metadata": {"instance_key": "__github_oauth__"},
            },
        ]
    )
    rotated = {"cloudflare": "cf-rotated", "github": "gh-rotated"}
    uploads: list[dict] = []
    lock = threading.Lock()
    revision = {"n": 1}
    put_meter = _InFlightMeter()
    # Supplies the detection window without a sleep. Each PUT waits here for a
    # second one to join: serialized, none ever does and the barrier breaks on
    # timeout; unserialized, the sibling arrives and both pass straight through
    # — and the meter records the overlap either way.
    put_rendezvous = threading.Barrier(2)

    def entry_for(provider: str, instance: str) -> dict:
        return {
            "provider_id": provider,
            "instance_key": "acct",
            "state": "pending",
            "signing_token": f"tok-{provider}",
            "credential": {
                "kind": "oauth_mint",
                "sentinel_key": instance,
                "mint_path": f"/oauth/v1/{provider}/mint",
            },
            "plan": {
                "requests": [
                    {
                        "url": f"https://api.{provider}.test/usage",
                        "method": "GET",
                        "headers": {},
                        "body": None,
                        "purpose": "usage",
                    }
                ],
                "auth_header": "authorization",
                "auth_scheme": "Bearer",
            },
        }

    def api_handler(request: httpx.Request) -> httpx.Response:
        p, m = request.url.path, request.method
        if p.endswith("/vault") and m == "GET":
            return httpx.Response(
                200, json={"jwe": blob, "revision": revision["n"], "updated_at": "x"}
            )
        if p.endswith("/vault") and m == "PUT":
            with put_meter:
                body = json.loads(request.content)
                plaintext, _, _ = vault.decrypt_jwe(body["jwe"], PASSWORD)
                doc = json.loads(bytes(plaintext))
                try:
                    put_rendezvous.wait(timeout=1.0)
                except threading.BrokenBarrierError:
                    pass  # Expected when serialized: no sibling to meet.
                with lock:
                    uploads.append(
                        {e["provider"]: e["api_key"] for e in doc["entries"]}
                    )
                    revision["n"] += 1
                return httpx.Response(200, json={"revision": revision["n"]})
        if m == "POST" and p.endswith("/fetch-runs"):
            return httpx.Response(
                200,
                json={
                    "run_id": "run-1",
                    "fetches": [
                        entry_for("cloudflare", "__cloudflare_oauth__"),
                        entry_for("github", "__github_oauth__"),
                    ],
                },
            )
        if "/responses" in p:
            return httpx.Response(200, json={"state": "success", "events_ingested": 1})
        if "/finalize" in p:
            return httpx.Response(
                200, json={"run_id": "run-1", "status": "success", "providers": []}
            )
        if p.endswith("/dashboard/summary"):
            return httpx.Response(200, json={"mtd_usd": 2.0})
        return httpx.Response(404, json={"error": p})

    def oauth_handler(request: httpx.Request) -> httpx.Response:
        provider = "cloudflare" if "cloudflare" in request.url.path else "github"
        return httpx.Response(
            200,
            json={
                "access_token": f"at-{provider}",
                "refresh_token": rotated[provider],
                "expires_in": 3600,
            },
        )

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
    oauth_client = oauth.OAuthBrokerClient(
        "https://x/oauth/v1",
        "sk-cli",
        http=httpx.Client(transport=httpx.MockTransport(oauth_handler)),
    )
    cfg = config.Config(api_key="sk-cli", api_url="https://x/api/v1")

    result = orchestrator.run(
        cfg,
        "sk-cli",
        None,
        PASSWORD,
        client=client,
        broker=brk,
        oauth_client=oauth_client,
        echo=lambda *_: None,
        concurrency=5,
    )

    assert all(o.state == "success" for o in result.outcomes)
    # Both rotations really happened — otherwise "never overlapped" is vacuous.
    assert len(uploads) == 2
    assert put_meter.peak == 1, "two rotation write-backs were in flight at once"
    # Both rotations landed, each in its own write.
    assert uploads[-1]["cloudflare"] == rotated["cloudflare"]
    assert uploads[-1]["github"] == rotated["github"]

"""The run budget: a run gives up on its own rather than holding the client
hostage to a throttled provider.

Mirrors the browser reference's "run budget" suite case for case. The clock is
injected rather than faked globally, and advanced from inside a request handler
so the jump lands at a chosen seam instead of an arbitrary moment.
"""

from __future__ import annotations

import json
import os
import threading

import httpx
import pytest

from costcompass import api, config, vault
from costcompass.refresh import broker, orchestrator

PASSWORD = "pw"
BUDGET_S = 900.0


def _vault_blob(entries):
    doc = {"schema_version": 1, "entries": entries}
    return vault.encrypt_jwe(
        json.dumps(doc).encode(), PASSWORD, os.urandom(16), vault.DEFAULT_PBKDF2_ITERS
    )


class _Clock:
    """A wall clock the test drives by hand."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._t = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._t

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._t += seconds


def _entry(provider: str, purposes: list[str]) -> dict:
    # Every request gets a DISTINCT url, keyed on its purpose. A fan-out plan's
    # requests differ by URL in real life (one per day), and the deadline stubs
    # are only attributable to a specific day because each carries its own
    # request's URL — a fixture that reused one URL could not tell that apart
    # from every stub copying the first request's.
    return {
        "provider_id": provider,
        "instance_key": "",
        "state": "pending",
        "signing_token": f"tok-{provider}",
        "plan": {
            "requests": [
                {
                    "url": f"https://api.{provider}.test/{purpose}",
                    "method": "GET",
                    "headers": {},
                    "body": None,
                    "purpose": purpose,
                }
                for purpose in purposes
            ],
            "auth_header": "x-api-key",
            "auth_scheme": None,
        },
    }


class _Harness:
    """One run over `providers`, recording what actually reached the server."""

    def __init__(
        self,
        providers: list[str],
        purposes: list[str] | None = None,
        finalize_status: int = 200,
    ) -> None:
        self.providers = providers
        self.purposes = purposes or ["usage"]
        self.finalize_status = finalize_status
        self.clock = _Clock()
        self.submitted: list[dict] = []
        self.finalize_bodies: list[dict] = []
        self.forwards = 0
        self._lock = threading.Lock()
        self.on_forward = lambda count: None
        self.on_submit = lambda: None

    def _api(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "GET" and path.endswith("/vault"):
            blob = _vault_blob(
                [
                    {
                        "id": str(i + 1),
                        "provider": p,
                        "api_key": f"sk-{p}",
                        "metadata": {"instance_key": ""},
                    }
                    for i, p in enumerate(self.providers)
                ]
            )
            return httpx.Response(
                200, json={"jwe": blob, "revision": 1, "updated_at": "x"}
            )
        if method == "POST" and path.endswith("/fetch-runs"):
            return httpx.Response(
                200,
                json={
                    "run_id": "run-1",
                    "fetches": [_entry(p, self.purposes) for p in self.providers],
                },
            )
        if "/responses" in path:
            with self._lock:
                self.submitted.append(json.loads(request.content))
            self.on_submit()
            return httpx.Response(200, json={"state": "success", "events_ingested": 1})
        if "/finalize" in path:
            with self._lock:
                self.finalize_bodies.append(json.loads(request.content))
            if self.finalize_status != 200:
                return httpx.Response(self.finalize_status, json={"error": "nope"})
            return httpx.Response(
                200, json={"run_id": "run-1", "status": "success", "providers": []}
            )
        if path.endswith("/dashboard/summary"):
            return httpx.Response(200, json={"mtd_usd": 4.0})
        return httpx.Response(404, json={"error": path})

    def _broker(self, _request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.forwards += 1
            count = self.forwards
        self.on_forward(count)
        return httpx.Response(
            200,
            json={"status": 200, "headers": {}, "body": "Yg==", "signature": "s"},
        )

    def run(self, concurrency: int = 1):
        client = api.Client(
            "https://x/api/v1",
            "sk-cli",
            http=httpx.Client(transport=httpx.MockTransport(self._api)),
        )
        brk = broker.BrokerClient(
            "https://x/broker/v1",
            "sk-cli",
            http=httpx.Client(transport=httpx.MockTransport(self._broker)),
        )
        return orchestrator.run(
            config.Config(api_key="sk-cli", api_url="https://x/api/v1"),
            "sk-cli",
            None,
            PASSWORD,
            client=client,
            broker=brk,
            echo=lambda *_: None,
            concurrency=concurrency,
            now=self.clock,
            run_budget_s=BUDGET_S,
        )

    def submitted_providers(self) -> list[str]:
        return [s["provider_id"] for s in self.submitted]


def test_entry_pool_stops_and_finalizes_cancelled():
    """Budget spent after entry 1 of 3: entry 1 submitted, 2 and 3 never
    claimed, run finalized as cancelled and the deadline surfaced."""
    h = _Harness(["deepseek", "openai", "anthropic"])
    # Burn the budget once the first card has submitted, so the stop lands
    # exactly at an entry seam.
    h.on_submit = lambda: h.clock.advance(BUDGET_S * 2)

    with pytest.raises(orchestrator.RefreshDeadlineExceeded):
        # concurrency=1 so "2 and 3 were never claimed" is a fact about the
        # budget rather than a race. The concurrent drain has its own test.
        h.run(concurrency=1)

    assert h.submitted_providers() == ["deepseek"]
    assert h.finalize_bodies == [{"cancelled": True}]


def test_request_loop_banks_siblings_and_stubs_the_tail():
    """Budget spent inside one plan: the requests it cut off become synthetic
    stubs, and the response that DID arrive is still submitted.

    A non-synthetic stub would reach ``plugin.process()``, whose contract makes
    it raise on a 5xx — taking the fetched sibling down with it. Honesty about
    the truncation comes from the server's window-completeness verdict instead,
    which is what makes banking the sibling safe.
    """
    h = _Harness(["deepseek"], purposes=["usage_1", "usage_2", "usage_3"])
    # Expire after request 1 returns, so request 2 is never issued.
    h.on_forward = lambda count: h.clock.advance(BUDGET_S * 2) if count == 1 else None

    # The run RESOLVES rather than raising, and finalizes cancelled=False. That
    # is the intended split: the deadline landed inside the only entry, so every
    # entry was still attempted and none was left pending server-side.
    # RefreshDeadlineExceeded is reserved for work never attempted, which is
    # what the cancelling finalize cleans up.
    h.run(concurrency=1)

    assert h.forwards == 1
    responses = h.submitted[0]["responses"]
    # The relayed response survives — the whole point — followed by one stub
    # per abandoned request.
    assert len(responses) == 3
    assert responses[0]["request_purpose"] == "usage_1"
    assert responses[0].get("synthetic") is not True

    for stub, purpose in zip(responses[1:], ["usage_2", "usage_3"]):
        assert stub["status"] == 504
        # Synthetic, so the App Server strips it before plugin.process() and
        # the sibling above still ingests.
        assert stub["synthetic"] is True
        # No reason marker: the server's marker short-circuits are for an
        # entry whose SOLE response is synthetic, and would mislabel this one.
        assert "synthetic_reason" not in stub
        assert "signature" not in stub
        # ITS OWN planned URL, not a cc-internal:// sentinel and not a sibling's
        # — that is what tells the server which segment is missing so it can
        # re-plan exactly that one.
        assert stub["request_url"] == f"https://api.deepseek.test/{purpose}"
        assert stub["request_purpose"] == purpose

    assert h.finalize_bodies == [{"cancelled": False}]


def test_request_loop_stubs_every_abandoned_request():
    """One stub per cut-off request, each with its own purpose — not a single
    stub for the entry. A per-day fan-out needs every missing day named or the
    next refresh cannot tell which ones to re-plan."""
    purposes = [f"usage_{i}" for i in range(1, 6)]
    h = _Harness(["deepseek"], purposes=purposes)
    h.on_forward = lambda count: h.clock.advance(BUDGET_S * 2) if count == 2 else None

    h.run(concurrency=1)

    assert h.forwards == 2
    responses = h.submitted[0]["responses"]
    assert [r["request_purpose"] for r in responses] == purposes
    # Each stub is built from ITS OWN abandoned request, so the urls track the
    # purposes one-for-one rather than every stub repeating the cursor's.
    assert [r["request_url"] for r in responses] == [
        f"https://api.deepseek.test/{p}" for p in purposes
    ]
    assert [r.get("synthetic") is True for r in responses] == [
        False,
        False,
        True,
        True,
        True,
    ]


def test_under_budget_is_untouched():
    """A clock that never advances reproduces the happy path exactly."""
    h = _Harness(["deepseek", "openai", "anthropic"])

    result = h.run(concurrency=1)

    assert sorted(h.submitted_providers()) == ["anthropic", "deepseek", "openai"]
    assert h.finalize_bodies == [{"cancelled": False}]
    assert len(result.outcomes) == 3


def test_work_in_flight_is_never_interrupted():
    """The entry already claimed when the deadline passes still completes and
    still submits — abandoning it is what would manufacture the half-done state
    the budget exists to prevent."""
    h = _Harness(["deepseek", "openai", "anthropic"])
    # Expire mid-fetch, between the broker call and the submit.
    h.on_forward = lambda count: h.clock.advance(BUDGET_S * 2) if count == 1 else None

    with pytest.raises(orchestrator.RefreshDeadlineExceeded):
        h.run(concurrency=1)

    assert h.submitted_providers() == ["deepseek"]


def test_concurrent_drain_is_consistent():
    """With several workers in flight, every claimed entry submits, no
    unclaimed one does, and finalize runs after the last worker joins."""
    providers = [f"p{i}" for i in range(8)]
    h = _Harness(providers)
    h.on_forward = lambda count: h.clock.advance(BUDGET_S * 2) if count == 3 else None

    with pytest.raises(orchestrator.RefreshDeadlineExceeded):
        h.run(concurrency=3)

    submitted = h.submitted_providers()
    # No entry submitted twice, and every forward led to exactly one submit —
    # the sliced prefix has no holes and nothing was dropped.
    assert len(set(submitted)) == len(submitted)
    assert len(submitted) == h.forwards
    # The run genuinely stopped short.
    assert len(submitted) < len(providers)
    assert h.finalize_bodies == [{"cancelled": True}]


def test_deadline_still_raised_when_the_cancelling_finalize_fails():
    """The deadline is the cause worth reporting; the server's lease reaps the
    row whether or not this finalize landed."""
    h = _Harness(["deepseek", "openai", "anthropic"], finalize_status=500)
    h.on_submit = lambda: h.clock.advance(BUDGET_S * 2)

    with pytest.raises(orchestrator.RefreshDeadlineExceeded):
        h.run(concurrency=1)

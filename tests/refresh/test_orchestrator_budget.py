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
        # Every sleep the retry loop actually took. The retry seam's whole
        # claim is that a wait too big for the budget is never taken, and
        # "no forward happened" cannot tell that apart from a wait that was
        # taken and then gave up.
        self.sleeps: list[float] = []
        # What the wall clock did WHILE a sleep ran. Default nothing; a test
        # overrides it to model a host suspended mid-backoff, which is the only
        # way a wait the budget could afford still lands past the deadline.
        self.on_sleep = lambda seconds: None
        # The broker envelope for forward N (1-based). Default is a healthy
        # relayed 200; a test overrides it to throttle a chosen request.
        self.broker_reply = lambda count: {
            "status": 200,
            "headers": {},
            "body": "Yg==",
            "signature": "s",
        }

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
        return httpx.Response(200, json=self.broker_reply(count))

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.on_sleep(seconds)

    def run(self, concurrency: int = 1, run_budget_s: float = BUDGET_S):
        client = api.Client(
            "https://x/api/v1",
            "sk-cli",
            http=httpx.Client(transport=httpx.MockTransport(self._api)),
        )
        brk = broker.BrokerClient(
            "https://x/broker/v1",
            "sk-cli",
            http=httpx.Client(transport=httpx.MockTransport(self._broker)),
            # Base 0 so a recorded sleep is the Retry-After the provider asked
            # for and nothing else, and the sleep is recorded rather than taken
            # — the suite must not spend real seconds to observe one.
            retry_base_s=0.0,
            sleep=self._sleep,
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
            run_budget_s=run_budget_s,
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


def _throttled(seconds: int) -> dict:
    """An upstream 429 relayed through the broker, asking for `seconds` of wait."""
    return {
        "status": 429,
        "headers": {"retry-after": str(seconds)},
        "body": "Yg==",
        "signature": "s",
    }


def test_retry_backoff_never_sleeps_past_the_deadline():
    """The seam the other two cannot reach.

    Before this existed the loop slept the full Retry-After and forwarded again
    with no deadline in sight — ``RETRY_ATTEMPTS`` times over, at a ten-minute
    cap, is ~40 minutes past a fifteen-minute budget, every second of it holding
    the refresh lock.
    """
    h = _Harness(["deepseek"], purposes=["usage_1"])
    h.broker_reply = lambda count: _throttled(600)

    # Ten minutes of requested backoff against five minutes of budget. Stated as
    # a short budget rather than an advanced clock so the wait is refused on the
    # FIRST retry, with nothing else having happened.
    h.run(concurrency=1, run_budget_s=300.0)

    # One forward, and — the actual bug — no sleep at all. Sleeping right up to
    # the deadline and only then giving up spends the budget to learn nothing,
    # so the wait is refused before it is taken.
    assert h.forwards == 1
    assert h.sleeps == []

    # Banked as the same stub a request the budget never issued gets: from the
    # server's side they are one event, a planned segment nobody fetched.
    responses = h.submitted[0]["responses"]
    assert len(responses) == 1
    assert responses[0]["status"] == 504
    assert responses[0]["synthetic"] is True
    assert responses[0]["request_url"] == "https://api.deepseek.test/usage_1"
    assert responses[0]["request_purpose"] == "usage_1"


def test_retry_whose_backoff_fits_is_still_taken():
    """The other side of the check: a one-second Retry-After inside the full
    budget behaves exactly as it did before, or the fix has traded a rare
    overrun for routine lost recovery."""
    h = _Harness(["deepseek"], purposes=["usage_1"])
    h.broker_reply = lambda count: (
        _throttled(1)
        if count == 1
        else {"status": 200, "headers": {}, "body": "Yg==", "signature": "s"}
    )

    h.run(concurrency=1)

    assert h.forwards == 2
    assert h.sleeps == [1.0]
    responses = h.submitted[0]["responses"]
    assert len(responses) == 1
    assert responses[0].get("synthetic") is not True


def test_retry_gives_up_when_the_sleep_itself_overruns_the_deadline():
    """The prediction made before the sleep is not a guarantee.

    A sleep duration is a floor: the host can suspend mid-backoff and stop the
    clock, so a wait the budget could comfortably afford wakes far past the
    deadline — and machine suspend is a case the run budget exists to catch.
    Waking late and forwarding anyway is how a retry escapes a budget it just
    passed.
    """
    h = _Harness(["deepseek"], purposes=["usage_1"])
    h.broker_reply = lambda count: _throttled(1)
    # A one-second wait the pre-check accepts, against a machine that was asleep
    # for an hour while it ran.
    h.on_sleep = lambda seconds: h.clock.advance(3600.0)

    h.run(concurrency=1)

    # The sleep was taken — it was affordable when asked for — but the forward
    # on the far side of it is not issued.
    assert h.sleeps == [1.0]
    assert h.forwards == 1
    responses = h.submitted[0]["responses"]
    assert len(responses) == 1
    assert responses[0]["status"] == 504
    assert responses[0]["synthetic"] is True
    assert responses[0]["request_purpose"] == "usage_1"


def test_only_the_request_whose_backoff_did_not_fit_is_written_off():
    """One provider asking for ten minutes has not spent the run's budget.

    The alternative — treating an oversized backoff as "the run is over" —
    throws away days there was still time to fetch, and the request seam is
    already there to end the loop once the budget genuinely is gone.
    """
    h = _Harness(["deepseek"], purposes=["usage_1", "usage_2"])
    # Requests within a plan are issued in order, so a counter names them: the
    # first day is throttled, the second is healthy.
    h.broker_reply = lambda count: (
        _throttled(600)
        if count == 1
        else {"status": 200, "headers": {}, "body": "Yg==", "signature": "s"}
    )

    h.run(concurrency=1, run_budget_s=300.0)

    assert h.forwards == 2
    responses = h.submitted[0]["responses"]
    assert [r["request_purpose"] for r in responses] == ["usage_1", "usage_2"]
    assert [r.get("synthetic") is True for r in responses] == [True, False]


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
    """The deadline is the cause worth reporting, and the run is the client's to
    abandon whether or not this finalize landed. The row is then stranded
    ``running`` until the retention horizon — there is no server-side reaper."""
    h = _Harness(["deepseek", "openai", "anthropic"], finalize_status=500)
    h.on_submit = lambda: h.clock.advance(BUDGET_S * 2)

    with pytest.raises(orchestrator.RefreshDeadlineExceeded):
        h.run(concurrency=1)

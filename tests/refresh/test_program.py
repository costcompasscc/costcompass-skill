from __future__ import annotations

import base64
import json

import pytest

from costcompass.refresh import program
from costcompass.refresh.broker import BrokerForwardCapError


class FakeBroker:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def forward(self, request):
        self.requests.append(request)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def env(status, obj):
    return {
        "status": status,
        "headers": {},
        "body": base64.b64encode(json.dumps(obj).encode()).decode(),
        "signature": "s",
    }


def _bq_program():
    return {
        "purpose": "billing",
        "bindings": {},
        "steps": [
            {
                "kind": "request",
                "request": {"host": "bq", "path": "/q", "method": "POST", "query": {}, "headers": {}, "body": None, "purpose": "billing", "req_sig": "sig"},
                "extract": [{"path": "jobId", "as_binding": "jobId", "coerce": "string"}],
                "stop_on_status_gte": 400,
            },
            {
                "kind": "poll",
                "request": {"host": "bq", "path": "/jobs/{jobId}", "method": "GET", "query": {}, "headers": {}, "purpose": "billing"},
                "until": {"any_of": [{"kind": "falsey", "binding": "done"}]},
                "extract": [{"path": "done", "as_binding": "done", "coerce": "bool"}],
                "require_bindings": ["jobId"],
                "deadline_ms": 90_000,
                "max_iterations": 64,
                "stop_on_status_gte": 400,
            },
        ],
    }


def test_request_then_poll_loop():
    broker = FakeBroker([
        env(200, {"jobId": "abc"}),
        env(200, {"done": False}),
        env(200, {"done": True}),
    ])
    out = program.run_program(_bq_program(), {"Authorization": "Bearer t"}, "tok", broker)
    assert len(out) == 3
    # jobId substituted into the poll path
    poll_paths = [r["target"]["path"] for r in broker.requests if r["target"]["method"] == "GET"]
    assert poll_paths == ["/jobs/abc", "/jobs/abc"]
    # req_sig relayed verbatim on the first (signed) step
    assert broker.requests[0]["req_sig"] == "sig"


def test_stop_on_status_terminates_without_extract():
    broker = FakeBroker([env(500, {"jobId": "abc"})])
    out = program.run_program(_bq_program(), {}, "tok", broker)
    assert len(out) == 1  # poll step never runs
    assert out[0]["status"] == 500


def test_path_traversal_guard():
    broker = FakeBroker([env(200, {"jobId": "../evil"})])
    out = program.run_program(_bq_program(), {}, "tok", broker)
    assert out[-1]["status"] == 502
    assert out[-1]["request_url"] == "cc-internal://cli-refresh"


def test_poll_deadline_emits_504():
    clock_values = iter([0.0, 100_000.0, 200_000.0])
    broker = FakeBroker([env(200, {"jobId": "abc"})])
    out = program.run_program(_bq_program(), {}, "tok", broker, now=lambda: next(clock_values))
    assert out[-1]["status"] == 504
    assert "deadline" in json.loads(base64.b64decode(out[-1]["body_b64"]))["error"]


def test_forward_cap_propagates():
    broker = FakeBroker([BrokerForwardCapError(100, 100)])
    with pytest.raises(BrokerForwardCapError):
        program.run_program(_bq_program(), {}, "tok", broker)


def test_js_string_coercion_matches_browser():
    assert program._js_string(True) == "true"
    assert program._js_string(False) == "false"
    assert program._js_string(None) == ""
    assert program._js_string(1.0) == "1"      # JS: String(1.0) === "1"
    assert program._js_string(1.5) == "1.5"
    assert program._js_string(42) == "42"
    assert program._js_string("abc") == "abc"

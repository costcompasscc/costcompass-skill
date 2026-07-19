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
                "request": {
                    "host": "bq",
                    "path": "/q",
                    "method": "POST",
                    "query": {},
                    "headers": {},
                    "body": None,
                    "purpose": "billing",
                    "req_sig": "sig",
                },
                "extract": [
                    {"path": "jobId", "as_binding": "jobId", "coerce": "string"}
                ],
                "stop_on_status_gte": 400,
            },
            {
                "kind": "poll",
                "request": {
                    "host": "bq",
                    "path": "/jobs/{jobId}",
                    "method": "GET",
                    "query": {},
                    "headers": {},
                    "purpose": "billing",
                },
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
    broker = FakeBroker(
        [
            env(200, {"jobId": "abc"}),
            env(200, {"done": False}),
            env(200, {"done": True}),
        ]
    )
    out = program.run_program(
        _bq_program(), {"Authorization": "Bearer t"}, "tok", broker
    )
    assert len(out) == 3
    # jobId substituted into the poll path
    poll_paths = [
        r["target"]["path"] for r in broker.requests if r["target"]["method"] == "GET"
    ]
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
    out = program.run_program(
        _bq_program(), {}, "tok", broker, now=lambda: next(clock_values)
    )
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
    assert program._js_string(1.0) == "1"  # JS: String(1.0) === "1"
    assert program._js_string(1.5) == "1.5"
    assert program._js_string(42) == "42"
    assert program._js_string("abc") == "abc"


def test_js_string_matches_browser_for_arrays_and_objects():
    # Node-verified: String([1,"a",null]) === "1,a,"; nested arrays flatten
    # via Array.prototype.join; String({}) === "[object Object]".
    assert program._js_string([1, "a", None]) == "1,a,"
    assert program._js_string([[1, 2], [3]]) == "1,2,3"
    assert program._js_string([None]) == ""
    assert program._js_string([True, False]) == "true,false"
    assert program._js_string({}) == "[object Object]"
    assert program._js_string([{}]) == "[object Object]"


def test_js_string_matches_browser_number_notation():
    # Node-verified ECMA notation: decimal through (1e-6, 1e21), unpadded
    # exponents outside, shortest digits zero-extended above 2**53 (the
    # double's exact integer value is NOT what JS prints).
    assert program._js_string(0.00001) == "0.00001"  # repr: '1e-05'
    assert program._js_string(0.000001) == "0.000001"  # repr: '1e-06'
    assert program._js_string(1.5e-7) == "1.5e-7"  # repr: '1.5e-07'
    assert program._js_string(-1.5e-7) == "-1.5e-7"
    assert program._js_string(5e-324) == "5e-324"
    assert program._js_string(1e21) == "1e+21"
    assert program._js_string(1e18) == "1000000000000000000"
    assert program._js_string(1000000000000000128) == "1000000000000000100"
    assert program._js_string(1.2345678901234568e20) == "123456789012345680000"


def test_non_standard_json_constants_make_the_body_unparseable():
    # CPython's json accepts bare Infinity/-Infinity/NaN; JSON.parse rejects
    # them, so the browser reference falls into its catch and every binding
    # from that body becomes "". Match it, rather than extracting an "inf"
    # no other relay can produce.
    for literal in ("Infinity", "-Infinity", "NaN"):
        bindings: dict[str, str] = {}
        body = base64.b64encode(f'{{"n": {literal}, "jobId": "abc"}}'.encode()).decode()
        program._apply_extract(
            [
                {"path": "n", "as_binding": "n"},
                {"path": "jobId", "as_binding": "jobId"},
            ],
            body,
            bindings,
        )
        # Not just the offending value — the WHOLE body is unparseable,
        # so the sibling binding is empty too.
        assert bindings == {"n": "", "jobId": ""}, literal


def test_exponent_overflow_matches_browser_infinity():
    # 1e400 is VALID JSON, so parse_constant never sees it: CPython yields
    # inf and repr spells it "inf" where JS String() gives "Infinity".
    # Node-verified: JSON.parse('1e400') === Infinity, String(...) ===
    # 'Infinity'. The Swift port agrees via its own overflow-to-infinity.
    bindings: dict[str, str] = {}
    body = base64.b64encode(b'{"big": 1e400, "small": -1e400}').decode()
    program._apply_extract(
        [
            {"path": "big", "as_binding": "big"},
            {"path": "small", "as_binding": "small"},
        ],
        body,
        bindings,
    )
    assert bindings == {"big": "Infinity", "small": "-Infinity"}


def test_js_number_string_non_finite_spellings():
    assert program._js_number_string(float("inf")) == "Infinity"
    assert program._js_number_string(float("-inf")) == "-Infinity"
    assert program._js_number_string(float("nan")) == "NaN"

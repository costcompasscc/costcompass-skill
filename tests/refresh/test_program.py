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
    # them, so the browser reference falls into its catch. Match it, rather
    # than extracting an "inf" no other relay can produce — and report the
    # failure rather than binding "" for every rule.
    for literal in ("Infinity", "-Infinity", "NaN"):
        bindings: dict[str, str] = {}
        body = base64.b64encode(f'{{"n": {literal}, "jobId": "abc"}}'.encode()).decode()
        ok = program._apply_extract(
            [
                {"path": "n", "as_binding": "n"},
                {"path": "jobId", "as_binding": "jobId"},
            ],
            body,
            bindings,
        )
        # Not just the offending value — the WHOLE body is unreadable, and
        # bindings are left untouched rather than filled with "".
        assert ok is False, literal
        assert bindings == {}, literal


def test_apply_extract_leaves_bindings_untouched_on_a_non_json_body():
    bindings = {"prior": "kept"}
    body = base64.b64encode(b"<html>not json</html>").decode()
    ok = program._apply_extract([{"path": "a", "as_binding": "a"}], body, bindings)
    assert ok is False
    assert bindings == {"prior": "kept"}


def test_apply_extract_with_no_rules_tolerates_a_non_json_body():
    bindings: dict[str, str] = {}
    body = base64.b64encode(b"<html>not json</html>").decode()
    assert program._apply_extract([], body, bindings) is True
    assert bindings == {}


# Every input the three relays used to DISAGREE on, plus the one they always
# agreed on. Each is now rejected by all three under the shared decode contract
# (`program._decode_body_strict`), and `tests/vectors/relay/program.json`
# carries the same set so the ports cannot drift apart again.
#
# The old-behaviour note is what makes each case worth its line — a case all
# three already rejected (`!!!not-base64!!!`) pins nothing, which is exactly
# why the one pre-existing invalid-base64 test anywhere in the three relays
# (macOS's, using that string) could not catch this bug.
#
# SCOPE: these assert INTERPRETER OUTPUT, not the end-to-end outcome, and the
# two diverge for the base64-invalid rows. There the relayed response queued
# alongside the 502 carries the malformed body verbatim, so ``RawResponseIn``
# 422s the whole ``/responses`` batch and the server never reads the
# provider-error; the entry lands on its ``failed`` fallback instead. Still a
# red card, different route. That ordering predates this contract — do not
# "fix" it by weakening these assertions.
OUTSIDE_THE_DECODE_CONTRACT = [
    ("e30=!!", "excess data after padding — THIS relay accepted, atob/Swift rejected"),
    ("e3 0=", "embedded space — THIS relay and atob accepted, Swift rejected"),
    ("e30", "unpadded — only the browser accepted"),
    ("e30==", "over-padded — THIS relay and Swift accepted, atob rejected"),
    ("!!!not-base64!!!", "all three already rejected — the control case"),
    ("eyJhIjoi/yJ9", '{"a":"<0xFF>"} — the browser bound U+FFFD and kept going'),
    ("eyJhIjoiXHVkODAwIn0=", "lone \\ud800 escape — Swift cannot represent one"),
    ("eyJhIjoiASJ9", "raw 0x01 in a string — only Swift accepted (RFC 8259 §7)"),
    ("77u/e30=", "leading BOM — only the browser accepted"),
    # Not a relay divergence — a near-miss in the GUARD. Python's `$`
    # also matches before a trailing newline, so the obvious spelling of
    # _CANONICAL_BASE64 accepted this while the browser and Swift
    # rejected it, recreating the very divergence the guard removes.
    ("e30=\n", "trailing newline — `$` would accept it, `\\Z` does not"),
]


@pytest.mark.parametrize("body,why", OUTSIDE_THE_DECODE_CONTRACT)
def test_apply_extract_refuses_bodies_outside_the_decode_contract(body, why):
    bindings = {"prior": "kept"}
    ok = program._apply_extract([{"path": "a", "as_binding": "a"}], body, bindings)
    assert ok is False, why
    assert bindings == {"prior": "kept"}, why  # untouched, as on any decode failure


def test_apply_extract_accepts_the_one_canonical_spelling():
    # `e30=` is the single row of the divergence matrix every relay always
    # agreed on, and the only one the App Server's own
    # `b64decode(validate=True)` accepts. Guards against "fixing" the
    # divergence by making everything fail.
    bindings: dict[str, str] = {}
    assert program._apply_extract([{"path": "a", "as_binding": "a"}], "e30=", bindings)
    assert bindings == {"a": ""}  # absent path binds "", which is not a failure


def test_apply_extract_refuses_a_body_past_the_depth_cap_without_crashing():
    # Before the cap this raised RecursionError — which is NOT a ValueError, so
    # it escaped `_apply_extract`'s except and killed the process. macOS was
    # worse still (an uncatchable SIGSEGV in its recursive-descent parser).
    # That this merely returns False is the whole point of the test.
    deep = base64.b64encode(b"[" * 50_000 + b"]" * 50_000).decode()
    bindings: dict[str, str] = {}
    ok = program._apply_extract([{"path": "a", "as_binding": "a"}], deep, bindings)
    assert ok is False
    assert bindings == {}


def test_depth_scan_does_not_count_brackets_inside_a_string():
    # The scan must not count string CONTENT — a provider error message full of
    # `[` is a legitimate body, and rejecting it would turn this guard into its
    # own outage.
    noisy = base64.b64encode(json.dumps({"a": "[" * 200}).encode()).decode()
    bindings: dict[str, str] = {}
    assert program._apply_extract([{"path": "a", "as_binding": "a"}], noisy, bindings)
    assert bindings == {"a": "[" * 200}


def test_apply_extract_binds_empty_for_an_absent_path():
    # google's `pageToken` and atlascloud's `next_page` are legitimately
    # absent on the last page. Only an unreadable BODY is an error.
    bindings: dict[str, str] = {}
    body = base64.b64encode(b'{"present": "x"}').decode()
    ok = program._apply_extract(
        [{"path": "pageToken", "as_binding": "page"}], body, bindings
    )
    assert ok is True
    assert bindings == {"page": ""}


def _poll_only_program(bindings, require_bindings):
    """A single poll step; bindings and require_bindings are the knobs."""
    return {
        "purpose": "billing",
        "bindings": bindings,
        "steps": [
            {
                "kind": "poll",
                "request": {
                    "host": "bq",
                    "path": "/pages",
                    "method": "GET",
                    "query": {"cursor": "{cursor}"},
                    "headers": {},
                    "purpose": "billing",
                },
                "until": {"any_of": [{"kind": "truthy", "binding": "more"}]},
                "extract": [
                    {"path": "has_more", "as_binding": "more", "coerce": "bool"},
                    {"path": "next_page", "as_binding": "cursor"},
                ],
                "require_bindings": require_bindings,
                "deadline_ms": 90_000,
                "max_iterations": 64,
                "stop_on_status_gte": 400,
            }
        ],
    }


def _error_detail(resp):
    return json.loads(base64.b64decode(resp["body_b64"]))["error"]


def test_require_binding_missing_emits_502_and_never_forwards():
    broker = FakeBroker([])  # any forward would raise
    out = program.run_program(
        _poll_only_program({"more": "true"}, ["cursor"]), {}, "tok", broker
    )
    assert broker.requests == []
    assert len(out) == 1
    assert out[0]["status"] == 502
    assert out[0].get("signature") is None
    assert not out[0].get("synthetic")
    assert _error_detail(out[0]) == "required binding missing: cursor"


def test_require_binding_names_the_first_missing_in_declaration_order():
    broker = FakeBroker([])
    out = program.run_program(
        _poll_only_program({"more": "true", "b": "set"}, ["a", "b", "c"]),
        {},
        "tok",
        broker,
    )
    assert _error_detail(out[0]) == "required binding missing: a"


def test_require_binding_cleared_mid_loop_still_emits_502():
    # atlascloud's truncation shape: page 1 hands back a cursor, page 2 still
    # claims has_more but omits it. The check must fire on iteration N.
    broker = FakeBroker(
        [
            env(200, {"has_more": True, "next_page": "p2"}),
            env(200, {"has_more": True}),
        ]
    )
    out = program.run_program(
        _poll_only_program({"more": "true", "cursor": "p1"}, ["cursor"]),
        {},
        "tok",
        broker,
    )
    assert len(broker.requests) == 2  # both pages fetched, then the cursor is gone
    assert len(out) == 3  # two relayed pages + the error
    assert out[2]["status"] == 502
    assert _error_detail(out[2]) == "required binding missing: cursor"


def test_undecodable_body_emits_502_and_terminates_before_the_next_step():
    prog = {
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
                    "purpose": "billing",
                },
                "extract": [{"path": "jobId", "as_binding": "jobId"}],
                "stop_on_status_gte": 400,
            },
            {
                "kind": "request",
                "request": {
                    "host": "bq",
                    "path": "/never",
                    "method": "GET",
                    "query": {},
                    "headers": {},
                    "purpose": "billing",
                },
                "extract": [],
                "stop_on_status_gte": 400,
            },
        ],
    }
    broker = FakeBroker(
        [
            {
                "status": 200,
                "headers": {},
                "body": base64.b64encode(b"<html>not json</html>").decode(),
                "signature": "s",
            }
        ]
    )
    out = program.run_program(prog, {}, "tok", broker)

    assert len(broker.requests) == 1  # the second step never ran
    assert len(out) == 2  # the relayed 200, then the error
    assert out[0]["status"] == 200
    assert out[0]["signature"] == "s"  # still relayed verbatim
    assert out[1]["status"] == 502
    assert _error_detail(out[1]) == "extract: response body is not JSON"


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

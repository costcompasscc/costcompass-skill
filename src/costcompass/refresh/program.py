# LOCKSTEP: one of three relay implementations (browser is the
# reference) — semantic changes here must land in the CostCompass
# monorepo, checked out alongside this repo, at
# ../costcompass/frontend/src/lib/refresh/program-interpreter.ts and
# ../costcompass/client/macos/CostCompassKit/Sources/CostCompassKit/
#   Refresh/ProgramInterpreter.swift.
# See "Three relay implementations" in that repo's root CLAUDE.md;
# `make lockstep` there enumerates the whole set across both repos.

"""Generic interpreter for a server-authored declarative FetchProgram.

A direct port of
``../costcompass/frontend/src/lib/refresh/program-interpreter.ts``:
walk steps, substitute ``{binding}`` tokens, forward each concrete
request through the broker, relay every signed response verbatim, and
drive loop control from ``extract`` rules. Plugin-agnostic — the program
shape carries everything; no provider branching.

Failure mapping (CRITICAL): broker-transport failures and poll deadlines
produce an UNSIGNED, NON-SYNTHETIC provider-error response (so the App
Server's status taxonomy classifies it), never a throw or a synthetic
stub. ``BrokerForwardCapError`` is the one propagated exception.
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any, Callable
from urllib.parse import urlencode

from .broker import (
    BrokerClient,
    BrokerError,
    BrokerForwardCapError,
    build_forward_request,
    provider_error_response,
    to_raw_response_payload,
)

_TOKEN_RE = re.compile(r"\{([^}]+)\}")
_PATH_VALUE_CHARSET = re.compile(r"^[A-Za-z0-9._-]*$")

# The shared `body_b64` alphabet — see `_decode_body_strict`. Canonical
# standard-alphabet base64: whole 4-char groups, padding only at the tail.
#
# `\Z`, never `$`. Python's `$` also matches just BEFORE a trailing newline, so
# the `$` spelling accepts "e30=\n" — which the browser and Swift both reject,
# recreating in the guard the exact cross-relay divergence the guard exists to
# remove. Verified against 813k generated inputs: the two spellings disagree on
# 7372 of them, all trailing-newline. JS and Swift have no equivalent trap.
_CANONICAL_BASE64 = re.compile(
    r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?\Z"
)


def is_canonical_base64(body_b64: str) -> bool:
    """The base64 clause of the decode contract, on its own.

    Named and exported so the shared ``body-b64-contract.json`` corpus can
    assert THIS stage across all four implementations. The later clauses
    (UTF-8, BOM, depth, surrogate) are relay-only — the App Server implements
    the base64 half and nothing else — so the corpus can only pin this much.
    Asserting an end-to-end decode against it would assert an equality that is
    deliberately false.
    """
    return _CANONICAL_BASE64.match(body_b64) is not None

# Nesting cap, checked BEFORE parsing. Not a hardening nicety — it is the only
# portable fix: macOS's hand-rolled recursive-descent parser SIGSEGVs on deeply
# nested input (measured: 20k deep parses, 50k segfaults) and a segfault cannot
# be caught after the fact, so the input has to be refused before it reaches the
# parser. Here the symptom was different and just as bad: `RecursionError` is
# not a `ValueError`, so it escaped `_apply_extract`'s `except` and killed the
# process. Only the browser degraded cleanly.
#
# 100 fires far below every host limit (CPython ~1000, V8 ~10k, Swift ~20k-50k),
# which is the point: all three refuse at the same depth instead of each at its
# own accident. Provider billing JSON nests under 10.
_MAX_JSON_DEPTH = 100

Clock = Callable[[], float]


def _now_ms() -> float:
    return time.monotonic() * 1000.0


def run_program(
    program: dict[str, Any],
    auth_headers: dict[str, str],
    signing_token: str | None,
    broker_client: BrokerClient,
    now: Clock = _now_ms,
) -> list[dict[str, Any]]:
    bindings: dict[str, str] = dict(program.get("bindings") or {})
    out: list[dict[str, Any]] = []
    for step in program.get("steps", []):
        if step.get("kind") == "request":
            outcome = _run_request_step(
                step, bindings, auth_headers, signing_token, broker_client, program, out
            )
        else:
            outcome = _run_poll_step(
                step,
                bindings,
                auth_headers,
                signing_token,
                broker_client,
                program,
                out,
                now,
            )
        if outcome == "terminate":
            return out
    return out


def _run_request_step(
    step, bindings, auth_headers, signing_token, broker_client, program, out
) -> str:
    return _forward_and_extract(
        step["request"],
        step.get("extract") or [],
        step.get("stop_on_status_gte", 400),
        bindings,
        auth_headers,
        signing_token,
        broker_client,
        program,
        out,
    )


def _run_poll_step(
    step, bindings, auth_headers, signing_token, broker_client, program, out, now
) -> str:
    loop_start = now()
    iterations = 0
    require_bindings = step.get("require_bindings") or []
    deadline_ms = step.get("deadline_ms", 90_000)
    max_iterations = step.get("max_iterations", 64)
    while True:
        if not _should_continue(step["until"], bindings):
            return "continue"
        # `require_bindings` is the step's declaration of what it CANNOT poll
        # without. Missing one is a failure to read what an earlier step
        # promised — google's `jobs.query` answering 200 with no
        # `jobReference.jobId`, or atlascloud paging that claims `has_more`
        # and then omits the cursor. Both truncate the window silently, so
        # this must not be a clean stop: `skipped` reads as healthy, and a run
        # that polled nothing has to be visible. Position matters — after
        # `until` (a satisfied loop wants no bindings) and before
        # `max_iterations`.
        missing = next(
            (b for b in require_bindings if (bindings.get(b) or "") == ""), None
        )
        if missing is not None:
            out.append(
                provider_error_response(
                    502,
                    program.get("purpose", ""),
                    f"required binding missing: {missing}",
                )
            )
            return "terminate"
        if iterations >= max_iterations:
            return "continue"
        if now() - loop_start > deadline_ms:
            out.append(
                provider_error_response(
                    504, program.get("purpose", ""), "poll deadline exceeded"
                )
            )
            return "terminate"
        iterations += 1
        outcome = _forward_and_extract(
            step["request"],
            step.get("extract") or [],
            step.get("stop_on_status_gte", 400),
            bindings,
            auth_headers,
            signing_token,
            broker_client,
            program,
            out,
        )
        if outcome == "terminate":
            return "terminate"


def _forward_and_extract(
    req,
    extract,
    stop_gte,
    bindings,
    auth_headers,
    signing_token,
    broker_client,
    program,
    out,
) -> str:
    built = _build_concrete_request(req, bindings)
    if built is None:
        out.append(
            provider_error_response(
                502,
                program.get("purpose", ""),
                "substituted path value outside allowed charset",
            )
        )
        return "terminate"

    try:
        broker_resp = broker_client.forward(
            build_forward_request(built, auth_headers, signing_token)
        )
    except BrokerForwardCapError:
        raise
    except BrokerError as err:
        out.append(
            provider_error_response(
                502, program.get("purpose", ""), f"{err.code}: {err}"
            )
        )
        return "terminate"

    relayed = to_raw_response_payload(built, broker_resp)
    out.append(relayed)
    if relayed["status"] >= stop_gte:
        return "terminate"
    if not _apply_extract(extract, relayed["body_b64"], bindings):
        # A 2xx we declared extract rules against, whose body will not decode.
        # Continuing would forward later steps against empty bindings and land
        # a green card holding no data; fail loudly instead.
        out.append(
            provider_error_response(
                502, program.get("purpose", ""), "extract: response body is not JSON"
            )
        )
        return "terminate"
    return "continue"


def _build_concrete_request(
    req: dict[str, Any], bindings: dict[str, str]
) -> dict[str, Any] | None:
    path = _substitute_path_guarded(req["path"], bindings)
    if path is None:
        return None
    params: list[tuple[str, str]] = []
    for key, template in (req.get("query") or {}).items():
        value = _substitute(template, bindings)
        if value == "":
            continue
        params.append((key, value))
    url = f"https://{req['host']}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    return {
        "url": url,
        "method": req.get("method", "GET"),
        "headers": req.get("headers") or {},
        "body": req.get("body"),
        "purpose": req.get("purpose", ""),
        "req_sig": req.get("req_sig"),
    }


def _substitute(template: str, bindings: dict[str, str]) -> str:
    return _TOKEN_RE.sub(lambda m: bindings.get(m.group(1), ""), template)


def _substitute_path_guarded(template: str, bindings: dict[str, str]) -> str | None:
    bad = False

    def repl(match: re.Match[str]) -> str:
        nonlocal bad
        v = bindings.get(match.group(1), "")
        if not _PATH_VALUE_CHARSET.match(v) or ".." in v:
            bad = True
        return v

    value = _TOKEN_RE.sub(repl, template)
    return None if bad else value


def _reject_json_constant(name: str) -> Any:
    """Refuse the JSON constants ``JSON.parse`` refuses. ``json.loads`` calls
    this for a bare ``Infinity``/``-Infinity``/``NaN`` literal; raising turns
    the whole body unparseable, exactly as the browser reference sees it."""
    raise ValueError(f"non-standard JSON constant: {name}")


def _decode_body_strict(body_b64: str) -> Any:
    """Decode a broker ``body_b64`` into parsed JSON under the one cross-relay
    contract, raising on anything outside it.

    LOCKSTEP INVARIANT 13 (one decode contract for ``body_b64``): every relay
    must accept EXACTLY these bytes. Left to each host language's own decoders
    the three disagreed on five of seven measured inputs, in no consistent
    strictness order — and once an undecodable body became a terminal 502 that
    showed up as a red card on one relay and a green card on another.

    The contract is anchored to the App Server's, not freshly invented:
    ``RawResponseIn.body_is_decodable`` tests this same canonical regex, so a
    body one relay extracts from is exactly a body the ``/responses`` submit
    accepts. That is what settles it — not taste.

    The two used to differ, and the record is worth keeping. The server checked
    ``b64decode(validate=True)`` alone, which validates the ALPHABET only and
    so still took a few non-canonical spellings this regex refuses (``"++++="``
    — four data chars plus a stray pad): over 813k generated inputs, 0 accepted
    here and rejected there, 6561 the other way. Safe, but two rules where one
    would do. The server now spells the canonical rule directly and this half
    of the contract is EQUAL on both sides; the relay stays stricter overall
    through the UTF-8, BOM, surrogate and depth clauses below.

    Each check pins one measured divergence:

    - **Canonical base64.** ``b64decode`` without ``validate`` silently
      DISCARDS stray characters, so this relay alone accepted ``e30=!!``,
      ``e3 0=`` and ``e30==``. The explicit alphabet check mirrors
      ``vault._b64u_decode``, which exists for the same reason.
    - **Strict UTF-8** — already the CPython default; stated here because the
      browser's ``TextDecoder`` was not, and needed changing.
    - **No leading BOM** — CPython rejects one, the browser stripped it.
    - **Depth cap** — see ``_MAX_JSON_DEPTH``.
    - **No lone surrogates.** ``json.loads`` and ``JSON.parse`` both accept an
      unpaired ``\\uD800`` escape; Swift ``String`` cannot REPRESENT one, so
      "reject" is the only contract all three can implement. Checked after
      parsing because the escape only becomes a lone surrogate then — valid
      UTF-8 cannot carry one directly.
    """
    if not is_canonical_base64(body_b64):
        raise ValueError("body_b64 is not canonical base64")
    text = base64.b64decode(body_b64.encode("ascii"), validate=True).decode("utf-8")
    if text.startswith("\ufeff"):
        raise ValueError("body has a byte-order mark")
    if _exceeds_max_depth(text, _MAX_JSON_DEPTH):
        raise ValueError("body nests deeper than the shared limit")
    parsed = json.loads(
        text,
        # CPython accepts bare `Infinity`/`-Infinity`/`NaN` as a
        # non-standard extension; `JSON.parse` REJECTS them, so the
        # browser reference treats such a body as unparseable. Raise to
        # land in the same `except` and route to the same provider-error,
        # instead of extracting an "inf" no other relay can produce.
        parse_constant=_reject_json_constant,
    )
    if _contains_lone_surrogate(parsed):
        raise ValueError("body contains an unpaired UTF-16 surrogate")
    return parsed


def _exceeds_max_depth(text: str, limit: int) -> bool:
    """True when bracket nesting in ``text`` ever exceeds ``limit``.

    String-aware: a ``[`` inside a JSON string is data, not structure, and
    counting it would reject legitimate bodies whose error text happens to
    contain brackets.
    """
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            if depth > limit:
                return True
        elif ch in "]}":
            depth -= 1
    return False


def _contains_lone_surrogate(value: Any) -> bool:
    """Walk parsed JSON for an unpaired surrogate in any string or object key.

    Every surrogate reachable here is unpaired: CPython combines a well-formed
    ``\\uD83D\\uDE00`` pair into the single astral character at parse time, so
    a code point left in the surrogate range never had a partner. Recursion is
    safe only because ``_exceeds_max_depth`` already ran.
    """
    if isinstance(value, str):
        return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)
    if isinstance(value, list):
        return any(_contains_lone_surrogate(item) for item in value)
    if isinstance(value, dict):
        return any(
            _contains_lone_surrogate(key) or _contains_lone_surrogate(item)
            for key, item in value.items()
        )
    return False


def _apply_extract(
    rules: list[dict[str, Any]], body_b64: str, bindings: dict[str, str]
) -> bool:
    """Apply a step's extract rules, mutating ``bindings``. Returns ``False``
    when the body could not be decoded at all — the caller turns that into a
    provider-error and stops.

    A body we cannot read is NOT the same as a field that happens to be
    absent. An absent path still binds ``""`` (google's ``pageToken`` and
    atlascloud's ``next_page`` are legitimately absent on the last page);
    ``require_bindings`` is where a step declares what it actually needs.
    Writing ``""`` for every rule off an unreadable body conflated the two and
    let a program walk every step, extract nothing, and finish green.

    ``bindings`` is left UNTOUCHED on failure. That is safe only because every
    caller terminates the run on ``False`` — nothing in the signature enforces
    the coupling. ASSUMPTION: a decode failure is always terminal. If that ever
    stops being true, the loop resumes against bindings still holding the
    PREVIOUS iteration's values, and the next request goes out with a stale
    cursor rather than an empty one — a wrong page silently ingested, not a
    visible failure. Clear the rules' bindings here before relaxing it.
    """
    if not rules:
        return True
    try:
        parsed: Any = _decode_body_strict(body_b64)
    except (ValueError, UnicodeDecodeError):
        return False
    for rule in rules:
        raw = _read_dotted_path(parsed, rule["path"])
        if rule.get("coerce") == "bool":
            value = "true" if raw is True else ""
        else:
            value = _js_string(raw)
        bindings[rule["as_binding"]] = value
    return True


def _js_string(raw: Any) -> str:
    """Stringify like JS ``String()`` so the port matches the browser
    interpreter for every JSON value shape: arrays join element-strings
    with "," (``None`` renders empty, nested arrays flatten), objects read
    ``"[object Object]"``, numbers use ECMA-262 notation."""
    if raw is None:
        return ""
    if raw is True:
        return "true"
    if raw is False:
        return "false"
    if isinstance(raw, list):
        return ",".join(_js_string(x) for x in raw)
    if isinstance(raw, dict):
        return "[object Object]"
    if isinstance(raw, (int, float)):
        return _js_number_string(raw)
    return str(raw)


def _js_number_string(raw: int | float) -> str:
    """ECMA-262 ``Number::toString(10)``. CPython's ``repr`` produces the
    same shortest round-trip DIGITS as JS but different NOTATION: it
    switches to exponent form at 1e16/1e-5 (JS: 1e21/1e-6) and zero-pads
    single-digit exponents (``'1e-07'`` vs JS ``'1e-7'``). A JSON int is
    first collapsed through ``float`` because the browser interpreter
    reads every JSON number as a double."""
    try:
        x = float(raw)
    except OverflowError:
        # A JSON integer literal too large for a double: JS parses it as
        # Infinity and String() renders that.
        return "Infinity" if raw > 0 else "-Infinity"
    # A float literal that overflows the double range (``1e400``) is VALID
    # JSON, so ``parse_constant`` never sees it — CPython yields ``inf`` and
    # ``repr`` spells it "inf" where JS ``String()`` gives "Infinity". Handle
    # it before ``_ecma_notation``, which would pass the letters through as
    # if they were digits.
    if x != x:
        return "NaN"
    if x == float("inf"):
        return "Infinity"
    if x == float("-inf"):
        return "-Infinity"
    if x.is_integer() and abs(x) < 2**53:
        return str(int(x))
    return _ecma_notation(repr(x))


def _ecma_notation(shortest: str) -> str:
    """Reformat a shortest-round-trip float ``repr`` into ECMA-262
    §6.1.6.1.20 notation: with ``s`` the significant digits (length ``k``)
    and ``n`` the decimal-point position (value = 0.s x 10^n) — plain
    digits + zeros for ``k <= n <= 21``, embedded point for
    ``0 < n <= 21``, ``0.0...s`` down to ``n > -6``, exponent form
    (unpadded, explicit sign) beyond."""
    sign = ""
    body = shortest
    if body.startswith("-"):
        sign, body = "-", body[1:]
    mantissa, _, exp_str = body.partition("e")
    exp10 = int(exp_str) if exp_str else 0
    int_part, _, frac_part = mantissa.partition(".")
    digits = int_part + frac_part
    point_pos = len(int_part) + exp10
    stripped = digits.lstrip("0")
    point_pos -= len(digits) - len(stripped)
    digits = stripped.rstrip("0")
    if not digits:
        return "0"
    k, n = len(digits), point_pos
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    e = n - 1
    head = digits[0] + ("." + digits[1:] if k > 1 else "")
    return sign + head + "e" + ("+" if e >= 0 else "-") + str(abs(e))


def _read_dotted_path(obj: Any, path: str) -> Any:
    cur = obj
    for segment in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(segment)
    return cur


def _should_continue(until: dict[str, Any], bindings: dict[str, str]) -> bool:
    return any(_predicate_true(p, bindings) for p in until["any_of"])


def _predicate_true(pred: dict[str, Any], bindings: dict[str, str]) -> bool:
    v = bindings.get(pred["binding"], "")
    falsey = v == "" or v == "false"
    return falsey if pred["kind"] == "falsey" else not falsey

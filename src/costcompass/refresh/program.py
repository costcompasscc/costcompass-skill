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
            outcome = _run_request_step(step, bindings, auth_headers, signing_token, broker_client, program, out)
        else:
            outcome = _run_poll_step(step, bindings, auth_headers, signing_token, broker_client, program, out, now)
        if outcome == "terminate":
            return out
    return out


def _run_request_step(step, bindings, auth_headers, signing_token, broker_client, program, out) -> str:
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


def _run_poll_step(step, bindings, auth_headers, signing_token, broker_client, program, out, now) -> str:
    loop_start = now()
    iterations = 0
    require_bindings = step.get("require_bindings") or []
    deadline_ms = step.get("deadline_ms", 90_000)
    max_iterations = step.get("max_iterations", 64)
    while True:
        if not _should_continue(step["until"], bindings):
            return "continue"
        if any((bindings.get(b) or "") == "" for b in require_bindings):
            return "continue"
        if iterations >= max_iterations:
            return "continue"
        if now() - loop_start > deadline_ms:
            out.append(provider_error_response(504, program.get("purpose", ""), "poll deadline exceeded"))
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
    req, extract, stop_gte, bindings, auth_headers, signing_token, broker_client, program, out
) -> str:
    built = _build_concrete_request(req, bindings)
    if built is None:
        out.append(
            provider_error_response(502, program.get("purpose", ""), "substituted path value outside allowed charset")
        )
        return "terminate"

    try:
        broker_resp = broker_client.forward(
            build_forward_request(built, auth_headers, signing_token)
        )
    except BrokerForwardCapError:
        raise
    except BrokerError as err:
        out.append(provider_error_response(502, program.get("purpose", ""), f"{err.code}: {err}"))
        return "terminate"

    relayed = to_raw_response_payload(built, broker_resp)
    out.append(relayed)
    if relayed["status"] >= stop_gte:
        return "terminate"
    _apply_extract(extract, relayed["body_b64"], bindings)
    return "continue"


def _build_concrete_request(req: dict[str, Any], bindings: dict[str, str]) -> dict[str, Any] | None:
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


def _apply_extract(rules: list[dict[str, Any]], body_b64: str, bindings: dict[str, str]) -> None:
    if not rules:
        return
    try:
        parsed: Any = json.loads(base64.b64decode(body_b64).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        parsed = {}
    for rule in rules:
        raw = _read_dotted_path(parsed, rule["path"])
        if rule.get("coerce") == "bool":
            value = "true" if raw is True else ""
        else:
            value = _js_string(raw)
        bindings[rule["as_binding"]] = value


def _js_string(raw: Any) -> str:
    """Stringify like JS ``String()`` so the port matches the browser
    interpreter for non-string JSON values (bool/number)."""
    if raw is None:
        return ""
    if raw is True:
        return "true"
    if raw is False:
        return "false"
    if isinstance(raw, float) and raw.is_integer():
        return str(int(raw))  # JS: String(1.0) === "1"
    return str(raw)


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

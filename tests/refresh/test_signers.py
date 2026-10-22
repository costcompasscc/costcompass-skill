from __future__ import annotations

import base64
import hashlib

import pytest

from costcompass.refresh import signers


def test_build_auth_header_value():
    assert signers.build_auth_header_value(None, "sk") == "sk"
    assert signers.build_auth_header_value("Bearer", "sk") == "Bearer sk"


_TARGET = {"host": "h", "path": "/p", "method": "GET"}


def test_flat_plan_headers():
    plan = {"auth_header": "x-api-key", "auth_scheme": None}
    sign = signers.build_plan_signer(plan, "sk-1")
    assert sign(_TARGET, None) == {"x-api-key": "sk-1"}


def test_flat_plan_headers_with_scheme():
    plan = {"auth_header": "Authorization", "auth_scheme": "Bearer"}
    sign = signers.build_plan_signer(plan, "sk-1")
    assert sign(_TARGET, None) == {"Authorization": "Bearer sk-1"}


def test_qc_hmac_v1_known_vector():
    # Independently re-derive the spec formula:
    #   hashed = sha256hex(token:timestamp); auth = base64(user_id:hashed)
    token = "apitoken"
    ts = 1_700_000_000
    user = "42"
    hashed = hashlib.sha256(f"{token}:{ts}".encode()).hexdigest()
    expected_auth = base64.b64encode(f"{user}:{hashed}".encode()).decode()

    plan = {
        "auth_header": "x",
        "signed_auth": {
            "kind": "qc_hmac_v1",
            "user_id": user,
            "authorization_header": "Authorization",
            "authorization_scheme": "Basic",
            "timestamp_header": "Timestamp",
        },
    }
    headers = signers.sign_quantconnect_hmac_v1(plan["signed_auth"], token, now_sec=ts)
    assert headers["Authorization"] == f"Basic {expected_auth}"
    assert headers["Timestamp"] == str(ts)
    # bearer path skipped entirely when signed_auth present
    assert "x" not in signers.build_plan_signer(plan, token)(_TARGET, None)


_OVH_PLAN = {
    "auth_header": "Authorization",
    "auth_scheme": "Bearer",
    "signed_auth": {
        "kind": "ovh_v1",
        "application_key": "ak-app",
        "server_now": 1_760_000_000,
    },
}
_BILLS = {
    "host": "eu.api.ovh.com",
    "path": "/1.0/me/bill",
    "method": "GET",
    "query": "date.from=2026-01-01",
}


def test_ovh_v1_known_vector():
    # Independently re-derive the documented OVH formula.
    pre = (
        b"as-secret+ck-consumer+GET+https://eu.api.ovh.com/1.0/me/bill"
        b"?date.from=2026-01-01++1760000000"
    )
    headers = signers.sign_ovh_v1(
        _OVH_PLAN["signed_auth"], "as-secret:ck-consumer", _BILLS, None, 1_760_000_000
    )
    assert headers == {
        "X-Ovh-Application": "ak-app",
        "X-Ovh-Consumer": "ck-consumer",
        "X-Ovh-Timestamp": "1760000000",
        "X-Ovh-Signature": "$1$" + hashlib.sha1(pre).hexdigest(),
    }


@pytest.mark.parametrize("cred", ["", "no-colon", ":ck-only", "as-only:"])
def test_ovh_v1_rejects_malformed_credential_without_echoing_it(cred):
    with pytest.raises(ValueError) as exc:
        signers.build_plan_signer(_OVH_PLAN, cred)
    assert (
        str(exc.value) == "ovh_v1 credential must be 'application_secret:consumer_key'"
    )


def test_ovh_v1_never_falls_back_to_bearer():
    headers = signers.build_plan_signer(_OVH_PLAN, "as-secret:ck-consumer")(
        _BILLS, None
    )
    assert sorted(headers) == [
        "X-Ovh-Application",
        "X-Ovh-Consumer",
        "X-Ovh-Signature",
        "X-Ovh-Timestamp",
    ]
    assert "as-secret" not in repr(headers)


def test_ovh_v1_clock_offset_is_fixed_at_plan_receipt(monkeypatch):
    received = 1_760_000_000 - 100  # local clock 100s behind the server
    monkeypatch.setattr(signers.time, "time", lambda: float(received))
    sign = signers.build_plan_signer(_OVH_PLAN, "as:ck", plan_received_at_sec=received)
    assert sign(_BILLS, None)["X-Ovh-Timestamp"] == "1760000000"
    # A retry 300s later carries the server's time, not a stale one.
    monkeypatch.setattr(signers.time, "time", lambda: float(received + 300))
    assert sign(_BILLS, None)["X-Ovh-Timestamp"] == "1760000300"


# One valid spec per implemented signer kind. Python has no compile-time check
# that ``IMPLEMENTED_SIGNER_KINDS`` matches what ``build_plan_signer`` signs
# with, so this pins it at test time: the set must equal this table's keys (a
# new kind needs a spec here), and every kind must build without reaching the
# unsupported branch — which is what the orchestrator's pre-flight relies on.
_VALID_SIGNER_PLANS = {
    "qc_hmac_v1": (
        {
            "auth_header": "x",
            "signed_auth": {
                "kind": "qc_hmac_v1",
                "user_id": "42",
                "authorization_header": "Authorization",
                "authorization_scheme": "Basic",
                "timestamp_header": "Timestamp",
            },
        },
        "apitoken",
    ),
    "ovh_v1": (_OVH_PLAN, "as-secret:ck-consumer"),
}


def test_implemented_signer_kinds_match_the_dispatcher():
    assert set(_VALID_SIGNER_PLANS) == signers.IMPLEMENTED_SIGNER_KINDS


@pytest.mark.parametrize("kind", sorted(signers.IMPLEMENTED_SIGNER_KINDS))
def test_every_implemented_signer_kind_builds(kind):
    plan, cred = _VALID_SIGNER_PLANS[kind]
    assert signers.supports_signer_kind(kind)
    assert signers.build_plan_signer(plan, cred)(_BILLS, None)


@pytest.mark.parametrize("kind", ["mystery_signer_v9", None, 7, ["qc_hmac_v1"]])
def test_an_unimplemented_signer_kind_is_unsupported_and_refused(kind):
    assert not signers.supports_signer_kind(kind)
    with pytest.raises(ValueError, match="unsupported signed_auth kind"):
        signers.build_plan_signer(
            {"auth_header": "x", "signed_auth": {"kind": kind}}, "k"
        )

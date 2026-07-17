from __future__ import annotations

import base64
import hashlib

from costcompass.refresh import signers


def test_build_auth_header_value():
    assert signers.build_auth_header_value(None, "sk") == "sk"
    assert signers.build_auth_header_value("Bearer", "sk") == "Bearer sk"


def test_flat_plan_headers():
    plan = {"auth_header": "x-api-key", "auth_scheme": None}
    assert signers.build_plan_headers(plan, "sk-1") == {"x-api-key": "sk-1"}


def test_flat_plan_headers_with_scheme():
    plan = {"auth_header": "Authorization", "auth_scheme": "Bearer"}
    assert signers.build_plan_headers(plan, "sk-1") == {"Authorization": "Bearer sk-1"}


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
    headers = signers.build_plan_headers(plan, token, now_sec=ts)
    assert headers["Authorization"] == f"Basic {expected_auth}"
    assert headers["Timestamp"] == str(ts)
    # bearer path skipped entirely when signed_auth present
    assert "x" not in headers

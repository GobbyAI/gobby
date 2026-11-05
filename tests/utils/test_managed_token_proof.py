"""A managed token's client-side proof matches what the issuing daemon answers for that token."""

from __future__ import annotations

import secrets

import pytest

from gobby.runtime_grants.handshake import challenge_proof
from gobby.utils.local_token import (
    derive_managed_signing_key,
    issue_agent_api_token,
    managed_token_challenge_claims,
    managed_token_proof,
    verify_agent_api_token,
)

pytestmark = pytest.mark.unit

MACHINE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _token(key: bytes) -> str:
    return issue_agent_api_token(
        key,
        agent_run_id="run-1",
        session_id="session-1",
        project_id="project-1",
        machine_id=MACHINE_ID,
        timeout_seconds=600,
    )


def test_proof_matches_the_daemon_answer_for_the_same_token() -> None:
    key = derive_managed_signing_key("operator-token")
    token = _token(key)
    claims = verify_agent_api_token(token, key)
    assert claims is not None
    nonce = secrets.token_bytes(32)

    assert managed_token_proof(token, nonce) == challenge_proof(
        nonce, kind="managed", signing_key=key, claims=claims
    )


def test_proof_does_not_match_a_daemon_holding_another_key() -> None:
    key = derive_managed_signing_key("operator-token")
    other = derive_managed_signing_key("another-daemon-token")
    token = _token(key)
    claims = verify_agent_api_token(token, key)
    assert claims is not None
    nonce = secrets.token_bytes(32)

    assert managed_token_proof(token, nonce) != challenge_proof(
        nonce, kind="managed", signing_key=other, claims=claims
    )


def test_challenge_claims_are_the_signed_payload_fields() -> None:
    token = _token(derive_managed_signing_key("operator-token"))

    claims = managed_token_challenge_claims(token)

    assert claims is not None
    assert set(claims) == {
        "session_id",
        "project_id",
        "machine_id",
        "iat",
        "exp",
        "agent_run_id",
        "kind",
    }
    assert claims["agent_run_id"] == "run-1"


def test_malformed_tokens_have_no_proof_and_no_claims() -> None:
    assert managed_token_proof("not-a-token", b"nonce") is None
    assert managed_token_challenge_claims("gobby-agent-v1.!!.!!") is None

"""Tests for managed daemon capability tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest

from gobby.utils.local_token import (
    issue_agent_api_token,
    issue_tool_api_token,
    verify_agent_api_token,
)

pytestmark = pytest.mark.unit


def _signed_token(payload: dict[str, object], operator_token: str) -> str:
    encoded_payload = (
        base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    signed = f"gobby-agent-v1.{encoded_payload}"
    signature = hmac.new(operator_token.encode(), signed.encode(), hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{signed}.{encoded_signature}"


def test_verifier_rejects_present_empty_second_owner_claim() -> None:
    operator_token = "operator-token"
    token = _signed_token(
        {
            "agent_run_id": "",
            "managed_execution_id": "tool-execution-1",
            "session_id": "session-1",
            "project_id": "project-1",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
        },
        operator_token,
    )

    assert verify_agent_api_token(token, operator_token) is None


def test_issued_tokens_carry_signed_machine_id() -> None:
    operator_token = "operator-token"
    machine_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    agent = issue_agent_api_token(
        operator_token,
        agent_run_id="run-1",
        session_id="session-1",
        project_id="project-1",
        machine_id=machine_id,
        timeout_seconds=30,
    )
    tool = issue_tool_api_token(
        operator_token,
        managed_execution_id="exec-1",
        session_id="session-1",
        project_id="project-1",
        machine_id=machine_id,
        timeout_seconds=30,
    )
    agent_claims = verify_agent_api_token(agent, operator_token)
    tool_claims = verify_agent_api_token(tool, operator_token)
    assert agent_claims is not None
    assert tool_claims is not None
    assert agent_claims.machine_id == machine_id
    assert tool_claims.machine_id == machine_id
    unsigned = _signed_token(
        {
            "agent_run_id": "run-1",
            "session_id": "session-1",
            "project_id": "project-1",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
        },
        operator_token,
    )
    assert verify_agent_api_token(unsigned, operator_token) is None

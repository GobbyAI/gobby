"""Tests for managed daemon capability tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest

from gobby.utils import local_token
from gobby.utils.local_token import (
    classify_agent_api_token,
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


@pytest.mark.parametrize(
    ("token_name", "operator_token", "expected"),
    [
        ("operator", "operator-token", "invalid_token"),
        ("live", None, "operator_token_unavailable"),
        ("live", "rotated-operator-token", "capability_invalid"),
        ("expired", "operator-token", "capability_expired"),
    ],
)
def test_classifier_names_why_a_capability_was_refused(
    token_name: str, operator_token: str | None, expected: str
) -> None:
    now = int(time.time())
    claims: dict[str, object] = {
        "agent_run_id": "run-1",
        "session_id": "session-1",
        "project_id": "project-1",
        "machine_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "iat": now,
    }
    tokens = {
        # An operator token presented as a bearer is not a managed capability.
        "operator": "operator-token",
        "live": _signed_token({**claims, "exp": now + 60}, "operator-token"),
        "expired": _signed_token({**claims, "exp": now - 1}, "operator-token"),
    }

    assert classify_agent_api_token(tokens[token_name], operator_token) == expected


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


def test_unreadable_local_token_reads_as_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A sandbox denial on the operator token is "no token", not a crash.

    ``local_cli_token`` is one of the credential roots a managed grant may never
    read, so ``gobby mcp-server`` inside an agent sandbox is answered with
    ``PermissionError``. Run that command under a real Ask sandbox policy with no
    run capability in the environment and the error escapes here, exits the
    process as ``MCP server failed: [Errno 1] Operation not permitted``, and the
    client registers no server at all. ``daemon_auth_headers`` already prefers
    the run capability and copes with no operator token, so the denial has to
    arrive as ``None``.
    """
    token_path = tmp_path / "local_cli_token"
    token_path.write_text("operator-token", encoding="utf-8")
    token_path.chmod(0o000)
    monkeypatch.setattr(local_token, "local_token_path", lambda: token_path)
    try:
        assert local_token.read_local_api_token() is None
    finally:
        token_path.chmod(0o600)


def test_run_capability_is_preferred_over_an_unreadable_operator_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The sandboxed MCP bridge authenticates with its run capability."""
    token_path = tmp_path / "local_cli_token"
    token_path.write_text("operator-token", encoding="utf-8")
    token_path.chmod(0o000)
    monkeypatch.setattr(local_token, "local_token_path", lambda: token_path)
    monkeypatch.setenv("GOBBY_AGENT_API_TOKEN", "run-capability")
    monkeypatch.setenv("GOBBY_SESSION_ID", "session-1")
    try:
        headers = local_token.daemon_auth_headers()
    finally:
        token_path.chmod(0o600)
    assert headers["Authorization"] == "Bearer run-capability"
    assert headers["X-Gobby-Session-Id"] == "session-1"

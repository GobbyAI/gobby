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
    derive_managed_signing_key,
    issue_agent_api_token,
    issue_tool_api_token,
    verify_agent_api_token,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("valid_signature", [True, False])
def test_expiry_diagnostic_contains_only_verified_timestamps(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    valid_signature: bool,
) -> None:
    monkeypatch.setattr("gobby.utils.local_token.time.time", lambda: 2000.0)
    key = derive_managed_signing_key("expiry-diagnostic-fixture")
    token = _signed_token(
        {
            "agent_run_id": "run-1",
            "session_id": "session-1",
            "project_id": "project-1",
            "machine_id": "machine-1",
            "iat": 1000,
            "exp": 1660,
        },
        key if valid_signature else b"different-fixture-key",
    )

    result = classify_agent_api_token(token, key)

    messages = [
        record.getMessage() for record in caplog.records if record.name == local_token.__name__
    ]
    if valid_signature:
        assert result == "capability_expired"
        assert messages == [
            "Managed capability rejected code=capability_expired iat=1000 exp=1660 now=2000"
        ]
    else:
        assert result == "capability_invalid"
        assert messages == []
    assert token not in caplog.text
    assert "expiry-diagnostic-fixture" not in caplog.text


@pytest.mark.parametrize(
    ("elapsed_seconds", "valid"),
    [(900, True), (1259, True), (1260, False)],
    ids=["fifteen-minutes", "last-valid-second", "timeout-plus-grace"],
)
def test_managed_run_capability_lifetime_matches_timeout(
    monkeypatch: pytest.MonkeyPatch, elapsed_seconds: int, valid: bool
) -> None:
    from gobby.utils.local_token import derive_managed_signing_key

    monkeypatch.setattr("gobby.utils.local_token.time.time", lambda: 1000.0)
    key = derive_managed_signing_key("synthetic-lifetime-key")
    token = issue_agent_api_token(
        key,
        agent_run_id="run-lifetime",
        session_id="session-lifetime",
        project_id="project-lifetime",
        machine_id="machine-lifetime",
        timeout_seconds=1200,
    )
    initial_claims = verify_agent_api_token(token, key)
    assert initial_claims is not None
    assert initial_claims.iat == 1000
    assert initial_claims.exp == 2260

    monkeypatch.setattr("gobby.utils.local_token.time.time", lambda: 1000.0 + elapsed_seconds)
    claims = verify_agent_api_token(token, key)
    assert (claims is not None) is valid


def test_operator_key_reads_bootstrap_fresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bootstrap = tmp_path / "bootstrap.yaml"
    monkeypatch.setattr(local_token, "_daemon_bootstrap", bootstrap)
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    assert local_token.read_local_api_token() is None
    bootstrap.write_text("api_key: first-key\n", encoding="utf-8")
    assert local_token.read_local_api_token() == "first-key"
    bootstrap.write_text("api_key: second-key\n", encoding="utf-8")
    assert local_token.read_local_api_token() == "second-key"
    for content in ("", "api_key: ''\n", "api_key: 123\n", "api_key: [\n"):
        bootstrap.write_text(content, encoding="utf-8")
        assert local_token.read_local_api_token() is None


def test_managed_tokens_sign_with_derived_key() -> None:
    from gobby.utils.local_token import derive_managed_signing_key

    api_key = "bootstrap-key"
    key = derive_managed_signing_key(api_key)
    assert key == hmac.new(api_key.encode(), b"gobby-managed-token-v1", hashlib.sha256).digest()
    token = issue_agent_api_token(
        key,
        agent_run_id="run-1",
        session_id="session-1",
        project_id="project-1",
        machine_id="machine-1",
    )
    assert verify_agent_api_token(token, key) is not None
    assert verify_agent_api_token(token, api_key.encode()) is None
    assert verify_agent_api_token(token, derive_managed_signing_key("other-key")) is None
    raw_signed = _signed_token(
        {
            "agent_run_id": "run-1",
            "session_id": "session-1",
            "project_id": "project-1",
            "machine_id": "machine-1",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
        },
        api_key.encode(),
    )
    assert verify_agent_api_token(raw_signed, key) is None


def test_missing_bootstrap_key_refuses_issuance_and_verification(tmp_path: Path) -> None:
    from gobby.utils.local_token import read_managed_signing_key

    bootstrap = tmp_path / "bootstrap.yaml"
    for content in ("", "api_key: null\n", "api_key: ''\n", "api_key: 42\n"):
        bootstrap.write_text(content)
        key = read_managed_signing_key(bootstrap)
        assert key is None
        assert classify_agent_api_token("gobby-agent-v1.e30.sig", key) == "signing_key_unavailable"
        with pytest.raises(ValueError, match="signing_key_unavailable"):
            issue_agent_api_token(
                key or b"",
                agent_run_id="run-1",
                session_id="session-1",
                project_id="project-1",
                machine_id="machine-1",
            )


def test_managed_signing_key_reads_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.utils.local_token import read_managed_signing_key

    bootstrap = tmp_path / "bootstrap.yaml"
    assert read_managed_signing_key(bootstrap) is None
    for content in (b"[unterminated", b"\xff", b"[]"):
        bootstrap.write_bytes(content)
        assert read_managed_signing_key(bootstrap) is None
    bootstrap.write_text("api_key: bootstrap-key\n")

    def denied_read(_path: Path) -> bytes:
        raise PermissionError("sandbox denial")

    monkeypatch.setattr(Path, "read_bytes", denied_read)
    assert read_managed_signing_key(bootstrap) is None


def _signed_token(payload: dict[str, object], operator_token: bytes) -> str:
    encoded_payload = (
        base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    signed = f"gobby-agent-v1.{encoded_payload}"
    signature = hmac.new(operator_token, signed.encode(), hashlib.sha256).digest()
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
        derive_managed_signing_key(operator_token),
    )

    assert verify_agent_api_token(token, derive_managed_signing_key(operator_token)) is None


@pytest.mark.parametrize(
    ("token_name", "operator_token", "expected"),
    [
        ("operator", "operator-token", "invalid_token"),
        ("live", None, "signing_key_unavailable"),
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
        "live": _signed_token(
            {**claims, "exp": now + 60}, derive_managed_signing_key("operator-token")
        ),
        "expired": _signed_token(
            {**claims, "exp": now - 1}, derive_managed_signing_key("operator-token")
        ),
    }

    assert (
        classify_agent_api_token(
            tokens[token_name],
            derive_managed_signing_key(operator_token) if operator_token is not None else None,
        )
        == expected
    )


def test_issued_tokens_carry_signed_machine_id() -> None:
    operator_token = "operator-token"
    machine_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    agent = issue_agent_api_token(
        derive_managed_signing_key(operator_token),
        agent_run_id="run-1",
        session_id="session-1",
        project_id="project-1",
        machine_id=machine_id,
        timeout_seconds=30,
    )
    tool = issue_tool_api_token(
        derive_managed_signing_key(operator_token),
        managed_execution_id="exec-1",
        session_id="session-1",
        project_id="project-1",
        machine_id=machine_id,
        timeout_seconds=30,
    )
    agent_claims = verify_agent_api_token(agent, derive_managed_signing_key(operator_token))
    tool_claims = verify_agent_api_token(tool, derive_managed_signing_key(operator_token))
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
        derive_managed_signing_key(operator_token),
    )
    assert verify_agent_api_token(unsigned, derive_managed_signing_key(operator_token)) is None


def test_unreadable_bootstrap_key_reads_as_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A sandbox denial on bootstrap is an absent credential, not a crash."""
    token_path = tmp_path / "bootstrap.yaml"
    token_path.write_text("api_key: operator-token\n", encoding="utf-8")
    token_path.chmod(0o000)
    monkeypatch.setattr(local_token, "daemon_bootstrap_path", lambda: token_path)
    try:
        assert local_token.read_local_api_token() is None
    finally:
        token_path.chmod(0o600)


def test_run_capability_is_preferred_over_an_unreadable_operator_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The sandboxed MCP bridge authenticates with its run capability."""
    token_path = tmp_path / "bootstrap.yaml"
    token_path.write_text("api_key: operator-token\n", encoding="utf-8")
    token_path.chmod(0o000)
    monkeypatch.setattr(local_token, "daemon_bootstrap_path", lambda: token_path)
    monkeypatch.setenv("GOBBY_AGENT_API_TOKEN", "run-capability")
    monkeypatch.setenv("GOBBY_SESSION_ID", "session-1")
    try:
        headers = local_token.daemon_auth_headers()
    finally:
        token_path.chmod(0o600)
    assert headers["Authorization"] == "Bearer run-capability"
    assert headers["X-Gobby-Session-Id"] == "session-1"

"""A spawned seat restarts through a signed request; its bearer and derived key never leave it."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from click.testing import CliRunner

from gobby.cli import daemon_lifecycle as lifecycle
from gobby.cli.daemon_lifecycle import restart
from gobby.runtime_grants.signed_request import (
    SIGNED_MAC_HEADER,
    SIGNED_NONCE_HEADER,
    SIGNED_PAYLOAD_HEADER,
    derive_signed_request_key,
    signed_request_mac,
)
from gobby.utils.local_token import derive_managed_signing_key, issue_agent_api_token

pytestmark = pytest.mark.unit

DAEMON_URL = "http://127.0.0.1:61987"
NONCE_URL = "http://localhost:61987/api/runtime/signed-request/nonce"
RESTART_URL = "http://localhost:61987/api/admin/restart"
PROTECTED_RUNS_URL = "http://localhost:61987/api/admin/cron/protected-runs"
MACHINE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SIGNING_KEY = derive_managed_signing_key("operator-token")
TOKEN = issue_agent_api_token(
    SIGNING_KEY,
    agent_run_id="exec-1",
    session_id="session-1",
    project_id="project-1",
    machine_id=MACHINE_ID,
    timeout_seconds=600,
)
_VERSION, PAYLOAD, SIGNATURE_TEXT = TOKEN.split(".")
SIGNATURE = base64.urlsafe_b64decode(SIGNATURE_TEXT + "=" * (-len(SIGNATURE_TEXT) % 4))
DERIVED_KEY = derive_signed_request_key(SIGNATURE)
# Every value that would let a listener present or forge a request, in each encoding it could take.
SECRETS = (
    TOKEN,
    SIGNATURE_TEXT,
    DERIVED_KEY.hex(),
    base64.urlsafe_b64encode(DERIVED_KEY).decode(),
    base64.b64encode(DERIVED_KEY).decode(),
)


def _forbidden(name: str) -> MagicMock:
    return MagicMock(side_effect=AssertionError(f"the spawned seat touched {name}"))


class _Listener:
    """A rogue listener at the spawned URL. It answers like a daemon and records every request."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        nonce_status: int = 200,
        restart_status: int = 200,
        runs: list[dict[str, Any]] | None = None,
        unreachable: bool = False,
    ) -> None:
        self.nonce_status = nonce_status
        self.restart_status = restart_status
        self.runs = runs or []
        self.unreachable = unreachable
        self.calls: list[dict[str, Any]] = []
        monkeypatch.setattr(httpx, "post", self._post)
        monkeypatch.setattr(httpx, "get", self._get)

    def _record(self, method: str, url: str, kwargs: dict[str, Any]) -> None:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(kwargs.get("headers") or {}),
                "params": dict(kwargs.get("params") or {}),
                "json": kwargs.get("json"),
            }
        )
        if self.unreachable:
            raise httpx.ConnectError("connection refused")

    def _post(self, url: str, **kwargs: Any) -> httpx.Response:
        self._record("POST", url, kwargs)
        if url == NONCE_URL:
            if self.nonce_status != 200:
                return httpx.Response(self.nonce_status, json={"error": "unavailable"})
            return httpx.Response(200, json={"nonce": "rogue-nonce"})
        if self.restart_status != 200:
            return httpx.Response(self.restart_status, json={"error": "unavailable"})
        return httpx.Response(200, json={"status": "restarting", "backend_pid": 42})

    def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        self._record("GET", url, kwargs)
        return httpx.Response(200, json={"runs": self.runs})

    def urls(self) -> set[str]:
        return {str(call["url"]) for call in self.calls}

    def restart_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["url"] == RESTART_URL]

    def wire(self) -> str:
        """Everything the listener could read: URLs, headers, query parameters and bodies."""
        return "\n".join(repr(call) for call in self.calls)


def _expected_restart_mac(force: str) -> str:
    return signed_request_mac(
        DERIVED_KEY,
        operation="restart",
        method="POST",
        path="/api/admin/restart",
        query=(("force", force), ("terminals", "false")),
        body=b"",
        nonce="rogue-nonce",
        encoded_payload=PAYLOAD,
    )


def _restart(*, force: bool = False) -> bool | None:
    return lifecycle._restart_backend(
        MagicMock(), force=force, wait=False, drain_terminals=False, spawned=True
    )


@pytest.fixture
def seat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[MagicMock]:
    """A spawned seat with a private envelope and a spawn-time URL, and no operator bootstrap.

    Yields the local stop half, which must never run for a spawned seat's restart.
    """
    envelope = tmp_path / "envelope.json"
    envelope.write_text(
        json.dumps({"managed_api_token": TOKEN, "principal": {"execution_id": "exec-1"}})
    )
    os.chmod(envelope, 0o600)
    monkeypatch.setenv("GOBBY_MANAGED_EXECUTION_ID", "exec-1")
    monkeypatch.setenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", str(envelope))
    monkeypatch.setenv("GOBBY_DAEMON_URL", DAEMON_URL)
    monkeypatch.delenv("GOBBY_AGENT_RUN_ID", raising=False)
    monkeypatch.setattr("gobby.utils.local_token.daemon_bootstrap_path", _forbidden("bootstrap"))
    monkeypatch.setattr(
        "gobby.releases.activation.operator_bootstrap_path", _forbidden("bootstrap")
    )
    monkeypatch.setattr("gobby.cli.runtime.get_cli_runtime", _forbidden("runtime"))
    monkeypatch.setattr(lifecycle, "restart_start_refusal", _forbidden("start proof"))
    stop = _forbidden("local stop")
    monkeypatch.setattr(lifecycle, "_do_stop", stop)
    monkeypatch.setattr(lifecycle, "is_test_protect_enabled", lambda: False)
    monkeypatch.setattr(lifecycle, "_is_daemon_healthy", lambda _port: True)
    monkeypatch.setattr(lifecycle, "_is_process_alive", MagicMock(return_value=False))
    monkeypatch.setattr("gobby.cli.daemon_start._poll_startup_progress", lambda *_a, **_k: True)
    yield stop


def test_spawned_restart_sends_a_signed_request_and_no_bearer(
    monkeypatch: pytest.MonkeyPatch, seat: MagicMock
) -> None:
    listener = _Listener(monkeypatch)

    assert _restart() is True

    (restart_call,) = listener.restart_calls()
    assert "Authorization" not in restart_call["headers"]
    assert restart_call["headers"][SIGNED_PAYLOAD_HEADER] == PAYLOAD
    assert restart_call["headers"][SIGNED_NONCE_HEADER] == "rogue-nonce"
    assert restart_call["headers"][SIGNED_MAC_HEADER] == _expected_restart_mac("false")
    seat.assert_not_called()


def test_rogue_listener_never_receives_the_bearer_or_the_derived_key(
    monkeypatch: pytest.MonkeyPatch, seat: MagicMock
) -> None:
    listener = _Listener(monkeypatch)

    _restart()

    assert listener.restart_calls()
    assert all("Authorization" not in call["headers"] for call in listener.calls)
    for secret in SECRETS:
        assert secret not in listener.wire()


def test_protected_run_check_is_signed_and_carries_no_bearer(
    monkeypatch: pytest.MonkeyPatch, seat: MagicMock
) -> None:
    listener = _Listener(monkeypatch, runs=[{"name": "dream", "remaining_seconds": 0}])

    assert _restart() is False

    (check,) = [call for call in listener.calls if call["url"] == PROTECTED_RUNS_URL]
    assert "Authorization" not in check["headers"]
    assert check["headers"][SIGNED_MAC_HEADER] == signed_request_mac(
        DERIVED_KEY,
        operation="protected-runs",
        method="GET",
        path="/api/admin/cron/protected-runs",
        query=(),
        body=b"",
        nonce="rogue-nonce",
        encoded_payload=PAYLOAD,
    )
    assert listener.restart_calls() == []
    for secret in SECRETS:
        assert secret not in listener.wire()


def test_force_flag_is_forwarded_and_covered_by_the_signature(
    monkeypatch: pytest.MonkeyPatch, seat: MagicMock
) -> None:
    listener = _Listener(monkeypatch)

    _restart(force=True)

    (restart_call,) = listener.restart_calls()
    assert restart_call["params"] == {"force": "true", "terminals": "false"}
    assert restart_call["headers"][SIGNED_MAC_HEADER] == _expected_restart_mac("true")
    assert _expected_restart_mac("true") != _expected_restart_mac("false")


def test_failed_signing_refuses_before_any_restart_or_protected_run_request(
    monkeypatch: pytest.MonkeyPatch, seat: MagicMock
) -> None:
    listener = _Listener(monkeypatch, nonce_status=503)

    assert _restart() is False

    assert listener.urls() == {NONCE_URL}
    assert all("Authorization" not in call["headers"] for call in listener.calls)
    seat.assert_not_called()


def test_unreachable_listener_gets_no_signed_request_and_no_bearer(
    monkeypatch: pytest.MonkeyPatch, seat: MagicMock
) -> None:
    listener = _Listener(monkeypatch, unreachable=True)

    assert _restart() is False

    assert listener.restart_calls() == []
    assert all("Authorization" not in call["headers"] for call in listener.calls)
    for secret in SECRETS:
        assert secret not in listener.wire()


def test_spawned_restart_without_a_managed_envelope_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, seat: MagicMock
) -> None:
    listener = _Listener(monkeypatch)
    monkeypatch.delenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP")

    assert _restart() is False

    assert listener.calls == []
    seat.assert_not_called()


def test_spawned_restart_with_a_mismatched_envelope_owner_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, seat: MagicMock, tmp_path: Path
) -> None:
    listener = _Listener(monkeypatch)
    other = tmp_path / "other.json"
    other.write_text(
        json.dumps({"managed_api_token": TOKEN, "principal": {"execution_id": "exec-2"}})
    )
    os.chmod(other, 0o600)
    monkeypatch.setenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", str(other))

    assert _restart() is False

    assert listener.calls == []
    seat.assert_not_called()


@pytest.mark.parametrize("flags", [["--full"], ["--docker"]])
def test_spawned_whole_daemon_restart_stays_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, seat: MagicMock, flags: list[str]
) -> None:
    listener = _Listener(monkeypatch)

    result = CliRunner().invoke(restart, flags)

    assert result.exit_code == 1
    assert "spawned agents may not stop or start the daemon" in result.output
    assert listener.calls == []
    seat.assert_not_called()


def test_unavailable_backend_is_refused_for_a_spawned_seat_not_stopped(
    monkeypatch: pytest.MonkeyPatch, seat: MagicMock
) -> None:
    listener = _Listener(monkeypatch, restart_status=503)

    result = CliRunner().invoke(restart, [])

    assert result.exit_code == 1
    assert "the daemon answered 503 to the restart request" in result.output
    assert "did not accept the restart request" not in result.output
    assert all("Authorization" not in call["headers"] for call in listener.calls)
    seat.assert_not_called()


def test_test_protect_without_isolated_home_names_the_guard_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, seat: MagicMock
) -> None:
    listener = _Listener(monkeypatch)
    monkeypatch.setattr(lifecycle, "is_test_protect_enabled", lambda: True)

    result = CliRunner().invoke(restart, [])

    assert result.exit_code == 1
    assert "GOBBY_TEST_PROTECT is set" in result.output
    assert "did not accept the restart request" not in result.output
    assert listener.calls == []
    seat.assert_not_called()


def test_failed_health_probe_names_the_probe_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, seat: MagicMock
) -> None:
    listener = _Listener(monkeypatch)
    monkeypatch.setattr(lifecycle, "_is_daemon_healthy", lambda _port: False)

    result = CliRunner().invoke(restart, [])

    assert result.exit_code == 1
    assert "health check at localhost:61987 failed" in result.output
    assert "did not accept the restart request" not in result.output
    assert listener.calls == []
    seat.assert_not_called()

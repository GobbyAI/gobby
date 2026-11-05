"""A spawned seat's `gobby restart` proves the daemon before it presents its bearer.

The seat presents its private managed envelope to the daemon URL it was spawned with. The bearer
leaves the process only after that listener answers a challenge that only the daemon holding the
token's signing key can answer. The daemon route then verifies the token and decides admission.
The seat never runs the local stop or start halves and never reads the operator bootstrap.
"""

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
from gobby.cli._daemon_protected_runs import clear_protected_runs as real_clear_protected_runs
from gobby.cli.daemon_lifecycle import restart
from gobby.runtime_grants.handshake import challenge_proof
from gobby.servers.routes.runtime_handshake import ChallengeRequest, _challenge_claims
from gobby.utils.local_token import derive_managed_signing_key, issue_agent_api_token

pytestmark = pytest.mark.unit

DAEMON_URL = "http://127.0.0.1:61987"
CHALLENGE_URL = "http://localhost:61987/api/runtime/handshake/challenge"
ROUTE_URL = "http://localhost:61987/api/admin/restart"
PROTECTED_RUNS_URL = "http://localhost:61987/api/admin/cron/protected-runs"
MACHINE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SIGNING_KEY = derive_managed_signing_key("operator-token")
OTHER_KEY = derive_managed_signing_key("another-daemon-token")
TOKEN = issue_agent_api_token(
    SIGNING_KEY,
    agent_run_id="exec-1",
    session_id="session-1",
    project_id="project-1",
    machine_id=MACHINE_ID,
    timeout_seconds=600,
)


def _forbidden(name: str) -> MagicMock:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(f"{name} must not run on a spawned seat's restart")

    return MagicMock(side_effect=refuse)


class _Daemon:
    """The listener at the spawned URL. It answers challenges as a daemon holding ``key`` would.

    Every request it receives is recorded, so a test can prove which calls carried the bearer.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, key: bytes = SIGNING_KEY) -> None:
        self.key = key
        self.restart = httpx.Response(200, json={"status": "restarting", "backend_pid": 42})
        self.unreachable = False
        self.calls: list[dict[str, Any]] = []
        monkeypatch.setattr(httpx, "post", self._post)
        monkeypatch.setattr(httpx, "get", self._get)

    def _post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(
            {
                "method": "POST",
                "url": url,
                "headers": dict(kwargs.get("headers") or {}),
                "params": kwargs.get("params"),
            }
        )
        if url == CHALLENGE_URL:
            if self.unreachable:
                raise httpx.ConnectError("connection refused")
            return self._challenge(kwargs["json"])
        return self.restart

    def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(
            {
                "method": "GET",
                "url": url,
                "headers": dict(kwargs.get("headers") or {}),
                "params": None,
            }
        )
        return httpx.Response(200, json={"runs": []})

    def _challenge(self, body: dict[str, Any]) -> httpx.Response:
        request = ChallengeRequest.model_validate(body)
        nonce = base64.urlsafe_b64decode(request.nonce + "=" * (-len(request.nonce) % 4))
        claims = _challenge_claims(request)
        proof = challenge_proof(nonce, kind="managed", signing_key=self.key, claims=claims)
        return httpx.Response(200, json={"proof": proof})

    def sent(self) -> list[tuple[str, str]]:
        return [(call["method"], call["url"]) for call in self.calls]

    def bearer_sent_to(self) -> list[str]:
        return [
            call["url"]
            for call in self.calls
            if call["headers"].get("Authorization") == f"Bearer {TOKEN}"
        ]


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> _Daemon:
    return _Daemon(monkeypatch)


@pytest.fixture
def do_stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[MagicMock]:
    """A spawned seat with a private envelope and a spawn-time daemon URL, and no bootstrap.

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
    # The sandbox denies the operator bootstrap: any read of it is a failure.
    monkeypatch.setattr("gobby.utils.local_token.daemon_bootstrap_path", _forbidden("bootstrap"))
    monkeypatch.setattr("gobby.releases.activation.daemon_bootstrap_path", _forbidden("bootstrap"))
    monkeypatch.setattr("gobby.cli.runtime.get_cli_runtime", _forbidden("runtime"))
    monkeypatch.setattr(lifecycle, "restart_start_refusal", _forbidden("start proof"))
    stop = _forbidden("local stop")
    monkeypatch.setattr(lifecycle, "_do_stop", stop)
    monkeypatch.setattr(lifecycle, "is_test_protect_enabled", lambda: False)
    monkeypatch.setattr(lifecycle, "_is_daemon_healthy", lambda _port: True)
    monkeypatch.setattr(lifecycle, "clear_protected_runs", MagicMock(return_value=True))
    monkeypatch.setattr(lifecycle, "_is_process_alive", MagicMock(return_value=False))
    monkeypatch.setattr("gobby.cli.daemon_start._poll_startup_progress", lambda *_a, **_k: True)
    yield stop


def test_spawned_restart_proves_the_daemon_before_it_presents_the_managed_token(
    do_stop: MagicMock, daemon: _Daemon
) -> None:
    result = CliRunner().invoke(restart, [])

    assert result.exit_code == 0, result.output
    assert daemon.sent() == [("POST", CHALLENGE_URL), ("POST", ROUTE_URL)]
    assert "Authorization" not in daemon.calls[0]["headers"]
    assert daemon.calls[1]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert daemon.calls[1]["params"] == {"force": "false", "terminals": "false"}
    do_stop.assert_not_called()


def test_spawned_restart_forwards_the_force_flag_the_user_gave(
    do_stop: MagicMock, daemon: _Daemon
) -> None:
    result = CliRunner().invoke(restart, ["--force"])

    assert result.exit_code == 0, result.output
    assert daemon.calls[1]["params"]["force"] == "true"


def test_route_refusal_is_reported_and_the_daemon_is_left_alone(
    do_stop: MagicMock, daemon: _Daemon
) -> None:
    route_text = "managed_activation_refused: the verified definition does not hold activate_daemon"
    daemon.restart = httpx.Response(403, json={"detail": route_text})

    result = CliRunner().invoke(restart, [])

    assert result.exit_code == 1
    assert route_text in result.output
    do_stop.assert_not_called()


def test_spawned_restart_without_a_managed_envelope_sends_nothing(
    do_stop: MagicMock, daemon: _Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP")

    result = CliRunner().invoke(restart, [])

    assert result.exit_code == 1
    assert "no managed capability is presented for restart" in result.output
    assert daemon.calls == []
    do_stop.assert_not_called()


def test_spawned_restart_with_a_mismatched_envelope_owner_sends_nothing(
    do_stop: MagicMock, daemon: _Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_MANAGED_EXECUTION_ID", "exec-copied")

    result = CliRunner().invoke(restart, [])

    assert result.exit_code == 1
    assert "owner does not match" in result.output
    assert daemon.calls == []


def test_spawned_restart_without_a_daemon_url_sends_nothing(
    do_stop: MagicMock, daemon: _Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GOBBY_DAEMON_URL")

    result = CliRunner().invoke(restart, [])

    assert result.exit_code == 1
    assert "no daemon URL is presented" in result.output
    assert daemon.calls == []


@pytest.mark.parametrize("flags", [["--full"], ["--docker"]])
def test_spawned_whole_daemon_restart_stays_refused_before_any_request(
    do_stop: MagicMock, daemon: _Daemon, flags: list[str]
) -> None:
    result = CliRunner().invoke(restart, flags)

    assert result.exit_code == 1
    assert "spawned agents may not stop or start the daemon" in result.output
    assert daemon.calls == []
    do_stop.assert_not_called()


def test_unavailable_backend_is_refused_for_a_spawned_seat_not_stopped(
    do_stop: MagicMock, daemon: _Daemon
) -> None:
    daemon.restart = httpx.Response(503, json={"error": "unavailable"})

    result = CliRunner().invoke(restart, [])

    assert result.exit_code == 1
    assert "did not accept the restart request" in result.output
    do_stop.assert_not_called()


def test_a_listener_that_cannot_prove_the_capability_gets_no_bearer(
    do_stop: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    rogue = _Daemon(monkeypatch, key=OTHER_KEY)

    result = CliRunner().invoke(restart, [])

    assert result.exit_code == 1
    assert "did not prove" in result.output
    assert rogue.sent() == [("POST", CHALLENGE_URL)]
    assert rogue.bearer_sent_to() == []
    do_stop.assert_not_called()


def test_an_unreachable_listener_gets_no_bearer(do_stop: MagicMock, daemon: _Daemon) -> None:
    daemon.unreachable = True

    result = CliRunner().invoke(restart, [])

    assert result.exit_code == 1
    assert "did not prove" in result.output
    assert daemon.bearer_sent_to() == []
    do_stop.assert_not_called()


def test_the_protected_run_check_uses_the_proven_daemon_and_the_managed_token(
    do_stop: MagicMock, daemon: _Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "clear_protected_runs", real_clear_protected_runs)

    result = CliRunner().invoke(restart, [])

    assert result.exit_code == 0, result.output
    assert daemon.sent() == [
        ("POST", CHALLENGE_URL),
        ("GET", PROTECTED_RUNS_URL),
        ("POST", ROUTE_URL),
    ]
    assert daemon.bearer_sent_to() == [PROTECTED_RUNS_URL, ROUTE_URL]
    do_stop.assert_not_called()

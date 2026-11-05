"""A spawned seat's `gobby restart` posts to the daemon route and decides nothing locally.

The seat presents its private managed envelope and the daemon URL it was spawned with. It never
reads the operator bootstrap, never runs the local stop or start halves, and never proves the
start half itself: the route verifies the token with the daemon's key and installed definition.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from click.testing import CliRunner

from gobby.cli import daemon_lifecycle as lifecycle
from gobby.cli.daemon_lifecycle import restart

pytestmark = pytest.mark.unit

ROUTE_URL = "http://localhost:61987/api/admin/restart"
TOKEN = "managed-token-1"


def _forbidden(name: str) -> MagicMock:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(f"{name} must not run on a spawned seat's restart")

    return MagicMock(side_effect=refuse)


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
    monkeypatch.setenv("GOBBY_DAEMON_URL", "http://127.0.0.1:61987")
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


def _post(monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> MagicMock:
    post = MagicMock(return_value=response)
    monkeypatch.setattr("gobby.cli.daemon_lifecycle.httpx.post", post)
    return post


def test_spawned_restart_posts_its_managed_token_and_leaves_the_decision_to_the_route(
    do_stop: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    post = _post(monkeypatch, httpx.Response(200, json={"status": "restarting", "backend_pid": 42}))

    result = CliRunner().invoke(restart, [])

    assert result.exit_code == 0, result.output
    post.assert_called_once()
    assert post.call_args.args[0] == ROUTE_URL
    assert post.call_args.kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert post.call_args.kwargs["params"] == {"force": "false", "terminals": "false"}
    do_stop.assert_not_called()


def test_spawned_restart_forwards_the_force_flag_the_user_gave(
    do_stop: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    post = _post(monkeypatch, httpx.Response(200, json={"status": "restarting", "backend_pid": 42}))

    result = CliRunner().invoke(restart, ["--force"])

    assert result.exit_code == 0, result.output
    assert post.call_args.kwargs["params"]["force"] == "true"


def test_route_refusal_is_reported_and_the_daemon_is_left_alone(
    do_stop: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    route_text = "managed_activation_refused: the verified definition does not hold activate_daemon"
    _post(monkeypatch, httpx.Response(403, json={"detail": route_text}))

    result = CliRunner().invoke(restart, [])

    assert result.exit_code == 1
    assert route_text in result.output
    do_stop.assert_not_called()


def test_spawned_restart_without_a_managed_envelope_posts_nothing(
    do_stop: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GOBBY_MANAGED_EXECUTION_BOOTSTRAP")
    post = _post(monkeypatch, httpx.Response(200, json={}))

    result = CliRunner().invoke(restart, [])

    assert result.exit_code == 1
    assert "no managed capability is presented for restart" in result.output
    post.assert_not_called()
    do_stop.assert_not_called()


def test_spawned_restart_with_a_mismatched_envelope_owner_posts_nothing(
    do_stop: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_MANAGED_EXECUTION_ID", "exec-copied")
    post = _post(monkeypatch, httpx.Response(200, json={}))

    result = CliRunner().invoke(restart, [])

    assert result.exit_code == 1
    assert "owner does not match" in result.output
    post.assert_not_called()


def test_spawned_restart_without_a_daemon_url_posts_nothing(
    do_stop: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GOBBY_DAEMON_URL")
    post = _post(monkeypatch, httpx.Response(200, json={}))

    result = CliRunner().invoke(restart, [])

    assert result.exit_code == 1
    assert "no daemon URL is presented" in result.output
    post.assert_not_called()


@pytest.mark.parametrize("flags", [["--full"], ["--docker"]])
def test_spawned_whole_daemon_restart_stays_refused_before_any_post(
    do_stop: MagicMock, monkeypatch: pytest.MonkeyPatch, flags: list[str]
) -> None:
    post = _post(monkeypatch, httpx.Response(200, json={}))

    result = CliRunner().invoke(restart, flags)

    assert result.exit_code == 1
    assert "spawned agents may not stop or start the daemon" in result.output
    post.assert_not_called()
    do_stop.assert_not_called()


def test_unavailable_backend_is_refused_for_a_spawned_seat_not_stopped(
    do_stop: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _post(monkeypatch, httpx.Response(503, json={"error": "unavailable"}))

    result = CliRunner().invoke(restart, [])

    assert result.exit_code == 1
    assert "did not accept the restart request" in result.output
    do_stop.assert_not_called()

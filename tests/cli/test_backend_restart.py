"""CLI restart keeps native ownership while replacing only the backend."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import click
import httpx
import pytest
from click.testing import CliRunner

from gobby.cli import daemon_lifecycle as lifecycle
from gobby.cli.daemon_lifecycle import STARTUP_READINESS_TIMEOUT_SECONDS
from gobby.cli.runtime import CliRuntime
from gobby.config.app import DaemonConfig
from gobby.sessions.handoff_shutdown import HandoffShutdownBlocked

pytestmark = pytest.mark.unit


def test_restart_shares_warning_set_with_full_stop_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = MagicMock(return_value=None)
    stop = MagicMock(return_value=False)
    monkeypatch.setattr(lifecycle, "restart_start_refusal", lambda _ctx: None)
    monkeypatch.setattr(lifecycle, "_restart_backend", backend)
    monkeypatch.setattr(lifecycle, "_do_stop", stop)

    result = CliRunner().invoke(lifecycle.restart, ["--wait"])

    assert result.exit_code == 1
    backend.assert_called_once()
    stop.assert_called_once()
    assert (
        backend.call_args.kwargs["logged_stale_markers"]
        is stop.call_args.kwargs["logged_stale_markers"]
    )


@pytest.fixture
def restart_boundaries(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    runtime = MagicMock(spec=CliRuntime)
    runtime.read_only_operational_config.return_value = DaemonConfig(daemon_port=61987)
    monkeypatch.setattr("gobby.cli.runtime.get_cli_runtime", lambda _ctx: runtime)
    monkeypatch.setattr(lifecycle, "is_test_protect_enabled", lambda: False)
    monkeypatch.setattr(lifecycle, "_is_daemon_healthy", lambda _port: True)
    monkeypatch.setattr(lifecycle, "daemon_auth_headers", lambda: {"Authorization": "Bearer test"})
    monkeypatch.setattr(
        lifecycle, "protect_pending_handoffs", lambda *_args, **_kwargs: nullcontext()
    )
    guards = MagicMock(return_value=True)
    monkeypatch.setattr(lifecycle, "clear_protected_runs", guards)
    alive = MagicMock(side_effect=[True, False])
    monkeypatch.setattr(lifecycle, "_is_process_alive", alive)
    monkeypatch.setattr("gobby.cli.daemon_lifecycle.time.sleep", lambda _seconds: None)
    ready = MagicMock(return_value=True)
    monkeypatch.setattr("gobby.cli.daemon_start._poll_startup_progress", ready)
    post = MagicMock(
        return_value=httpx.Response(200, json={"status": "restarting", "backend_pid": 42})
    )
    monkeypatch.setattr("gobby.cli.daemon_lifecycle.httpx.post", post)
    return {"guards": guards, "alive": alive, "ready": ready, "post": post}


def test_backend_restart_waits_for_old_process_before_readiness(
    restart_boundaries: dict[str, MagicMock],
) -> None:
    result = lifecycle._restart_backend(
        click.Context(click.Command("restart")), force=False, wait=True, drain_terminals=True
    )
    assert result is True
    restart_boundaries["alive"].assert_called_with(42)
    assert restart_boundaries["alive"].call_count == 2
    restart_boundaries["post"].assert_called_once_with(
        "http://localhost:61987/api/admin/restart",
        params={"force": "true", "terminals": "true"},
        headers={"Authorization": "Bearer test"},
        timeout=5.0,
    )
    restart_boundaries["ready"].assert_called_once()
    assert restart_boundaries["ready"].call_args.args == (61987,)
    max_wait = restart_boundaries["ready"].call_args.kwargs["max_wait"]
    assert 0 < max_wait <= STARTUP_READINESS_TIMEOUT_SECONDS
    assert restart_boundaries["guards"].call_args.kwargs["wait"] is True


def test_backend_restart_exit_wait_and_readiness_share_one_deadline(
    monkeypatch: pytest.MonkeyPatch, restart_boundaries: dict[str, MagicMock]
) -> None:
    # The old backend outlives a 30 s exit wait on a loaded host, then exits.
    clock = iter([0.0, 45.0, 60.0])
    monkeypatch.setattr(
        lifecycle, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=MagicMock())
    )

    result = lifecycle._restart_backend(
        click.Context(click.Command("restart")), force=False, wait=True, drain_terminals=True
    )

    assert result is True
    restart_boundaries["ready"].assert_called_once_with(
        61987, max_wait=STARTUP_READINESS_TIMEOUT_SECONDS - 60.0
    )


def test_backend_restart_fails_when_old_process_outlives_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    restart_boundaries: dict[str, MagicMock],
) -> None:
    restart_boundaries["alive"].side_effect = None
    restart_boundaries["alive"].return_value = True
    clock = iter([0.0, STARTUP_READINESS_TIMEOUT_SECONDS])
    monkeypatch.setattr(
        lifecycle, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=MagicMock())
    )

    result = lifecycle._restart_backend(
        click.Context(click.Command("restart")), force=False, wait=True, drain_terminals=True
    )

    assert result is False
    restart_boundaries["ready"].assert_not_called()
    captured = capsys.readouterr()
    assert "Previous backend did not exit after restart request" in captured.out + captured.err


@pytest.mark.parametrize("status, expected", [(503, None), (409, False), (401, False)])
def test_backend_restart_falls_back_only_for_unavailable_backend(
    status: int, expected: bool | None, restart_boundaries: dict[str, MagicMock]
) -> None:
    restart_boundaries["post"].return_value = httpx.Response(status, json={"error": "unavailable"})
    assert (
        lifecycle._restart_backend(
            click.Context(click.Command("restart")), force=False, wait=False, drain_terminals=False
        )
        is expected
    )
    restart_boundaries["ready"].assert_not_called()
    restart_boundaries["alive"].assert_not_called()


def test_protected_cron_prevents_restart_request(restart_boundaries: dict[str, MagicMock]) -> None:
    restart_boundaries["guards"].return_value = False
    assert (
        lifecycle._restart_backend(
            click.Context(click.Command("restart")), force=False, wait=False, drain_terminals=False
        )
        is False
    )
    restart_boundaries["post"].assert_not_called()


def test_pending_handoff_prevents_restart_request(
    monkeypatch: pytest.MonkeyPatch, restart_boundaries: dict[str, MagicMock]
) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise HandoffShutdownBlocked("pending handoff")

    monkeypatch.setattr(lifecycle, "protect_pending_handoffs", blocked)
    assert (
        lifecycle._restart_backend(
            click.Context(click.Command("restart")), force=False, wait=False, drain_terminals=False
        )
        is False
    )
    restart_boundaries["post"].assert_not_called()


def test_serving_backend_transport_failure_does_not_stop_native_owner(
    restart_boundaries: dict[str, MagicMock],
) -> None:
    restart_boundaries["post"].side_effect = httpx.ReadError("connection ended")
    assert (
        lifecycle._restart_backend(
            click.Context(click.Command("restart")), force=False, wait=False, drain_terminals=False
        )
        is False
    )
    restart_boundaries["ready"].assert_not_called()

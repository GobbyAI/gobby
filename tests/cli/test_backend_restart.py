"""CLI restart keeps native ownership while replacing only the backend."""

from contextlib import nullcontext
from unittest.mock import MagicMock

import click
import httpx
import pytest

from gobby.cli import daemon_lifecycle as lifecycle
from gobby.cli.runtime import CliRuntime
from gobby.config.app import DaemonConfig
from gobby.sessions.handoff_shutdown import HandoffShutdownBlocked

pytestmark = pytest.mark.unit


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
    restart_boundaries["ready"].assert_called_once_with(61987)
    assert restart_boundaries["guards"].call_args.kwargs["wait"] is True


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

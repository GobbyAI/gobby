"""Regression tests for exclusive daemon PID-file ownership."""

from __future__ import annotations

import logging
import os
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gobby.runner_lifecycle as runner_lifecycle
from gobby.runner import GobbyRunner
from gobby.runner_pid_file import claim_pid_file
from tests.runner_helpers import create_base_patches

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("fast_stop_hook_grace_window")]


def test_claim_pid_file_refuses_to_overwrite_live_pid(tmp_path: Path) -> None:
    pid_file = tmp_path / "gobby.pid"
    pid_file.write_text("4242")

    with patch("gobby.runner_pid_file._pid_is_alive", return_value=True):
        claim = claim_pid_file(pid_file)

    assert claim is None
    assert pid_file.read_text() == "4242"


def test_second_pid_claim_preserves_winners_record(tmp_path: Path) -> None:
    pid_file = tmp_path / "gobby.pid"
    winner = claim_pid_file(pid_file)

    assert winner is not None
    try:
        loser = claim_pid_file(pid_file)
        assert loser is None
        assert pid_file.read_text() == str(os.getpid())
    finally:
        winner.release()


@pytest.mark.asyncio
async def test_serve_failure_cleans_up_and_exits_zero_when_winner_is_healthy(
    mock_config: MagicMock,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    patches = create_base_patches(mock_config=mock_config)
    pid_file = tmp_path / "gobby.pid"
    cleanup_pid_file = MagicMock(side_effect=lambda: pid_file.unlink(missing_ok=True))
    shutdown = AsyncMock()

    async def run_cleanup(*_args: object, **kwargs: object) -> None:
        cleanup = kwargs["cleanup_pid_file"]
        assert callable(cleanup)
        cleanup()

    shutdown.side_effect = run_cleanup
    with ExitStack() as stack:
        [stack.enter_context(item) for item in patches]
        stack.enter_context(patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path))
        stack.enter_context(patch("gobby.runner_maintenance.cleanup_pid_file", cleanup_pid_file))
        stack.enter_context(patch("gobby.runner_maintenance.setup_signal_handlers"))
        stack.enter_context(patch.object(runner_lifecycle, "_init_subsystems", AsyncMock()))
        stack.enter_context(patch.object(runner_lifecycle, "_start_periodic_tasks"))
        stack.enter_context(patch.object(runner_lifecycle, "shutdown_daemon_services", shutdown))
        stack.enter_context(patch("gobby.runner._healthy_daemon_running", return_value=True))
        stack.enter_context(patch("uvicorn.Config"))
        server_class = stack.enter_context(patch("uvicorn.Server"))
        stack.enter_context(
            patch(
                "gobby.servers.uvicorn_shutdown.install_uvicorn_shutdown_filter",
                return_value=MagicMock(),
            )
        )
        stack.enter_context(patch("gobby.servers.uvicorn_shutdown.remove_uvicorn_shutdown_filter"))

        server = SimpleNamespace(started=False, serve=AsyncMock(side_effect=SystemExit(1)))
        server_class.return_value = server
        runner = GobbyRunner()

        with caplog.at_level(logging.ERROR, logger="gobby.runner_lifecycle"):
            await runner.run()

    shutdown.assert_awaited_once()
    cleanup_pid_file.assert_called_once()
    assert runner._shutdown_requested is True
    assert "HTTP server failed before binding (SystemExit(1))" in caplog.text
    assert "requesting daemon shutdown" in caplog.text
    assert not pid_file.exists()

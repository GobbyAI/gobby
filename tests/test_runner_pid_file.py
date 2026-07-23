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
from gobby.runner import GobbyRunner, main, run_gobby
from gobby.runner_pid_file import FailOpenPidOwnership, claim_pid_file, probe_daemon_lock
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


def test_claim_records_owner_pid_in_lock_file(tmp_path: Path) -> None:
    pid_file = tmp_path / "gobby.pid"
    claim = claim_pid_file(pid_file)

    assert claim is not None
    try:
        assert (tmp_path / "gobby.pid.lock").read_text() == str(os.getpid())
    finally:
        claim.release()


def test_probe_daemon_lock_returns_none_when_free(tmp_path: Path) -> None:
    pid_file = tmp_path / "gobby.pid"

    assert probe_daemon_lock(pid_file) is None

    claim = claim_pid_file(pid_file)
    assert claim is not None
    claim.release()
    assert probe_daemon_lock(pid_file) is None


def test_probe_daemon_lock_reports_owner_while_held(tmp_path: Path) -> None:
    pid_file = tmp_path / "gobby.pid"
    claim = claim_pid_file(pid_file)

    assert claim is not None
    try:
        assert probe_daemon_lock(pid_file) == os.getpid()
    finally:
        claim.release()


async def test_run_gobby_contention_returns_before_runner_construction(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A losing daemon returns before any mutable subsystem is constructed."""
    with (
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.runner_pid_file.claim_pid_file", return_value=None),
        patch("gobby.runner_pid_file.probe_daemon_lock", return_value=4242),
        patch("gobby.runner.GobbyRunner") as runner_cls,
        caplog.at_level(logging.INFO, logger="gobby.runner"),
    ):
        await run_gobby()

    runner_cls.assert_not_called()
    assert "4242" in caplog.text
    assert "exiting cleanly" in caplog.text


def test_main_exits_zero_on_lock_contention(tmp_path: Path) -> None:
    """A launchd respawn losing the early claim exits 0 (no hot loop)."""
    bootstrap = MagicMock(daemon_port=8765, bind_host="localhost")
    with (
        patch("gobby.config.bootstrap.load_bootstrap", return_value=bootstrap),
        patch("gobby.runner._healthy_daemon_running", return_value=False),
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.runner_pid_file.claim_pid_file", return_value=None),
        patch("gobby.runner_pid_file.probe_daemon_lock", return_value=4242),
        patch("asyncio.run") as mock_asyncio_run,
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == 0
    mock_asyncio_run.assert_not_called()


def test_main_passes_early_claim_to_run_gobby(tmp_path: Path) -> None:
    class RecordingClaim:
        def __init__(self) -> None:
            self.release_count = 0

        def release(self) -> None:
            self.release_count += 1

    bootstrap = MagicMock(daemon_port=8765, bind_host="localhost")
    claim = RecordingClaim()
    with (
        patch("gobby.config.bootstrap.load_bootstrap", return_value=bootstrap),
        patch("gobby.runner._healthy_daemon_running", return_value=False),
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.runner_pid_file.claim_pid_file", return_value=claim),
        patch("asyncio.run"),
        patch("gobby.runner.run_gobby") as mock_run_gobby,
    ):
        mock_run_gobby.return_value = None
        main()

    assert mock_run_gobby.call_args.kwargs["ownership_resolution"] is claim
    assert claim.release_count == 1


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
            await runner.run(ownership_resolution=FailOpenPidOwnership("test"))

    shutdown.assert_awaited_once()
    cleanup_pid_file.assert_called_once()
    assert runner._shutdown_requested is True
    assert "HTTP server failed before binding (SystemExit(1))" in caplog.text
    assert "requesting daemon shutdown" in caplog.text
    assert not pid_file.exists()


def test_ownership_parameters_have_no_default() -> None:
    """Plan 1.4.19-1.4.21 witness: ownership must be resolved before construction,
    so neither entry point may offer a defaulted ownership parameter."""
    import inspect

    from gobby.runner import GobbyRunner
    from gobby.runner_lifecycle import run_daemon

    run_param = inspect.signature(GobbyRunner.run).parameters["ownership_resolution"]
    daemon_param = inspect.signature(run_daemon).parameters["ownership_resolution"]
    assert run_param.default is inspect.Parameter.empty
    assert daemon_param.default is inspect.Parameter.empty
    assert run_param.kind is inspect.Parameter.KEYWORD_ONLY
    assert daemon_param.kind is inspect.Parameter.KEYWORD_ONLY

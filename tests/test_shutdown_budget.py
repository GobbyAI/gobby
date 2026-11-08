"""Shutdown budgets stack inside the CLI stop escalation, read from one shared constant."""

from __future__ import annotations

import asyncio
import os
import signal
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

import gobby.cli.utils_shutdown as utils_shutdown
from gobby import runner_lifecycle_shutdown, runner_shutdown_storage, shutdown_budget
from gobby.cli.utils import stop_daemon
from gobby.runner import GobbyRunner


def test_worktree_delete_join_is_bounded_and_leaves_a_stuck_delete(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A worktree delete that outlives the join bound is logged and left; shutdown returns."""

    class StuckWorktreeDeleteExecutor:
        def is_joined(self) -> bool:
            return False

        def shutdown(self, *, cancel_futures: bool = True) -> None:
            return None

        def join(self) -> None:
            # A delete that never returns. The daemon join thread stays blocked, so it
            # never calls back into the closed test loop.
            threading.Event().wait()

    monkeypatch.setattr(runner_shutdown_storage, "_WORKTREE_DELETE_JOIN_SECONDS", 0.05)
    runner = SimpleNamespace(
        database_watchdog=None,
        worktree_delete_executor=StuckWorktreeDeleteExecutor(),
        coverage_executor=None,
        db_executor=None,
    )

    async def run() -> None:
        async with asyncio.timeout(5):
            await runner_shutdown_storage._shutdown_database_concurrency(cast(GobbyRunner, runner))

    asyncio.run(run())
    assert "Worktree delete executor did not settle" in caplog.text


def test_daemon_worst_case_stacks_under_the_escalation_with_margin() -> None:
    """Overall deadline, HTTP, fallback reap, finalizer and DB tail, summed as they stack."""
    worst_case = (
        runner_lifecycle_shutdown._OVERALL_SHUTDOWN_DEADLINE_SECONDS
        # Request cancel, then server wait, each bounded by the same timeout.
        + 2 * runner_lifecycle_shutdown._HTTP_REQUEST_TASK_CANCEL_TIMEOUT_SECONDS
        + runner_lifecycle_shutdown._DEADLINE_CHILD_REAP_BUDGET_SECONDS
        + runner_lifecycle_shutdown._FINALIZER_SETTLE_SECONDS
        # Coverage, database and worktree-delete joins, each with its own bound.
        + runner_shutdown_storage._COVERAGE_EXECUTOR_JOIN_SECONDS
        + runner_shutdown_storage._DATABASE_EXECUTOR_JOIN_SECONDS
        + runner_shutdown_storage._WORKTREE_DELETE_JOIN_SECONDS
    )
    limit = shutdown_budget.DAEMON_STOP_ESCALATION_SECONDS - shutdown_budget.SHUTDOWN_MARGIN_SECONDS
    assert worst_case <= limit, f"worst case {worst_case}s exceeds {limit}s"


def test_cli_stop_escalation_waits_the_shared_constant_before_sigkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SIGTERM wait before SIGKILL comes from the shared escalation constant."""
    monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)
    monkeypatch.setattr(utils_shutdown, "DAEMON_STOP_ESCALATION_SECONDS", 0.2, raising=False)
    safe_home = Path(os.environ["GOBBY_HOME"])
    (safe_home / "gobby.pid").write_text("12345")

    # Each entry is (signal sent, poll sleeps taken so far).
    kills: list[tuple[int, int]] = []
    sleeps: list[float] = []

    def fake_kill(pid: int, sig: int) -> None:
        kills.append((sig, len(sleeps)))

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def fake_alive(pid: int) -> bool:
        return not any(sig == signal.SIGKILL for sig, _ in kills)

    mock_proc = MagicMock()
    mock_proc.cmdline.return_value = ["python", "-m", "gobby.runner"]

    with (
        patch("gobby.cli.utils.kill_all_gobby_daemons", return_value=0),
        patch("gobby.cli.utils.stop_ui_server"),
        patch("gobby.cli.utils._is_process_alive", side_effect=fake_alive),
        patch("gobby.cli.utils.psutil.Process", return_value=mock_proc),
        patch("gobby.cli.utils.os.kill", side_effect=fake_kill),
        patch("gobby.cli.utils.time.sleep", side_effect=fake_sleep),
        patch(
            "gobby.cli.installers.service.get_service_status",
            return_value={"installed": False, "running": False},
        ),
    ):
        assert stop_daemon(quiet=True) is True

    sigkill_polls = [polls for sig, polls in kills if sig == signal.SIGKILL]
    # 0.2 s at 0.1 s per poll is two polls before the force kill.
    assert sigkill_polls == [2]

"""Tests for start_run_or_cleanup lost-CAS tolerance on the fresh-spawn path."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.spawn_agent import _failure_cleanup

pytestmark = pytest.mark.unit


def _runner(
    start_result: object = None,
    *,
    current_status: str | None = None,
    start_error: Exception | None = None,
) -> SimpleNamespace:
    run_storage = MagicMock()
    if start_error is not None:
        run_storage.start.side_effect = start_error
    else:
        run_storage.start.return_value = start_result
    run_storage.get.return_value = (
        SimpleNamespace(status=current_status) if current_status is not None else None
    )
    return SimpleNamespace(run_storage=run_storage)


async def _start_run_or_cleanup(runner: SimpleNamespace) -> dict[str, object] | None:
    return await _failure_cleanup.start_run_or_cleanup(
        runner,
        "run-1",
        MagicMock(),
        MagicMock(),
        completion_registry=None,
        cleanup_isolation=True,
        child_session_id="child-1",
    )


@pytest.mark.asyncio
async def test_start_cas_win_returns_success_without_cleanup() -> None:
    runner = _runner(SimpleNamespace(id="run-1", status="running"))

    with patch.object(_failure_cleanup, "cleanup_failed_spawn", AsyncMock()) as cleanup:
        result = await _start_run_or_cleanup(runner)

    assert result is None
    cleanup.assert_not_awaited()
    runner.run_storage.get.assert_not_called()


@pytest.mark.asyncio
async def test_lost_cas_with_running_run_treats_hook_win_as_success() -> None:
    """H4: SessionStart hook won the start race — no cleanup, tmux survives."""
    runner = _runner(None, current_status="running")

    with patch.object(_failure_cleanup, "cleanup_failed_spawn", AsyncMock()) as cleanup:
        result = await _start_run_or_cleanup(runner)

    assert result is None
    cleanup.assert_not_awaited()
    runner.run_storage.get.assert_called_once_with("run-1")


@pytest.mark.asyncio
async def test_lost_cas_with_non_running_run_cleans_up_and_reports_error() -> None:
    runner = _runner(None, current_status="cancelled")

    with patch.object(_failure_cleanup, "cleanup_failed_spawn", AsyncMock()) as cleanup:
        result = await _start_run_or_cleanup(runner)

    assert result == {
        "success": False,
        "error": "Agent run was no longer pending after spawn",
        "run_id": "run-1",
        "child_session_id": "child-1",
    }
    cleanup.assert_awaited_once()
    assert cleanup.await_args is not None
    assert cleanup.await_args.args[1] == "run-1"
    assert cleanup.await_args.kwargs == {
        "completion_registry": None,
        "cleanup_isolation": True,
        "child_session_id": "child-1",
    }


@pytest.mark.asyncio
async def test_start_raising_cleans_up_and_reports_error() -> None:
    runner = _runner(start_error=RuntimeError("db down"))

    with patch.object(_failure_cleanup, "cleanup_failed_spawn", AsyncMock()) as cleanup:
        result = await _start_run_or_cleanup(runner)

    assert result == {
        "success": False,
        "error": "Failed to mark agent run run-1 as running: db down",
        "run_id": "run-1",
        "child_session_id": "child-1",
    }
    cleanup.assert_awaited_once()
    runner.run_storage.get.assert_not_called()

"""Tests for start_run_or_cleanup lost-CAS tolerance on the fresh-spawn path."""

from __future__ import annotations

import signal
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.spawn_agent import _failure_cleanup

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_spawn_rollback_uses_shared_cancelled_terminalization() -> None:
    events: list[str] = []
    terminalize_arguments: dict[str, Any] = {}

    class RunStorage:
        db = object()

        def get(self, _run_id: str) -> None:
            return None

    async def terminalize(**kwargs: Any) -> bool:
        events.append("terminalize")
        terminalize_arguments.update(kwargs)
        return True

    async def cleanup_isolation(*_args: Any, **_kwargs: Any) -> None:
        events.append("cleanup-isolation")

    def delete_child(*_args: Any, **_kwargs: Any) -> None:
        events.append("delete-child")

    run_storage = RunStorage()
    runner = SimpleNamespace(run_storage=run_storage, agent_lifecycle_monitor=None)
    handler = SimpleNamespace()
    completion_registry = object()
    task_manager = object()
    with (
        patch(
            "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run",
            terminalize,
        ),
        patch.object(_failure_cleanup, "cleanup_created_isolation", cleanup_isolation),
        patch.object(_failure_cleanup, "_delete_child_session", delete_child),
    ):
        await _failure_cleanup.cleanup_failed_spawn(
            runner,
            "run-1",
            "spawn failed",
            handler,
            SimpleNamespace(),
            completion_registry=completion_registry,
            cleanup_isolation=False,
            task_manager=task_manager,
        )

    assert events == ["terminalize", "cleanup-isolation", "delete-child"]
    assert terminalize_arguments == {
        "runner": runner,
        "run_id": "run-1",
        "terminal_reason": "spawn_rollback",
        "lifecycle_monitor": None,
        "completion_registry": completion_registry,
        "task_manager": task_manager,
        "message": "spawn failed",
    }


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
        task_manager=None,
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
        "task_manager": None,
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


@pytest.mark.asyncio
async def test_terminate_does_not_sigkill_after_process_exits() -> None:
    sent: list[int] = []

    def fake_kill(_pid: int, sig: int) -> None:
        if sig == 0:
            raise ProcessLookupError
        sent.append(sig)

    cleanup_module = cast(Any, _failure_cleanup)
    with (
        patch.object(cleanup_module, "os") as mock_os,
        patch.object(cleanup_module, "asyncio") as mock_asyncio,
    ):
        mock_os.kill.side_effect = fake_kill
        mock_asyncio.sleep = AsyncMock()
        await _failure_cleanup._terminate_spawn_process(
            pid=4242,
            tmux_session_name=None,
            tmux_socket_name=None,
            tmux_socket_path=None,
        )

    assert sent == [signal.SIGTERM]


@pytest.mark.asyncio
async def test_terminate_sigkills_only_when_pid_still_alive() -> None:
    sent: list[int] = []

    def fake_kill(_pid: int, sig: int) -> None:
        sent.append(sig)

    cleanup_module = cast(Any, _failure_cleanup)
    with (
        patch.object(cleanup_module, "os") as mock_os,
        patch.object(cleanup_module, "asyncio") as mock_asyncio,
    ):
        mock_os.kill.side_effect = fake_kill
        mock_asyncio.sleep = AsyncMock()
        await _failure_cleanup._terminate_spawn_process(
            pid=4242,
            tmux_session_name=None,
            tmux_socket_name=None,
            tmux_socket_path=None,
        )

    assert sent == [signal.SIGTERM, 0, signal.SIGKILL]


async def test_get_after_lost_start_race_cleans_up_on_storage_error() -> None:
    runner = _runner(None)
    runner.run_storage.get.side_effect = RuntimeError("read failed")

    with patch.object(_failure_cleanup, "cleanup_failed_spawn", AsyncMock()) as cleanup:
        result = await _start_run_or_cleanup(runner)

    assert result == {
        "success": False,
        "error": "Failed to read agent run run-1 after start conflict: read failed",
        "run_id": "run-1",
        "child_session_id": "child-1",
    }
    cleanup.assert_awaited_once()

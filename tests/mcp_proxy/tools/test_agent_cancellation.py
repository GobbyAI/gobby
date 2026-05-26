"""Tests for shared agent cancellation helpers."""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.agent_cancellation import (
    stop_agent_run,
    terminalize_cancelled_agent_run,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_terminalize_cancelled_agent_run_uses_lifecycle_monitor() -> None:
    runner = MagicMock()
    lifecycle_monitor = MagicMock()
    lifecycle_monitor.terminalize_cancelled_run = AsyncMock(return_value=True)
    completion_registry = MagicMock()
    completion_registry.notify = AsyncMock()

    transitioned = await terminalize_cancelled_agent_run(
        runner=runner,
        run_id="run-123",
        terminal_reason="user_cancelled",
        lifecycle_monitor=lifecycle_monitor,
        completion_registry=completion_registry,
        task_manager=MagicMock(),
    )

    assert transitioned is True
    assert isinstance(transitioned, bool)
    lifecycle_monitor.terminalize_cancelled_run.assert_awaited_once_with(
        "run-123",
        terminal_reason="user_cancelled",
    )
    runner.cancel_run.assert_not_called()
    completion_registry.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminalize_cancelled_agent_run_fallback_recovers_task_claim() -> None:
    runner = MagicMock()
    runner.cancel_run.return_value = True
    runner.run_storage = MagicMock()
    cancelled_run = MagicMock()
    runner.run_storage.get.return_value = cancelled_run
    completion_registry = MagicMock()
    completion_registry.notify = AsyncMock()
    task_manager = MagicMock()

    with patch("gobby.mcp_proxy.tools.agent_cancellation.TaskRecoveryHandler") as recovery_cls:
        recovery = recovery_cls.return_value
        recovery.recover_task_from_terminal_agent = AsyncMock()

        transitioned = await terminalize_cancelled_agent_run(
            runner=runner,
            run_id="run-123",
            terminal_reason="user_cancelled",
            lifecycle_monitor=None,
            completion_registry=completion_registry,
            task_manager=task_manager,
            message="Agent run-123 cancelled",
        )

    assert transitioned is True
    assert isinstance(transitioned, bool)
    runner.cancel_run.assert_called_once_with("run-123")
    runner.run_storage.get.assert_called_once_with("run-123")
    recovery_cls.assert_called_once_with(task_manager, runner.run_storage, ANY)
    recovery.recover_task_from_terminal_agent.assert_awaited_once_with(
        cancelled_run,
        outcome="cancelled",
    )
    completion_registry.notify.assert_awaited_once_with(
        "run-123",
        {
            "status": "cancelled",
            "terminal_reason": "user_cancelled",
            "run_id": "run-123",
        },
        message="Agent run-123 cancelled",
    )


@pytest.mark.asyncio
async def test_terminalize_cancelled_agent_run_fallback_skips_recovery_when_not_transitioned() -> (
    None
):
    runner = MagicMock()
    runner.cancel_run.return_value = False
    runner.run_storage = MagicMock()
    completion_registry = MagicMock()
    completion_registry.notify = AsyncMock()

    transitioned = await terminalize_cancelled_agent_run(
        runner=runner,
        run_id="run-123",
        terminal_reason="user_cancelled",
        lifecycle_monitor=None,
        completion_registry=completion_registry,
        task_manager=MagicMock(),
    )

    assert transitioned is False
    assert isinstance(transitioned, bool)
    runner.cancel_run.assert_called_once_with("run-123")
    runner.run_storage.get.assert_not_called()
    completion_registry.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_agent_run_happy_path_call_order() -> None:
    call_order: list[str] = []
    run = MagicMock()
    run.id = "run-123"
    run.status = "pending"
    run.tmux_session_name = "tmux-run-123"
    run.child_session_id = "child-session-123"

    runner = MagicMock()
    runner.get_run.return_value = run
    agent_run_manager = MagicMock()
    agent_run_manager.db = object()

    async def kill_side_effect(*_args: object, **_kwargs: object) -> dict[str, bool]:
        call_order.append("kill")
        return {"success": True}

    async def terminalize_side_effect(**_kwargs: object) -> bool:
        call_order.append("terminalize")
        return True

    async def cleanup_side_effect(**_kwargs: object) -> None:
        call_order.append("cleanup")

    kill_agent_process = AsyncMock(side_effect=kill_side_effect)
    cleanup_terminal_artifacts = AsyncMock(side_effect=cleanup_side_effect)

    with patch(
        "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run",
        new_callable=AsyncMock,
        side_effect=terminalize_side_effect,
    ) as terminalize:
        result = await stop_agent_run(
            run_id="run-123",
            runner=runner,
            agent_run_manager=agent_run_manager,
            db=None,
            lifecycle_monitor=None,
            completion_registry=None,
            task_manager=None,
            session_manager=None,
            hook_manager_resolver=None,
            kill_agent_process=kill_agent_process,
            cleanup_terminal_artifacts=cleanup_terminal_artifacts,
        )

    assert result == {
        "success": True,
        "message": "Agent run run-123 stopped",
        "run_id": "run-123",
        "status": "cancelled",
        "terminal_reason": "user_cancelled",
    }
    assert call_order == ["kill", "terminalize", "cleanup"]
    kill_agent_process.assert_awaited_once_with(
        run,
        agent_run_manager.db,
        signal_name="TERM",
        close_terminal=True,
    )
    terminalize.assert_awaited_once_with(
        runner=runner,
        run_id="run-123",
        terminal_reason="user_cancelled",
        lifecycle_monitor=None,
        completion_registry=None,
        task_manager=None,
        message="Agent run-123 cancelled",
    )
    cleanup_terminal_artifacts.assert_awaited_once_with(
        run_id="run-123",
        db=agent_run_manager.db,
        tmux_session_name="tmux-run-123",
        agent_session_id="child-session-123",
        debug=False,
        session_manager=None,
        hook_manager_resolver=None,
        result={"success": True},
    )

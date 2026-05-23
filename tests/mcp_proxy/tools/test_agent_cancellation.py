"""Tests for shared agent cancellation helpers."""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.agent_cancellation import terminalize_cancelled_agent_run

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
    runner.cancel_run.assert_called_once_with("run-123")
    runner.run_storage.get.assert_not_called()
    completion_registry.notify.assert_not_awaited()

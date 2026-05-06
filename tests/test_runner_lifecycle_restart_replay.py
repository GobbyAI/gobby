"""Focused tests for agent restart cancellation replay."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gobby.runner_lifecycle as runner_lifecycle

pytestmark = pytest.mark.unit


class TestDaemonRestartAgentCancellationReplay:
    """Replay terminal daemon-restart cancellations after startup."""

    @pytest.mark.asyncio
    async def test_replay_notifies_subscribers_and_cleans_up(self) -> None:
        run = SimpleNamespace(
            id="run-1",
            terminal_reason="daemon_restart",
            continuation_prompt="Inspect the interrupted agent result",
        )
        runner = SimpleNamespace(
            agent_runner=SimpleNamespace(
                run_storage=SimpleNamespace(list_by_status=MagicMock(return_value=[run]))
            ),
            pipeline_execution_manager=SimpleNamespace(
                get_completion_subscribers=MagicMock(return_value=["parent-1"]),
                remove_completion_subscribers=MagicMock(),
            ),
            completion_registry=SimpleNamespace(
                is_registered=MagicMock(return_value=False),
                register=MagicMock(),
                notify=AsyncMock(),
                cleanup=MagicMock(),
            ),
        )

        replayed = await runner_lifecycle._replay_daemon_restart_agent_cancellations(runner)

        assert replayed == 1
        assert runner.completion_registry.notify.await_count == 1
        runner.agent_runner.run_storage.list_by_status.assert_called_once_with(
            "cancelled",
            limit=500,
        )
        runner.completion_registry.register.assert_called_once_with(
            "run-1",
            subscribers=["parent-1"],
            continuation_prompt="Inspect the interrupted agent result",
        )
        runner.completion_registry.notify.assert_awaited_once_with(
            "run-1",
            result={
                "status": "cancelled",
                "terminal_reason": "daemon_restart",
                "run_id": "run-1",
                "completion_id": "run-1",
            },
            message=(
                "Agent run-1 was interrupted by a daemon restart.\n"
                "Status: cancelled (daemon restarted)"
            ),
        )
        runner.pipeline_execution_manager.remove_completion_subscribers.assert_called_once_with(
            "run-1"
        )
        runner.completion_registry.cleanup.assert_called_once_with("run-1")

    @pytest.mark.asyncio
    async def test_replay_keeps_subscribers_when_notify_fails(self) -> None:
        run = SimpleNamespace(id="run-1", terminal_reason="daemon_restart")
        runner = SimpleNamespace(
            agent_runner=SimpleNamespace(
                run_storage=SimpleNamespace(list_by_status=MagicMock(return_value=[run]))
            ),
            pipeline_execution_manager=SimpleNamespace(
                get_completion_subscribers=MagicMock(return_value=["parent-1"]),
                remove_completion_subscribers=MagicMock(),
            ),
            completion_registry=SimpleNamespace(
                is_registered=MagicMock(return_value=False),
                register=MagicMock(),
                notify=AsyncMock(side_effect=RuntimeError("wake failed")),
                cleanup=MagicMock(),
            ),
        )

        replayed = await runner_lifecycle._replay_daemon_restart_agent_cancellations(runner)

        assert replayed == 0
        assert runner.completion_registry.notify.await_count == 1
        runner.pipeline_execution_manager.remove_completion_subscribers.assert_not_called()
        runner.completion_registry.cleanup.assert_not_called()

    @pytest.mark.asyncio
    async def test_replay_ignores_other_cancelled_runs(self) -> None:
        run = SimpleNamespace(id="run-1", terminal_reason="user_cancelled")
        runner = SimpleNamespace(
            agent_runner=SimpleNamespace(
                run_storage=SimpleNamespace(list_by_status=MagicMock(return_value=[run]))
            ),
            pipeline_execution_manager=SimpleNamespace(
                get_completion_subscribers=MagicMock(),
                remove_completion_subscribers=MagicMock(),
            ),
            completion_registry=SimpleNamespace(
                is_registered=MagicMock(),
                register=MagicMock(),
                notify=AsyncMock(),
                cleanup=MagicMock(),
            ),
        )

        replayed = await runner_lifecycle._replay_daemon_restart_agent_cancellations(runner)

        assert replayed == 0
        assert runner.completion_registry.notify.await_count == 0
        runner.pipeline_execution_manager.get_completion_subscribers.assert_not_called()
        runner.completion_registry.notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_replay_cleans_lingering_daemon_restart_tmux_session(self) -> None:
        run = SimpleNamespace(
            id="run-1",
            terminal_reason="daemon_restart",
            tmux_session_name="gobby-stale-agent",
        )
        tmux_manager = SimpleNamespace(kill_session=AsyncMock(return_value=True))
        runner = SimpleNamespace(
            agent_runner=SimpleNamespace(
                run_storage=SimpleNamespace(list_by_status=MagicMock(return_value=[run]))
            ),
            pipeline_execution_manager=SimpleNamespace(
                get_completion_subscribers=MagicMock(return_value=[]),
                remove_completion_subscribers=MagicMock(),
            ),
            completion_registry=SimpleNamespace(
                is_registered=MagicMock(),
                register=MagicMock(),
                notify=AsyncMock(),
                cleanup=MagicMock(),
            ),
        )

        with patch(
            "gobby.agents.tmux.session_manager.TmuxSessionManager",
            return_value=tmux_manager,
        ):
            replayed = await runner_lifecycle._replay_daemon_restart_agent_cancellations(runner)

        assert replayed == 0
        tmux_manager.kill_session.assert_awaited_once_with("gobby-stale-agent")
        runner.completion_registry.notify.assert_not_awaited()

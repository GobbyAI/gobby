"""Focused tests for agent restart reconciliation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gobby.runner_lifecycle as runner_lifecycle

pytestmark = pytest.mark.unit


class TestAgentRestartReconciliation:
    """Recover preserved tmux-backed agents after daemon startup."""

    @pytest.mark.asyncio
    async def test_reconcile_live_tmux_run_refreshes_pid_and_reader(self) -> None:
        run = SimpleNamespace(
            id="run-1",
            tmux_session_name="gobby-run-1",
            pid=111,
            continuation_prompt="continue later",
        )
        run_storage = SimpleNamespace(
            list_active=MagicMock(return_value=[run]),
            update_runtime=MagicMock(),
        )
        runner = self._runner(run_storage)
        tmux_manager = SimpleNamespace(
            list_sessions=AsyncMock(
                return_value=[
                    SimpleNamespace(name="gobby-run-1", pane_pid=222, pane_dead=False),
                ]
            )
        )
        output_reader = SimpleNamespace(start_reader=AsyncMock(return_value=True))

        with (
            patch(
                "gobby.agents.tmux.session_manager.TmuxSessionManager",
                return_value=tmux_manager,
            ),
            patch("gobby.agents.tmux.get_tmux_output_reader", return_value=output_reader),
        ):
            reconciled = await runner_lifecycle._reconcile_agent_runs_after_restart(runner)

        assert reconciled == 3
        runner.completion_registry.register.assert_called_once_with(
            "run-1",
            subscribers=["parent-1"],
            continuation_prompt="continue later",
        )
        run_storage.update_runtime.assert_called_once_with(
            "run-1",
            pid=222,
            tmux_session_name="gobby-run-1",
        )
        output_reader.start_reader.assert_awaited_once_with("run-1", "gobby-run-1")
        runner.agent_lifecycle_monitor.cleanup_agent.assert_not_awaited()
        runner.agent_lifecycle_monitor.get_cleanup_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconcile_missing_tmux_session_cleans_run(self) -> None:
        run = SimpleNamespace(id="run-1", tmux_session_name="gobby-run-1", pid=111)
        run_storage = SimpleNamespace(
            list_active=MagicMock(return_value=[run]),
            update_runtime=MagicMock(),
        )
        runner = self._runner(run_storage)
        tmux_manager = SimpleNamespace(list_sessions=AsyncMock(return_value=[]))

        with patch(
            "gobby.agents.tmux.session_manager.TmuxSessionManager",
            return_value=tmux_manager,
        ):
            reconciled = await runner_lifecycle._reconcile_agent_runs_after_restart(runner)

        assert reconciled == 2
        runner.agent_lifecycle_monitor.get_cleanup_agent.assert_called_once_with()
        runner.agent_lifecycle_monitor.cleanup_agent.assert_awaited_once()
        cleanup_call = runner.agent_lifecycle_monitor.cleanup_agent.await_args
        assert cleanup_call.args[0] is run
        assert "tmux session 'gobby-run-1' was missing" in cleanup_call.kwargs["terminal_payload"]
        run_storage.update_runtime.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconcile_dead_tmux_pane_cleans_run(self) -> None:
        run = SimpleNamespace(id="run-1", tmux_session_name="gobby-run-1", pid=111)
        run_storage = SimpleNamespace(list_active=MagicMock(return_value=[run]))
        runner = self._runner(run_storage)
        tmux_manager = SimpleNamespace(
            list_sessions=AsyncMock(
                return_value=[
                    SimpleNamespace(name="gobby-run-1", pane_pid=222, pane_dead=True),
                ]
            )
        )

        with patch(
            "gobby.agents.tmux.session_manager.TmuxSessionManager",
            return_value=tmux_manager,
        ):
            reconciled = await runner_lifecycle._reconcile_agent_runs_after_restart(runner)

        assert reconciled == 2
        runner.agent_lifecycle_monitor.get_cleanup_agent.assert_called_once_with()
        runner.agent_lifecycle_monitor.cleanup_agent.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reconcile_active_non_tmux_run_only_hydrates_completion(self) -> None:
        run = SimpleNamespace(id="run-1", tmux_session_name=None, continuation_prompt=None)
        run_storage = SimpleNamespace(list_active=MagicMock(return_value=[run]))
        runner = self._runner(run_storage)

        reconciled = await runner_lifecycle._reconcile_agent_runs_after_restart(runner)

        assert reconciled == 1
        runner.completion_registry.register.assert_called_once_with(
            "run-1",
            subscribers=["parent-1"],
            continuation_prompt=None,
        )

    def _runner(self, run_storage: SimpleNamespace) -> SimpleNamespace:
        cleanup_agent = AsyncMock()
        return SimpleNamespace(
            agent_runner=SimpleNamespace(run_storage=run_storage),
            agent_lifecycle_monitor=SimpleNamespace(
                get_cleanup_agent=MagicMock(return_value=cleanup_agent),
                cleanup_agent=cleanup_agent,
            ),
            pipeline_execution_manager=SimpleNamespace(
                get_completion_subscribers=MagicMock(return_value=["parent-1"]),
            ),
            completion_registry=SimpleNamespace(
                is_registered=MagicMock(return_value=False),
                register=MagicMock(),
            ),
        )

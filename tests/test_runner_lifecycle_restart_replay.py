"""Focused tests for agent restart reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

import gobby.runner_lifecycle as runner_lifecycle
from gobby.agents.tmux import configure_tmux, get_tmux_output_reader, get_tmux_session_manager
from gobby.config.tmux import TmuxConfig
from gobby.runner_lifecycle_agents import _RUN_REPLAY_PAGE_SIZE, _list_active_agent_runs_once
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager

pytestmark = pytest.mark.unit


class TestAgentRestartReconciliation:
    """Recover preserved tmux-backed agents after daemon startup."""

    @pytest.mark.asyncio
    async def test_recover_agent_runs_after_restart_paginates_active_runs(self) -> None:
        page_size = _RUN_REPLAY_PAGE_SIZE
        runs = [
            SimpleNamespace(id=f"run-{index}", continuation_prompt=None)
            for index in range(page_size + 3)
        ]
        list_active = MagicMock(
            side_effect=lambda *, limit, offset=0: runs[offset : offset + limit]
        )
        runner = self._runner(SimpleNamespace(list_active=list_active))

        rehydrated = await runner_lifecycle._recover_agent_runs_after_restart(runner)

        assert rehydrated == page_size + 3
        assert list_active.call_args_list == [
            call(limit=page_size, offset=0),
            call(limit=page_size, offset=page_size),
        ]
        assert runner.completion_registry.register.call_count == page_size + 3

    def test_list_active_agent_runs_paginates_offsets(self) -> None:
        page_size = _RUN_REPLAY_PAGE_SIZE
        runs = [
            SimpleNamespace(id=f"run-{index}", tmux_session_name=None)
            for index in range(page_size + 2)
        ]
        list_active = MagicMock(
            side_effect=lambda *, limit, offset=0: runs[offset : offset + limit]
        )
        runner = self._runner(SimpleNamespace(list_active=list_active))

        active_runs = _list_active_agent_runs_once(runner)

        assert active_runs == runs
        assert list_active.call_args_list == [
            call(limit=page_size, offset=0),
            call(limit=page_size, offset=page_size),
        ]

    @pytest.mark.asyncio
    async def test_replay_daemon_restart_agent_cancellations_paginates_offsets(self) -> None:
        page_size = _RUN_REPLAY_PAGE_SIZE
        runs = [
            SimpleNamespace(
                id=f"run-{index}",
                terminal_reason="daemon_restart",
                continuation_prompt=None,
                tmux_session_name=None,
            )
            for index in range(page_size + 1)
        ]
        list_by_status = MagicMock(
            side_effect=lambda _status, *, limit, offset=0: runs[offset : offset + limit]
        )
        runner = self._runner(SimpleNamespace(list_by_status=list_by_status))

        replayed = await runner_lifecycle._replay_daemon_restart_agent_cancellations(runner)

        assert replayed == page_size + 1
        assert list_by_status.call_args_list == [
            call("cancelled", limit=page_size, offset=0),
            call("cancelled", limit=page_size, offset=page_size),
        ]
        assert runner.completion_registry.notify.await_count == page_size + 1

    @pytest.mark.asyncio
    async def test_reconcile_live_tmux_run_refreshes_pid_and_reader(self) -> None:
        run = SimpleNamespace(
            id="ac314d27-4314-5fe3-a0ab-01645086e137",
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
            patch("gobby.agents.tmux.get_tmux_session_manager", return_value=tmux_manager),
            patch("gobby.agents.tmux.get_tmux_output_reader", return_value=output_reader),
        ):
            reconciled = await runner_lifecycle._reconcile_agent_runs_after_restart(runner)

        # One live tmux-backed run performs three recovery actions: completion
        # registry hydration, runtime PID refresh, and output-reader restart.
        assert reconciled == 3
        assert run.tmux_session_name == "gobby-run-1"
        runner.completion_registry.register.assert_called_once_with(
            "ac314d27-4314-5fe3-a0ab-01645086e137",
            subscribers=["parent-1"],
            continuation_prompt="continue later",
        )
        run_storage.update_runtime.assert_called_once_with(
            "ac314d27-4314-5fe3-a0ab-01645086e137",
            pid=222,
            tmux_session_name="gobby-run-1",
        )
        output_reader.start_reader.assert_awaited_once_with(
            "ac314d27-4314-5fe3-a0ab-01645086e137", "gobby-run-1"
        )
        runner.agent_lifecycle_monitor.cleanup_agent.assert_not_awaited()
        runner.agent_lifecycle_monitor.get_cleanup_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconcile_uses_configured_tmux_socket_for_live_agent(self, tmp_path) -> None:
        config = TmuxConfig(
            socket_name="unused-name",
            socket_path=str(tmp_path / "gobby-test-reconcile-configured.sock"),
        )
        run = SimpleNamespace(
            id="ac314d27-4314-5fe3-a0ab-01645086e137", tmux_session_name="gobby-run-1", pid=111
        )
        run_storage = SimpleNamespace(
            list_active=MagicMock(return_value=[run]),
            update_runtime=MagicMock(),
        )
        runner = self._runner(run_storage)
        configure_tmux(config)
        tmux_manager = get_tmux_session_manager()
        output_reader = get_tmux_output_reader()
        list_sessions = AsyncMock(
            return_value=[
                SimpleNamespace(name="gobby-run-1", pane_pid=111, pane_dead=False),
            ]
        )
        start_reader = AsyncMock(return_value=True)

        with (
            patch.object(tmux_manager, "list_sessions", list_sessions),
            patch.object(output_reader, "start_reader", start_reader),
        ):
            reconciled = await runner_lifecycle._reconcile_agent_runs_after_restart(runner)

        assert tmux_manager.config == config
        list_sessions.assert_awaited_once_with()
        assert reconciled == 2
        runner.completion_registry.register.assert_called_once_with(
            "ac314d27-4314-5fe3-a0ab-01645086e137",
            subscribers=["parent-1"],
            continuation_prompt=None,
        )
        run_storage.update_runtime.assert_not_called()
        start_reader.assert_awaited_once_with("ac314d27-4314-5fe3-a0ab-01645086e137", "gobby-run-1")
        runner.agent_lifecycle_monitor.cleanup_agent.assert_not_awaited()
        runner.agent_lifecycle_monitor.get_cleanup_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconcile_missing_tmux_session_cleans_run(self) -> None:
        run = SimpleNamespace(
            id="ac314d27-4314-5fe3-a0ab-01645086e137", tmux_session_name="gobby-run-1", pid=111
        )
        run_storage = SimpleNamespace(
            list_active=MagicMock(return_value=[run]),
            update_runtime=MagicMock(),
        )
        runner = self._runner(run_storage)
        tmux_manager = SimpleNamespace(list_sessions=AsyncMock(return_value=[]))

        with patch(
            "gobby.agents.tmux.get_tmux_session_manager",
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
        run = SimpleNamespace(
            id="ac314d27-4314-5fe3-a0ab-01645086e137", tmux_session_name="gobby-run-1", pid=111
        )
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
            "gobby.agents.tmux.get_tmux_session_manager",
            return_value=tmux_manager,
        ):
            reconciled = await runner_lifecycle._reconcile_agent_runs_after_restart(runner)

        assert reconciled == 2
        runner.agent_lifecycle_monitor.get_cleanup_agent.assert_called_once_with()
        runner.agent_lifecycle_monitor.cleanup_agent.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reconcile_active_non_tmux_run_only_hydrates_completion(self) -> None:
        run = SimpleNamespace(
            id="ac314d27-4314-5fe3-a0ab-01645086e137",
            tmux_session_name=None,
            continuation_prompt=None,
        )
        run_storage = SimpleNamespace(list_active=MagicMock(return_value=[run]))
        runner = self._runner(run_storage)

        reconciled = await runner_lifecycle._reconcile_agent_runs_after_restart(runner)

        assert reconciled == 1
        runner.completion_registry.register.assert_called_once_with(
            "ac314d27-4314-5fe3-a0ab-01645086e137",
            subscribers=["parent-1"],
            continuation_prompt=None,
        )

    @pytest.mark.asyncio
    async def test_reconcile_active_run_refreshes_expired_dispatch_mutex(
        self, temp_db, sample_project
    ) -> None:
        task = LocalTaskManager(temp_db).create_task(
            sample_project["id"],
            "Restarted agent task",
            category="code",
            validation_criteria="Dispatch mutex is refreshed for recovered active runs.",
            implementation_domain="backend",
        )
        parent = SessionManager(temp_db).register(
            external_id="parent-1",
            machine_id="machine-1",
            source="test",
            project_id=sample_project["id"],
        )
        run_storage = LocalAgentRunManager(temp_db)
        run = run_storage.create(
            parent_session_id=parent.id,
            provider="codex",
            prompt="work",
            run_id="ac314d27-4314-5fe3-a0ab-01645086e137",
            task_id=task.id,
        )
        run = run_storage.start(run.id)
        assert run is not None
        past = datetime.now(UTC) - timedelta(minutes=20)
        mutexes = TaskDispatchMutexManager(temp_db)
        mutexes.acquire_mutex(
            task.id,
            holder="dispatcher",
            kind="heartbeat",
            ttl_seconds=1,
            run_id=run.id,
            now=past,
        )

        reconciled = await runner_lifecycle._reconcile_agent_runs_after_restart(
            self._runner(run_storage, db=temp_db)
        )

        mutex = mutexes.get_mutex(task.id)
        assert reconciled == 2
        assert mutex is not None
        assert mutex.run_id == "ac314d27-4314-5fe3-a0ab-01645086e137"
        assert mutex.lease_holder == "dispatcher"
        assert datetime.fromisoformat(mutex.lease_until) > datetime.now(UTC)

    @pytest.mark.asyncio
    async def test_reconcile_missing_tmux_run_does_not_refresh_expired_mutex(
        self, temp_db, sample_project
    ) -> None:
        task = LocalTaskManager(temp_db).create_task(
            sample_project["id"],
            "Missing tmux task",
            category="code",
            validation_criteria="Missing tmux sessions are terminalized instead of refreshed.",
            implementation_domain="backend",
        )
        parent = SessionManager(temp_db).register(
            external_id="parent-1",
            machine_id="machine-1",
            source="test",
            project_id=sample_project["id"],
        )
        run_storage = LocalAgentRunManager(temp_db)
        run = run_storage.create(
            parent_session_id=parent.id,
            provider="codex",
            prompt="work",
            run_id="ac314d27-4314-5fe3-a0ab-01645086e137",
            task_id=task.id,
        )
        run_storage.start(run.id)
        run_storage.update_runtime(run.id, pid=111, tmux_session_name="gobby-run-1")
        past = datetime.now(UTC) - timedelta(minutes=20)
        mutexes = TaskDispatchMutexManager(temp_db)
        mutexes.acquire_mutex(
            task.id,
            holder="dispatcher",
            kind="heartbeat",
            ttl_seconds=1,
            run_id=run.id,
            now=past,
        )
        tmux_manager = SimpleNamespace(list_sessions=AsyncMock(return_value=[]))

        with patch(
            "gobby.agents.tmux.get_tmux_session_manager",
            return_value=tmux_manager,
        ):
            reconciled = await runner_lifecycle._reconcile_agent_runs_after_restart(
                self._runner(run_storage, db=temp_db)
            )

        mutex = mutexes.get_mutex(task.id)
        assert reconciled == 2
        assert mutex is not None
        assert datetime.fromisoformat(mutex.lease_until) < datetime.now(UTC)
        assert run_storage.get(run.id).tmux_session_name == "gobby-run-1"

    def test_list_active_agent_runs_requires_agent_runner(self) -> None:
        runner = SimpleNamespace(agent_runner=None)

        with pytest.raises(RuntimeError, match="runner.agent_runner is not configured"):
            _list_active_agent_runs_once(runner)

    def _runner(self, run_storage: SimpleNamespace, db: object | None = None) -> SimpleNamespace:
        cleanup_agent = AsyncMock()
        return SimpleNamespace(
            database=db,
            agent_runner=SimpleNamespace(run_storage=run_storage),
            agent_lifecycle_monitor=SimpleNamespace(
                get_cleanup_agent=MagicMock(return_value=cleanup_agent),
                cleanup_agent=cleanup_agent,
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

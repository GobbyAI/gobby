"""Focused tests for agent restart reconciliation."""

from __future__ import annotations

import asyncio
import json
import logging
from copy import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import httpx
import pytest
from fastapi import FastAPI

import gobby.runner_lifecycle as runner_lifecycle
from gobby.agents.tmux import configure_tmux, get_tmux_output_reader, get_tmux_session_manager
from gobby.config.tmux import TmuxConfig
from gobby.hooks.inbox import (
    HookInboxBarrierResult,
    _get_hook_inbox_drain_lock,
    drain_hook_inbox_barrier,
    drain_hook_inbox_once,
)
from gobby.runner import GobbyRunner
from gobby.runner_lifecycle_agents import (
    _RUN_REPLAY_PAGE_SIZE,
    _list_active_agent_runs_once,
    _rehydrate_active_agent_completion_subscribers,
    _run_agent_hook_replay_barrier,
)
from gobby.runner_lifecycle_reconcile import (
    _reclassify_reconciliation_pending_runs,
    _resolve_provisional_daemon_resumes,
)
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.terminals import TerminalManager
from gobby.utils.machine_id import require_machine_id
from tests.agents.terminal_fixtures import make_live_terminal

pytestmark = pytest.mark.unit


class TestAgentRestartReconciliation:
    """Recover preserved tmux-backed agents after daemon startup."""

    @pytest.mark.asyncio
    async def test_restart_reconciles_and_rotates_managed_credentials_without_agent_runner(
        self,
    ) -> None:
        credential_manager = MagicMock()
        credential_manager.reconcile.return_value = 2
        credential_manager.rotate_due.return_value = [object()]
        runner = SimpleNamespace(
            agent_runner=None,
            managed_credential_manager=credential_manager,
        )

        reconciled = await runner_lifecycle._reconcile_agent_runs_after_restart(
            cast(GobbyRunner, runner)
        )

        assert reconciled == 0
        credential_manager.reconcile.assert_called_once_with()
        credential_manager.rotate_due.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_a_failed_credential_reconciliation_logs_what_failed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Startup continues past this, but not silently.

        Without the exception the log line names neither the failure nor which
        of the two calls raised, which is all an operator has to go on.
        """
        credential_manager = MagicMock()
        credential_manager.reconcile.side_effect = RuntimeError("keychain is locked")
        runner = SimpleNamespace(
            agent_runner=None,
            managed_credential_manager=credential_manager,
        )

        with caplog.at_level(logging.ERROR):
            reconciled = await runner_lifecycle._reconcile_agent_runs_after_restart(
                cast(GobbyRunner, runner)
            )

        # Startup carried on rather than failing the daemon.
        assert reconciled == 0
        credential_manager.rotate_due.assert_not_called()
        records = [r for r in caplog.records if "credential startup" in r.getMessage()]
        assert len(records) == 1
        assert records[0].exc_info is not None
        assert "keychain is locked" in caplog.text

    @pytest.mark.asyncio
    async def test_recover_agent_runs_after_restart_paginates_active_runs(self) -> None:
        page_size = _RUN_REPLAY_PAGE_SIZE
        runs = [
            SimpleNamespace(id=f"run-{index}", continuation_prompt=None)
            for index in range(page_size + 3)
        ]
        list_active_for_machine = MagicMock(
            side_effect=lambda _machine_id, *, limit, offset=0: runs[offset : offset + limit]
        )
        runner = self._runner(SimpleNamespace(list_active_for_machine=list_active_for_machine))

        rehydrated = await runner_lifecycle._recover_agent_runs_after_restart(runner)

        assert rehydrated == page_size + 3
        assert list_active_for_machine.call_args_list == [
            call(ANY, limit=page_size, offset=0),
            call(ANY, limit=page_size, offset=page_size),
        ]
        assert runner.completion_registry.register.call_count == page_size + 3

    @pytest.mark.asyncio
    async def test_rehydrate_subscribers_skips_reconciliation_pending_run(self) -> None:
        run = SimpleNamespace(
            id="ac314d27-4314-5fe3-a0ab-01645086e137",
            resume_metadata_json={"reconciliation_pending": True},
        )
        run_storage = SimpleNamespace(list_active_for_machine=MagicMock(return_value=[run]))
        subscriber_manager = SimpleNamespace(get_completion_subscribers=MagicMock())
        runner = self._runner(run_storage, db=object())

        with (
            patch(
                "gobby.runner_lifecycle_agents.LocalAgentRunManager",
                return_value=run_storage,
            ),
            patch(
                "gobby.runner_lifecycle_agents.CompletionSubscriberManager",
                return_value=subscriber_manager,
            ),
        ):
            rehydrated = await _rehydrate_active_agent_completion_subscribers(runner)

        assert rehydrated == 0
        subscriber_manager.get_completion_subscribers.assert_not_called()
        assert subscriber_manager.get_completion_subscribers.call_count == 0
        runner.completion_registry.register.assert_not_called()
        assert runner.completion_registry.register.call_count == 0
        assert run_storage.list_active_for_machine.call_count == 1

    @pytest.mark.asyncio
    async def test_provisional_resolution_skips_reconciliation_pending_run(self) -> None:
        run = SimpleNamespace(
            id="ac314d27-4314-5fe3-a0ab-01645086e137",
            resume_metadata_json={
                "daemon_stop_resume_phase": "prepared",
                "reconciliation_pending": True,
            },
        )
        run_storage = SimpleNamespace(
            list_active_for_machine=MagicMock(return_value=[]),
        )
        runner = self._runner(run_storage, provisional_runs=[run])
        tmux_manager = SimpleNamespace(list_sessions=AsyncMock(return_value=[]))

        with (
            patch(
                "gobby.agents.tmux.get_tmux_session_manager",
                return_value=tmux_manager,
            ),
            patch(
                "gobby.storage.agent_resume.rollback_prepared_daemon_resume",
            ) as rollback,
        ):
            resolved = await _resolve_provisional_daemon_resumes(runner)

        assert resolved == 0
        rollback.assert_not_called()
        assert rollback.call_count == 0
        assert tmux_manager.list_sessions.await_count == 1
        assert runner.agent_runner.run_storage.list_provisional_daemon_resumes.call_count == 1

    def test_list_active_agent_runs_paginates_offsets(self) -> None:
        page_size = _RUN_REPLAY_PAGE_SIZE
        runs = [
            SimpleNamespace(id=f"run-{index}", terminal_id=None) for index in range(page_size + 2)
        ]
        list_active_for_machine = MagicMock(
            side_effect=lambda _machine_id, *, limit, offset=0: runs[offset : offset + limit]
        )
        runner = self._runner(SimpleNamespace(list_active_for_machine=list_active_for_machine))

        active_runs = _list_active_agent_runs_once(runner)

        assert active_runs == runs
        assert list_active_for_machine.call_args_list == [
            call(ANY, limit=page_size, offset=0),
            call(ANY, limit=page_size, offset=page_size),
        ]

    @pytest.mark.asyncio
    async def test_reconcile_live_tmux_run_refreshes_pid_and_reader(self) -> None:
        run = SimpleNamespace(
            id="ac314d27-4314-5fe3-a0ab-01645086e137",
            terminal_id="gobby-run-1",
            pid=111,
            continuation_prompt="continue later",
        )
        run_storage = SimpleNamespace(
            list_active_for_machine=MagicMock(return_value=[run]),
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
        assert run.terminal_id == "gobby-run-1"
        runner.completion_registry.register.assert_called_once_with(
            "ac314d27-4314-5fe3-a0ab-01645086e137",
            subscribers=[],
            continuation_prompt="continue later",
        )
        run_storage.update_runtime.assert_called_once_with(
            "ac314d27-4314-5fe3-a0ab-01645086e137",
            pid=222,
            terminal_id="gobby-run-1",
        )
        output_reader.start_reader.assert_awaited_once_with(
            "ac314d27-4314-5fe3-a0ab-01645086e137", "gobby-run-1"
        )
        runner.agent_lifecycle_monitor.terminalize_cancelled_run.assert_not_awaited()
        runner.agent_lifecycle_monitor.get_cleanup_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconcile_uses_configured_tmux_socket_for_live_agent(
        self, tmp_path: Path
    ) -> None:
        config = TmuxConfig(
            socket_name="unused-name",
            socket_path=str(tmp_path / "gobby-test-reconcile-configured.sock"),
        )
        run = SimpleNamespace(
            id="ac314d27-4314-5fe3-a0ab-01645086e137", terminal_id="gobby-run-1", pid=111
        )
        run_storage = SimpleNamespace(
            list_active_for_machine=MagicMock(return_value=[run]),
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
            subscribers=[],
            continuation_prompt=None,
        )
        run_storage.update_runtime.assert_not_called()
        start_reader.assert_awaited_once_with("ac314d27-4314-5fe3-a0ab-01645086e137", "gobby-run-1")
        runner.agent_lifecycle_monitor.terminalize_cancelled_run.assert_not_awaited()
        runner.agent_lifecycle_monitor.get_cleanup_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconcile_missing_tmux_session_parks_and_resumes_run(self) -> None:
        run = SimpleNamespace(
            id="ac314d27-4314-5fe3-a0ab-01645086e137",
            terminal_id="gobby-run-1",
            pid=111,
            resume_metadata_json={},
            child_session_id="child-1",
        )
        run_storage = SimpleNamespace(
            list_active_for_machine=MagicMock(return_value=[run]),
            update_runtime=MagicMock(),
        )
        runner = self._runner(run_storage, parked_run=run)
        tmux_manager = SimpleNamespace(list_sessions=AsyncMock(return_value=[]))
        resolved_run_ids: set[str] = set()

        with (
            patch(
                "gobby.agents.tmux.get_tmux_session_manager",
                return_value=tmux_manager,
            ),
            patch(
                "gobby.agents.resume_executor.resume_agent_run",
                new=AsyncMock(return_value=SimpleNamespace(success=True, error=None)),
            ) as resume,
        ):
            reconciled = await runner_lifecycle._reconcile_agent_runs_after_restart(
                runner,
                resolved_run_ids=resolved_run_ids,
            )

        assert reconciled == 2
        assert resolved_run_ids == {run.id}
        runner.agent_lifecycle_monitor.terminalize_cancelled_run.assert_awaited_once_with(
            run.id,
            terminal_reason="daemon_stop",
        )
        assert runner.agent_lifecycle_monitor.terminalize_cancelled_run.await_count == 1
        resume.assert_awaited_once()
        assert resume.await_count == 1
        run_storage.update_runtime.assert_not_called()
        assert run_storage.update_runtime.call_count == 0
        assert tmux_manager.list_sessions.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("claimed_by", "claim"),
        [
            ("cooperative_close_handoff_pending", True),
            ("bound_task_is_closed", True),
            ("ended_caller_close_review_outcome", ("fail", "review_status=invalid")),
        ],
    )
    async def test_reconcile_missing_tmux_session_leaves_close_review_caller_running(
        self,
        claimed_by: str,
        claim: object,
    ) -> None:
        run = SimpleNamespace(
            id="ac314d27-4314-5fe3-a0ab-01645086e137",
            terminal_id="gobby-run-1",
            pid=111,
            resume_metadata_json={},
            child_session_id="child-1",
            task_id="task-1",
        )
        db = object()
        run_storage = SimpleNamespace(
            db=db,
            list_active_for_machine=MagicMock(return_value=[run]),
            update_runtime=MagicMock(),
        )
        runner = self._runner(run_storage, parked_run=run)
        tmux_manager = SimpleNamespace(list_sessions=AsyncMock(return_value=[]))
        resolved_run_ids: set[str] = set()
        predicates = {
            "cooperative_close_handoff_pending": MagicMock(return_value=False),
            "bound_task_is_closed": MagicMock(return_value=False),
            "ended_caller_close_review_outcome": MagicMock(return_value=None),
        }
        predicates[claimed_by].return_value = claim

        with (
            patch(
                "gobby.agents.tmux.get_tmux_session_manager",
                return_value=tmux_manager,
            ),
            patch.multiple("gobby.agents.run_completion", **predicates),
            patch(
                "gobby.agents.resume_executor.resume_agent_run",
                new=AsyncMock(return_value=SimpleNamespace(success=True, error=None)),
            ) as resume,
        ):
            reconciled = await runner_lifecycle._reconcile_agent_runs_after_restart(
                runner,
                resolved_run_ids=resolved_run_ids,
            )

        assert reconciled == 2
        assert resolved_run_ids == {run.id}
        predicates[claimed_by].assert_called_once_with(db, run)
        runner.agent_lifecycle_monitor.terminalize_cancelled_run.assert_not_awaited()
        resume.assert_not_awaited()
        run_storage.update_runtime.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconcile_dead_tmux_pane_parks_and_resumes_run(self) -> None:
        run = SimpleNamespace(
            id="ac314d27-4314-5fe3-a0ab-01645086e137",
            terminal_id="gobby-run-1",
            pid=111,
            resume_metadata_json={},
            child_session_id="child-1",
        )
        run_storage = SimpleNamespace(list_active_for_machine=MagicMock(return_value=[run]))
        runner = self._runner(run_storage, parked_run=run)
        tmux_manager = SimpleNamespace(
            list_sessions=AsyncMock(
                return_value=[
                    SimpleNamespace(name="gobby-run-1", pane_pid=222, pane_dead=True),
                ]
            )
        )

        with (
            patch(
                "gobby.agents.tmux.get_tmux_session_manager",
                return_value=tmux_manager,
            ),
            patch(
                "gobby.agents.resume_executor.resume_agent_run",
                new=AsyncMock(return_value=SimpleNamespace(success=True, error=None)),
            ) as resume,
        ):
            reconciled = await runner_lifecycle._reconcile_agent_runs_after_restart(runner)

        assert reconciled == 2
        runner.agent_lifecycle_monitor.terminalize_cancelled_run.assert_awaited_once_with(
            run.id,
            terminal_reason="daemon_stop",
        )
        assert runner.agent_lifecycle_monitor.terminalize_cancelled_run.await_count == 1
        resume.assert_awaited_once()
        assert resume.await_count == 1
        assert tmux_manager.list_sessions.await_count == 1

    @pytest.mark.asyncio
    async def test_reconcile_live_native_run_survives_without_probing_the_host(self) -> None:
        run = SimpleNamespace(
            id="ac314d27-4314-5fe3-a0ab-01645086e137",
            terminal_id="5c0a4b6e-7f1d-4c1e-9d2a-3e4f5a6b7c8d",
            continuation_prompt=None,
        )
        row = SimpleNamespace(id=run.terminal_id, backend="native", state="live")
        run_storage = SimpleNamespace(list_active_for_machine=MagicMock(return_value=[run]))
        runner = self._runner(run_storage)
        runner.terminal_manager = SimpleNamespace(get=MagicMock(return_value=row))
        runner.terminal_runtime_registry = SimpleNamespace(
            resolve=MagicMock(side_effect=AssertionError("host must not be probed"))
        )

        reconciled = await runner_lifecycle._reconcile_agent_runs_after_restart(runner)

        assert reconciled == 1
        runner.agent_lifecycle_monitor.terminalize_cancelled_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reconcile_orphaned_native_run_parks_and_resumes(self) -> None:
        run = SimpleNamespace(
            id="ac314d27-4314-5fe3-a0ab-01645086e137",
            terminal_id="5c0a4b6e-7f1d-4c1e-9d2a-3e4f5a6b7c8d",
            resume_metadata_json={},
            child_session_id="child-1",
        )
        row = SimpleNamespace(id=run.terminal_id, backend="native", state="orphaned")
        run_storage = SimpleNamespace(list_active_for_machine=MagicMock(return_value=[run]))
        runner = self._runner(run_storage, parked_run=run)
        runner.terminal_manager = SimpleNamespace(get=MagicMock(return_value=row))
        resolved_run_ids: set[str] = set()

        with patch(
            "gobby.agents.resume_executor.resume_agent_run",
            new=AsyncMock(return_value=SimpleNamespace(success=True, error=None)),
        ) as resume:
            reconciled = await runner_lifecycle._reconcile_agent_runs_after_restart(
                runner, resolved_run_ids=resolved_run_ids
            )

        assert reconciled == 2
        assert resolved_run_ids == {run.id}
        runner.agent_lifecycle_monitor.terminalize_cancelled_run.assert_awaited_once_with(
            run.id, terminal_reason="daemon_stop"
        )
        resume.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reconcile_active_non_tmux_run_only_hydrates_completion(self) -> None:
        run = SimpleNamespace(
            id="ac314d27-4314-5fe3-a0ab-01645086e137",
            terminal_id=None,
            continuation_prompt=None,
        )
        run_storage = SimpleNamespace(list_active_for_machine=MagicMock(return_value=[run]))
        runner = self._runner(run_storage)

        reconciled = await runner_lifecycle._reconcile_agent_runs_after_restart(runner)

        assert reconciled == 1
        runner.completion_registry.register.assert_called_once_with(
            "ac314d27-4314-5fe3-a0ab-01645086e137",
            subscribers=[],
            continuation_prompt=None,
        )

    @pytest.mark.asyncio
    async def test_reconcile_active_run_refreshes_expired_dispatch_mutex(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
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
            machine_id=require_machine_id(),
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
        started_run = run_storage.start(run.id)
        assert started_run is not None
        run = started_run
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
        assert mutex.lease_until is not None
        assert mutex.lease_until > datetime.now(UTC)

    @pytest.mark.asyncio
    async def test_reconcile_missing_tmux_run_does_not_refresh_expired_mutex(
        self, temp_db: HubDatabase, sample_project: dict[str, Any]
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
            machine_id=require_machine_id(),
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
        run_storage.update_runtime(run.id, pid=111)
        _live_run = run_storage.get(run.id)
        assert _live_run is not None
        make_live_terminal(_live_run, db=run_storage.db, session_name="gobby-run-1")

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
        assert reconciled == 1
        assert mutex is not None
        assert mutex.lease_until is not None
        assert mutex.lease_until < datetime.now(UTC)
        linked_id = _live_run.terminal_id
        assert linked_id is not None
        reloaded = run_storage.get(run.id)
        assert reloaded is not None
        assert reloaded.terminal_id == linked_id
        linked = TerminalManager(temp_db).get(linked_id)
        assert linked is not None
        assert linked.session_name == "gobby-run-1"

    def test_list_active_agent_runs_requires_agent_runner(self) -> None:
        # A runner without an agent_runner is exactly the invalid input under
        # test, so the fake is cast at the boundary rather than typed.
        runner = cast(GobbyRunner, SimpleNamespace(agent_runner=None))

        with pytest.raises(RuntimeError, match="runner.agent_runner is not configured"):
            _list_active_agent_runs_once(runner)

    @pytest.mark.asyncio
    async def test_periodic_recovery_excludes_unrelated_provisional_resume(self) -> None:
        fenced = SimpleNamespace(id="fenced-resume", resume_metadata_json={})
        unrelated = SimpleNamespace(id="unrelated-resume", resume_metadata_json={})
        metadata: dict[str, dict[str, bool]] = {}
        storage = SimpleNamespace(
            list_reconciliation_pending=lambda **kwargs: [fenced],
            list_active_for_machine=lambda *args, **kwargs: [],
            list_provisional_daemon_resumes=lambda **kwargs: [fenced, unrelated],
            merge_resume_metadata=lambda run_id, values: metadata.update({run_id: values}),
        )
        runner = self._runner(storage)
        runtime = SimpleNamespace(list_sessions=AsyncMock(return_value=[]))
        with (
            patch(
                "gobby.runner_lifecycle_reconcile._run_agent_hook_replay_barrier", return_value=True
            ),
            patch("gobby.runner_lifecycle_reconcile._tmux_runtime", return_value=runtime),
            patch(
                "gobby.runner_lifecycle_reconcile._resolve_provisional_daemon_resume_row",
                return_value=True,
            ) as resolve,
        ):
            count = await _reclassify_reconciliation_pending_runs(runner)

        assert count == 1
        resolve.assert_awaited_once_with(runner, fenced, {})
        assert metadata == {fenced.id: {"reconciliation_pending": False}}

    @pytest.mark.asyncio
    async def test_periodic_recovery_clears_fence_with_spawn_held_mutex(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, Any],
    ) -> None:
        task = LocalTaskManager(temp_db).create_task(
            sample_project["id"],
            "Spawn-held restart task",
            category="code",
            validation_criteria="Restart recovery preserves the attached mutex holder.",
            implementation_domain="backend",
        )
        parent = SessionManager(temp_db).register(
            external_id="parent-spawn-held-restart",
            machine_id=require_machine_id(),
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
        started_run = run_storage.start(run.id)
        assert started_run is not None
        run_storage.merge_resume_metadata(run.id, {"reconciliation_pending": True})
        mutexes = TaskDispatchMutexManager(temp_db)
        spawn_holder = "spawn-agent:0a5c9b1ed2f34cd6:11111111-2222-3333-4444-555555555555"
        assert mutexes.acquire_mutex(
            task.id,
            holder=spawn_holder,
            kind="spawn_agent",
            ttl_seconds=600,
            run_id=run.id,
        )

        runner = self._runner(run_storage, db=temp_db)
        with patch(
            "gobby.runner_lifecycle_reconcile._run_agent_hook_replay_barrier",
            new=AsyncMock(return_value=True),
        ):
            reclassified = await _reclassify_reconciliation_pending_runs(runner)

        recovered = run_storage.get(run.id)
        mutex = mutexes.get_mutex(task.id)
        assert reclassified == 2
        assert recovered is not None
        metadata = recovered.resume_metadata_json
        assert metadata is not None
        assert metadata["reconciliation_pending"] is False
        assert mutex is not None
        assert mutex.run_id == run.id
        assert mutex.lease_holder == spawn_holder

    @pytest.mark.asyncio
    async def test_periodic_recovery_keeps_fence_for_other_run_mutex(
        self,
        temp_db: HubDatabase,
        sample_project: dict[str, Any],
    ) -> None:
        task = LocalTaskManager(temp_db).create_task(
            sample_project["id"],
            "Conflicting restart task",
            category="code",
            validation_criteria="Restart recovery never takes another run's mutex.",
            implementation_domain="backend",
        )
        parent = SessionManager(temp_db).register(
            external_id="parent-conflicting-restart",
            machine_id=require_machine_id(),
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
        started_run = run_storage.start(run.id)
        assert started_run is not None
        run_storage.merge_resume_metadata(run.id, {"reconciliation_pending": True})
        mutexes = TaskDispatchMutexManager(temp_db)
        other_run_id = "bd425e38-5425-4fe4-b1bc-12756197f248"
        other_holder = "spawn-agent:other-run"
        past = datetime.now(UTC) - timedelta(minutes=20)
        assert mutexes.acquire_mutex(
            task.id,
            holder=other_holder,
            kind="spawn_agent",
            ttl_seconds=1,
            run_id=other_run_id,
            now=past,
        )

        runner = self._runner(run_storage, db=temp_db)
        with patch(
            "gobby.runner_lifecycle_reconcile._run_agent_hook_replay_barrier",
            new=AsyncMock(return_value=True),
        ):
            reclassified = await _reclassify_reconciliation_pending_runs(runner)

        recovered = run_storage.get(run.id)
        mutex = mutexes.get_mutex(task.id)
        assert reclassified == 1
        assert recovered is not None
        metadata = recovered.resume_metadata_json
        assert metadata is not None
        assert metadata["reconciliation_pending"] is True
        assert mutex is not None
        assert mutex.run_id == other_run_id
        assert mutex.lease_holder == other_holder

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fresh_pane_dead", [False, True])
    async def test_periodic_recovery_only_reconciles_captured_fenced_runs(
        self, fresh_pane_dead: bool
    ) -> None:
        fenced = SimpleNamespace(
            id="fenced-run",
            terminal_id="fenced-pane",
            pid=111,
            task_id=None,
            child_session_id="fenced-child",
            resume_metadata_json={"reconciliation_pending": True, "parent_session_id": "parent"},
        )
        fresh = SimpleNamespace(
            id="fresh-run",
            terminal_id="fresh-pane",
            pid=333,
            task_id=None,
            child_session_id="fresh-child",
            resume_metadata_json={"parent_session_id": "parent"},
        )
        active = [fenced]
        runtime_updates: dict[str, dict[str, Any]] = {}
        metadata_updates: dict[str, dict[str, bool]] = {}
        storage = SimpleNamespace(
            list_reconciliation_pending=lambda **kwargs: [fenced],
            list_active_for_machine=lambda *args, **kwargs: list(active),
            update_runtime=lambda run_id, **values: runtime_updates.update({run_id: values}),
            merge_resume_metadata=lambda run_id, values: metadata_updates.update({run_id: values}),
        )
        runner = self._runner(storage)

        async def settle(*args: Any, **kwargs: Any) -> bool:
            # This run is admitted while the periodic pass awaits inbox replay.
            active.append(fresh)
            return True

        runtime = SimpleNamespace(
            list_sessions=AsyncMock(
                return_value=[
                    SimpleNamespace(name="fenced-pane", pane_pid=222, pane_dead=False),
                    SimpleNamespace(name="fresh-pane", pane_pid=444, pane_dead=fresh_pane_dead),
                ]
            )
        )
        reader = SimpleNamespace(start_reader=AsyncMock(return_value=True))
        with (
            patch(
                "gobby.runner_lifecycle_reconcile._run_agent_hook_replay_barrier",
                side_effect=settle,
            ),
            patch("gobby.runner_lifecycle_reconcile._tmux_runtime", return_value=runtime),
            patch("gobby.agents.tmux.get_tmux_output_reader", return_value=reader),
            patch("gobby.agents.resume_finalization.notify_parent_of_recovery") as notify,
            patch(
                "gobby.runner_lifecycle_reconcile._cleanup_missing_terminal_agent_run",
                new_callable=AsyncMock,
            ) as cleanup,
        ):
            await _reclassify_reconciliation_pending_runs(runner)

        runner.completion_registry.register.assert_called_once_with(
            fenced.id,
            subscribers=[],
            continuation_prompt=None,
        )
        assert runtime_updates == {fenced.id: {"pid": 222, "terminal_id": "fenced-pane"}}
        reader.start_reader.assert_awaited_once_with(fenced.id, "fenced-pane")
        notify.assert_called_once()
        assert notify.call_args.kwargs["run_id"] == fenced.id
        cleanup.assert_not_awaited()
        assert metadata_updates == {fenced.id: {"reconciliation_pending": False}}

    def _runner(
        self,
        run_storage: Any,
        db: object | None = None,
        *,
        parked_run: Any | None = None,
        provisional_runs: list[Any] | None = None,
    ) -> Any:
        storage = copy(run_storage) if isinstance(run_storage, SimpleNamespace) else run_storage
        if not hasattr(storage, "list_provisional_daemon_resumes"):
            storage.list_provisional_daemon_resumes = MagicMock(return_value=provisional_runs or [])
        if not hasattr(storage, "get"):
            storage.get = MagicMock(return_value=parked_run)
        terminalize_cancelled_run = AsyncMock(return_value=True)
        return SimpleNamespace(
            database=db,
            config_runtime=SimpleNamespace(
                capture=lambda: SimpleNamespace(snapshot=SimpleNamespace(active=SimpleNamespace()))
            ),
            session_manager=MagicMock(),
            agent_runner=SimpleNamespace(
                child_session_manager=MagicMock(),
                run_storage=storage,
            ),
            agent_lifecycle_monitor=SimpleNamespace(
                terminalize_cancelled_run=terminalize_cancelled_run,
                get_cleanup_agent=MagicMock(),
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


class TestReclassifyReconciliationPendingRuns:
    """Fenced-run reclassification waits for the hook replay barrier to settle."""

    _RUN_ID = "ac314d27-4314-5fe3-a0ab-01645086e137"
    _OTHER_RUN_ID = "bd425e38-5425-4fe4-b1bc-12756197f248"

    def _runner(self, run_storage: Any) -> Any:
        return SimpleNamespace(
            agent_runner=SimpleNamespace(run_storage=run_storage),
            http_server=SimpleNamespace(app=object()),
            session_manager=None,
        )

    @pytest.mark.asyncio
    async def test_empty_pending_list_skips_replay_barrier(self) -> None:
        run_storage = SimpleNamespace(
            list_reconciliation_pending=MagicMock(return_value=[]),
            merge_resume_metadata=MagicMock(),
        )
        runner = self._runner(run_storage)
        barrier = AsyncMock(return_value=True)

        with patch(
            "gobby.runner_lifecycle_reconcile._run_agent_hook_replay_barrier",
            new=barrier,
        ):
            reclassified = await _reclassify_reconciliation_pending_runs(runner)

        assert reclassified == 0
        assert barrier.await_count == 0
        run_storage.merge_resume_metadata.assert_not_called()
        run_storage.list_reconciliation_pending.assert_called_once_with(
            machine_id=ANY,
            limit=_RUN_REPLAY_PAGE_SIZE,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent_services_available", [True, False])
    async def test_barrier_timeout_without_identities_is_non_blocking(
        self,
        agent_services_available: bool,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        run_storage = SimpleNamespace(
            get=MagicMock(),
            merge_resume_metadata=MagicMock(),
        )
        runner = self._runner(run_storage)
        if not agent_services_available:
            runner.agent_runner = None
        barrier_result = HookInboxBarrierResult(
            replayed=3,
            timed_out=True,
            unresolved_run_ids=(),
            unresolved_session_ids=(),
        )

        with (
            patch(
                "gobby.hooks.inbox.drain_hook_inbox_barrier",
                new=AsyncMock(return_value=barrier_result),
            ),
            caplog.at_level(logging.INFO, logger="gobby.runner_lifecycle"),
        ):
            settled = await _run_agent_hook_replay_barrier(runner)

        assert settled is True
        run_storage.get.assert_not_called()
        run_storage.merge_resume_metadata.assert_not_called()
        assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
        assert any(
            record.levelno == logging.INFO
            and "replaying 3 envelope(s)" in record.getMessage()
            and "0 session identity/identities" in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_barrier_timeout_session_without_agent_run_is_non_blocking(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        session_id = "019fac30-b716-73c2-934e-7d4fb8aa42d0"
        run_storage = SimpleNamespace(
            get=MagicMock(),
            merge_resume_metadata=MagicMock(),
        )
        session_manager = SimpleNamespace(
            get=MagicMock(return_value=SimpleNamespace(agent_run_id=None))
        )
        runner = self._runner(run_storage)
        runner.session_manager = session_manager
        barrier_result = HookInboxBarrierResult(
            replayed=2,
            timed_out=True,
            unresolved_run_ids=(),
            unresolved_session_ids=(session_id,),
        )

        with (
            patch(
                "gobby.hooks.inbox.drain_hook_inbox_barrier",
                new=AsyncMock(return_value=barrier_result),
            ),
            caplog.at_level(logging.INFO, logger="gobby.runner_lifecycle"),
        ):
            settled = await _run_agent_hook_replay_barrier(runner)

        assert settled is True
        session_manager.get.assert_called_once_with(session_id)
        run_storage.get.assert_not_called()
        run_storage.merge_resume_metadata.assert_not_called()
        assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
        assert any(
            "replaying 2 envelope(s)" in record.getMessage()
            and "1 session identity/identities" in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("missing_service", ["agent", "session"])
    async def test_barrier_timeout_with_identities_requires_resolution_services(
        self,
        missing_service: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        run_storage = SimpleNamespace(
            get=MagicMock(),
            merge_resume_metadata=MagicMock(),
        )
        runner = self._runner(run_storage)
        unresolved_run_ids: tuple[str, ...] = ()
        unresolved_session_ids: tuple[str, ...] = ()
        if missing_service == "agent":
            runner.agent_runner = None
            unresolved_run_ids = (self._RUN_ID,)
        else:
            unresolved_session_ids = ("019fac30-b716-73c2-934e-7d4fb8aa42d0",)
        barrier_result = HookInboxBarrierResult(
            replayed=0,
            timed_out=True,
            unresolved_run_ids=unresolved_run_ids,
            unresolved_session_ids=unresolved_session_ids,
        )

        with (
            patch(
                "gobby.hooks.inbox.drain_hook_inbox_barrier",
                new=AsyncMock(return_value=barrier_result),
            ),
            caplog.at_level(logging.WARNING, logger="gobby.runner_lifecycle"),
        ):
            settled = await _run_agent_hook_replay_barrier(runner)

        assert settled is False
        run_storage.get.assert_not_called()
        run_storage.merge_resume_metadata.assert_not_called()
        assert any(
            record.levelno == logging.WARNING
            and f"{missing_service} services were unavailable" in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stored_run", "settled_counts"),
        [
            pytest.param(
                SimpleNamespace(status="success"),
                "1 terminal and 0 missing",
                id="terminal-only",
            ),
            pytest.param(None, "0 terminal and 1 missing", id="missing-only"),
        ],
    )
    async def test_barrier_timeout_settles_terminal_or_missing_run_references(
        self,
        stored_run: Any,
        settled_counts: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        run_storage = SimpleNamespace(
            get=MagicMock(return_value=stored_run),
            merge_resume_metadata=MagicMock(),
        )
        runner = self._runner(run_storage)
        barrier_result = HookInboxBarrierResult(
            replayed=0,
            timed_out=True,
            unresolved_run_ids=(self._RUN_ID,),
            unresolved_session_ids=(),
        )

        with (
            patch(
                "gobby.hooks.inbox.drain_hook_inbox_barrier",
                new=AsyncMock(return_value=barrier_result),
            ),
            caplog.at_level(logging.INFO, logger="gobby.runner_lifecycle"),
        ):
            settled = await _run_agent_hook_replay_barrier(runner)

        assert settled is True
        run_storage.get.assert_called_once_with(self._RUN_ID)
        run_storage.merge_resume_metadata.assert_not_called()
        assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
        assert any(
            record.levelno == logging.INFO and settled_counts in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("active_status", ["pending", "running"])
    async def test_barrier_timeout_fences_active_runs_without_cancelling(
        self,
        active_status: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        active_run = SimpleNamespace(id=self._RUN_ID, status=active_status)
        cancelled_run = SimpleNamespace(id=self._OTHER_RUN_ID, status="cancelled")
        runs = {run.id: run for run in (active_run, cancelled_run)}
        run_storage = SimpleNamespace(
            get=MagicMock(side_effect=lambda run_id: runs.get(run_id)),
            merge_resume_metadata=MagicMock(),
            cancel=MagicMock(),
        )
        runner = self._runner(run_storage)
        barrier_result = HookInboxBarrierResult(
            replayed=0,
            timed_out=True,
            unresolved_run_ids=(self._RUN_ID, self._OTHER_RUN_ID),
            unresolved_session_ids=(),
        )
        drain = AsyncMock(return_value=barrier_result)

        with (
            patch("gobby.hooks.inbox.drain_hook_inbox_barrier", new=drain),
            caplog.at_level(logging.INFO, logger="gobby.runner_lifecycle"),
        ):
            settled = await _run_agent_hook_replay_barrier(runner, timeout_seconds=1.0)

        assert settled is False
        drain.assert_awaited_once()
        assert drain.await_args is not None
        assert drain.await_args.kwargs == {
            "timeout_seconds": 1.0,
            "restart_horizon_ms": None,
        }
        run_storage.merge_resume_metadata.assert_called_once_with(
            self._RUN_ID, {"reconciliation_pending": True}
        )
        run_storage.cancel.assert_not_called()
        assert any(
            record.levelno == logging.WARNING
            and "1 active fenced run(s)" in record.getMessage()
            and "0 unclassified run lookup(s)" in record.getMessage()
            and self._RUN_ID in record.getMessage()
            for record in caplog.records
        )
        assert any(
            record.levelno == logging.INFO
            and "1 terminal and 0 missing run reference(s)" in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_barrier_passes_http_bound_horizon(self) -> None:
        run_storage = SimpleNamespace(
            get=MagicMock(),
            merge_resume_metadata=MagicMock(),
        )
        runner = self._runner(run_storage)
        runner.http_bound_at_ms = 1_700_000_000_000
        drain = AsyncMock(
            return_value=HookInboxBarrierResult(
                replayed=0,
                timed_out=False,
                unresolved_run_ids=(),
                unresolved_session_ids=(),
            )
        )

        with patch("gobby.hooks.inbox.drain_hook_inbox_barrier", new=drain):
            settled = await _run_agent_hook_replay_barrier(runner)

        assert settled is True
        assert drain.await_args is not None
        assert drain.await_args.kwargs == {
            "timeout_seconds": 5.0,
            "restart_horizon_ms": 1_700_000_000_000,
        }

    @pytest.mark.asyncio
    async def test_progressing_replay_backlog_settles_without_fencing_reconnected_run(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A downtime backlog that outlasts the budget but keeps replaying does not fence.

        Mirrors the 14:54 restart: 17 residue hooks replayed at about 0.5 s each
        against the default 5 s budget. The loop clock is virtual and each replay
        is released by the test, so no wall-clock time decides the outcome.
        """
        horizon_ms = 1_700_000_000_000
        replay_seconds = 0.5
        inbox_dir = tmp_path / "hooks" / "inbox"
        inbox_dir.mkdir(parents=True)
        envelope = {
            "schema_version": 1,
            "enqueued_at": "2026-04-16T12:00:00Z",
            "critical": False,
            "response_capability": "hook-response.v1",
            "hook_type": "post-tool-use",
            "input_data": {"terminal_context": {"gobby_agent_run_id": self._RUN_ID}},
            "source": "claude",
            "headers": {},
        }
        residue = [
            inbox_dir / f"n-{horizon_ms - 20_000 + offset}-residue{offset:02d}.json"
            for offset in range(17)
        ]
        # The reconnected run's post-bind hook is live traffic, not restart residue.
        live = inbox_dir / f"n-{horizon_ms + 1_000}-live.json"
        for path in (*residue, live):
            path.write_text(json.dumps(envelope), encoding="utf-8")
        run_storage = SimpleNamespace(
            get=MagicMock(return_value=SimpleNamespace(id=self._RUN_ID, status="running")),
            merge_resume_metadata=MagicMock(),
        )
        runner = self._runner(run_storage)
        app = FastAPI()
        runner.http_server.app = app
        runner.http_bound_at_ms = horizon_ms

        loop = asyncio.get_running_loop()
        real_time = loop.time
        elapsed = 0.0

        def virtual_time() -> float:
            return real_time() + elapsed

        started: asyncio.Queue[asyncio.Event] = asyncio.Queue()

        async def replay(*args: object, **kwargs: object) -> httpx.Response:
            release = asyncio.Event()
            started.put_nowait(release)
            await release.wait()
            return httpx.Response(200)

        post = AsyncMock(side_effect=replay)
        with (
            patch.object(loop, "time", virtual_time),
            patch("gobby.hooks.inbox.get_hook_inbox_dir", return_value=inbox_dir),
            patch("gobby.hooks.inbox.read_local_api_token", return_value="test-token"),
            patch("gobby.hooks.inbox._post_envelope", new=post),
            caplog.at_level(logging.INFO, logger="gobby.runner_lifecycle"),
        ):
            barrier = asyncio.create_task(_run_agent_hook_replay_barrier(runner))
            # Each replay fits the budget; the backlog takes 8.5 s of loop time.
            for _ in residue:
                release = await started.get()
                elapsed += replay_seconds
                release.set()
            settled = await barrier
            async with _get_hook_inbox_drain_lock(app):
                pass  # a replay outliving its barrier finishes before patches unwind

        assert settled is True
        run_storage.merge_resume_metadata.assert_not_called()
        assert [
            record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING
        ] == []
        assert post.await_count == len(residue)
        assert [path for path in residue if path.exists()] == []
        assert live.exists()

    @pytest.mark.asyncio
    async def test_reclassify_does_not_warn_while_replay_progresses(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A lifecycle pass queued behind a live replay settles instead of re-fencing.

        Mirrors the 0.5.0 cutover: residue fell 63 -> 7 over three minutes, yet
        every reclassify pass waited a fixed 5 s on the drain lock the periodic
        drain held and re-fenced the same runs with a warning. The holder here
        settles one residue hook per loop second for 8 s on a virtual clock.
        """
        horizon_ms = 1_700_000_000_000
        inbox_dir = tmp_path / "hooks" / "inbox"
        inbox_dir.mkdir(parents=True)
        envelope = {
            "schema_version": 1,
            "enqueued_at": "2026-04-16T12:00:00Z",
            "critical": False,
            "response_capability": "hook-response.v1",
            "hook_type": "post-tool-use",
            "input_data": {"terminal_context": {"gobby_agent_run_id": self._RUN_ID}},
            "source": "claude",
            "headers": {},
        }
        residue = [
            inbox_dir / f"n-{horizon_ms - 20_000 + offset}-residue{offset}.json"
            for offset in range(8)
        ]
        for path in residue:
            path.write_text(json.dumps(envelope), encoding="utf-8")
        run_storage = SimpleNamespace(
            list_reconciliation_pending=MagicMock(return_value=[SimpleNamespace(id=self._RUN_ID)]),
            get=MagicMock(return_value=SimpleNamespace(id=self._RUN_ID, status="running")),
            merge_resume_metadata=MagicMock(),
        )
        runner = self._runner(run_storage)
        app = FastAPI()
        runner.http_server.app = app
        runner.http_bound_at_ms = horizon_ms

        async def reconcile(
            target: Any,
            *,
            include_fenced: bool,
            resolved_run_ids: set[str],
            run_ids: frozenset[str],
        ) -> int:
            resolved_run_ids.update(run_ids)
            return len(run_ids)

        barrier_entered = asyncio.Event()

        async def observed_barrier(*args: Any, **kwargs: Any) -> HookInboxBarrierResult:
            barrier_entered.set()
            return await drain_hook_inbox_barrier(*args, **kwargs)

        loop = asyncio.get_running_loop()
        real_time = loop.time
        elapsed = 0.0

        def virtual_time() -> float:
            return real_time() + elapsed

        started: asyncio.Queue[asyncio.Event] = asyncio.Queue()

        async def replay(*args: object, **kwargs: object) -> httpx.Response:
            release = asyncio.Event()
            started.put_nowait(release)
            await release.wait()
            return httpx.Response(200)

        post = AsyncMock(side_effect=replay)
        with (
            patch.object(loop, "time", virtual_time),
            patch("gobby.hooks.inbox.get_hook_inbox_dir", return_value=inbox_dir),
            patch("gobby.hooks.inbox.read_local_api_token", return_value="test-token"),
            patch("gobby.hooks.inbox._post_envelope", new=post),
            patch("gobby.hooks.inbox.drain_hook_inbox_barrier", new=observed_barrier),
            patch(
                "gobby.runner_lifecycle_reconcile._reconcile_agent_runs_after_restart",
                new=AsyncMock(side_effect=reconcile),
            ),
            caplog.at_level(logging.INFO, logger="gobby.runner_lifecycle"),
        ):
            # The periodic drain's first pass owns the lock, as at startup.
            holder = asyncio.create_task(drain_hook_inbox_once(app, inbox_dir, include_fresh=True))
            release = await started.get()
            reclassify = asyncio.create_task(_reclassify_reconciliation_pending_runs(runner))
            await barrier_entered.wait()
            for _ in residue[1:]:
                elapsed += 1.0
                release.set()
                release = await started.get()
            elapsed += 1.0
            release.set()
            assert await holder == len(residue)
            reclassified = await reclassify

        assert reclassified == 1
        assert [
            record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING
        ] == []
        assert run_storage.merge_resume_metadata.call_args_list == [
            call(self._RUN_ID, {"reconciliation_pending": False})
        ]
        assert post.await_count == len(residue)
        assert [path for path in residue if path.exists()] == []

    @pytest.mark.asyncio
    async def test_barrier_timeout_keeps_lookup_failures_unclassified(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        run_storage = SimpleNamespace(
            get=MagicMock(side_effect=RuntimeError("storage unavailable")),
            merge_resume_metadata=MagicMock(),
        )
        runner = self._runner(run_storage)
        barrier_result = HookInboxBarrierResult(
            replayed=0,
            timed_out=True,
            unresolved_run_ids=(self._RUN_ID,),
            unresolved_session_ids=(),
        )

        with (
            patch(
                "gobby.hooks.inbox.drain_hook_inbox_barrier",
                new=AsyncMock(return_value=barrier_result),
            ),
            caplog.at_level(logging.WARNING, logger="gobby.runner_lifecycle"),
        ):
            settled = await _run_agent_hook_replay_barrier(runner)

        assert settled is False
        run_storage.merge_resume_metadata.assert_not_called()
        assert any(
            record.levelno == logging.WARNING
            and f"Failed to load unresolved agent run {self._RUN_ID}" in record.getMessage()
            for record in caplog.records
        )
        assert any(
            record.levelno == logging.WARNING
            and "0 active fenced run(s)" in record.getMessage()
            and "1 unclassified run lookup(s)" in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_settled_barrier_reconciles_then_clears_fences(self) -> None:
        order: list[str] = []
        pending = [
            SimpleNamespace(id=self._RUN_ID),
            SimpleNamespace(id=self._OTHER_RUN_ID),
        ]
        run_storage = SimpleNamespace(
            list_reconciliation_pending=MagicMock(return_value=pending),
            merge_resume_metadata=MagicMock(
                side_effect=lambda run_id, metadata: order.append(f"clear:{run_id}")
            ),
        )
        runner = self._runner(run_storage)

        async def reconcile(
            target: Any,
            *,
            include_fenced: bool,
            resolved_run_ids: set[str],
            run_ids: frozenset[str],
        ) -> int:
            assert run_ids == frozenset({self._RUN_ID, self._OTHER_RUN_ID})
            order.append(f"reconcile:include_fenced={include_fenced}")
            resolved_run_ids.update({self._RUN_ID, self._OTHER_RUN_ID})
            return 2

        barrier = AsyncMock(return_value=True)
        reconcile_mock = AsyncMock(side_effect=reconcile)

        with (
            patch(
                "gobby.runner_lifecycle_reconcile._run_agent_hook_replay_barrier",
                new=barrier,
            ),
            patch(
                "gobby.runner_lifecycle_reconcile._reconcile_agent_runs_after_restart",
                new=reconcile_mock,
            ),
        ):
            reclassified = await _reclassify_reconciliation_pending_runs(runner)

        assert reclassified == 2
        barrier.assert_awaited_once()
        reconcile_mock.assert_awaited_once_with(
            runner,
            include_fenced=True,
            resolved_run_ids={self._RUN_ID, self._OTHER_RUN_ID},
            run_ids=frozenset({self._RUN_ID, self._OTHER_RUN_ID}),
        )
        assert order == [
            "reconcile:include_fenced=True",
            f"clear:{self._RUN_ID}",
            f"clear:{self._OTHER_RUN_ID}",
        ]
        assert run_storage.merge_resume_metadata.call_args_list == [
            call(self._RUN_ID, {"reconciliation_pending": False}),
            call(self._OTHER_RUN_ID, {"reconciliation_pending": False}),
        ]

    @pytest.mark.asyncio
    async def test_settled_barrier_keeps_unresolved_runs_fenced(self) -> None:
        pending = [
            SimpleNamespace(id=self._RUN_ID),
            SimpleNamespace(id=self._OTHER_RUN_ID),
        ]
        run_storage = SimpleNamespace(
            list_reconciliation_pending=MagicMock(return_value=pending),
            merge_resume_metadata=MagicMock(),
        )
        runner = self._runner(run_storage)

        async def reconcile(
            target: Any,
            *,
            include_fenced: bool,
            resolved_run_ids: set[str],
            run_ids: frozenset[str],
        ) -> int:
            assert target is runner
            assert include_fenced is True
            assert run_ids == frozenset({self._RUN_ID, self._OTHER_RUN_ID})
            resolved_run_ids.add(self._RUN_ID)
            return 1

        with (
            patch(
                "gobby.runner_lifecycle_reconcile._run_agent_hook_replay_barrier",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "gobby.runner_lifecycle_reconcile._reconcile_agent_runs_after_restart",
                new=AsyncMock(side_effect=reconcile),
            ),
        ):
            reclassified = await _reclassify_reconciliation_pending_runs(runner)

        assert reclassified == 1
        run_storage.merge_resume_metadata.assert_called_once_with(
            self._RUN_ID,
            {"reconciliation_pending": False},
        )

    @pytest.mark.asyncio
    async def test_unsettled_barrier_leaves_fences_and_skips_reconcile(self) -> None:
        pending = [SimpleNamespace(id=self._RUN_ID)]
        run_storage = SimpleNamespace(
            list_reconciliation_pending=MagicMock(return_value=pending),
            merge_resume_metadata=MagicMock(),
        )
        runner = self._runner(run_storage)
        barrier = AsyncMock(return_value=False)
        reconcile_mock = AsyncMock()

        with (
            patch(
                "gobby.runner_lifecycle_reconcile._run_agent_hook_replay_barrier",
                new=barrier,
            ),
            patch(
                "gobby.runner_lifecycle_reconcile._reconcile_agent_runs_after_restart",
                new=reconcile_mock,
            ),
        ):
            reclassified = await _reclassify_reconciliation_pending_runs(runner)

        assert reclassified == 0
        barrier.assert_awaited_once()
        assert reconcile_mock.await_count == 0
        run_storage.merge_resume_metadata.assert_not_called()

"""Red tests for project-wide build stop/resume."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.storage.agents import AgentRun
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


def test_stop_pauses_project_automation_without_cron_row(temp_db) -> None:
    from gobby.build.project_state import is_project_automation_enabled
    from gobby.build.service import build_stop
    from gobby.storage.cron import CronJobStorage

    result = build_stop(db=temp_db, project_id="0e27d5b7-167e-5a64-8bd9-6b980bd88f06")

    assert result.enabled is False
    assert is_project_automation_enabled(temp_db, "0e27d5b7-167e-5a64-8bd9-6b980bd88f06") is False
    assert CronJobStorage(temp_db).get_job_by_name("gobby:dispatcher") is None


def test_resume_enables_project_automation_without_cron_row(temp_db) -> None:
    from gobby.build.project_state import is_project_automation_enabled
    from gobby.build.service import build_resume, build_stop
    from gobby.storage.cron import CronJobStorage

    build_stop(db=temp_db, project_id="0e27d5b7-167e-5a64-8bd9-6b980bd88f06")

    result = build_resume(db=temp_db, project_id="0e27d5b7-167e-5a64-8bd9-6b980bd88f06")

    assert result.enabled is True
    assert is_project_automation_enabled(temp_db, "0e27d5b7-167e-5a64-8bd9-6b980bd88f06") is True
    assert CronJobStorage(temp_db).get_job_by_name("gobby:dispatcher") is None


def test_stop_creates_project_row_for_control_event(
    temp_db,
) -> None:
    from gobby.build.service import build_stop

    assert (
        temp_db.fetchone(
            "SELECT id FROM projects WHERE id = %s", ("0e27d5b7-167e-5a64-8bd9-6b980bd88f06",)
        )
        is None
    )

    build_stop(db=temp_db, project_id="0e27d5b7-167e-5a64-8bd9-6b980bd88f06")

    assert (
        temp_db.fetchone(
            "SELECT id FROM projects WHERE id = %s", ("0e27d5b7-167e-5a64-8bd9-6b980bd88f06",)
        )
        is not None
    )


def test_lifecycle_event_appended(temp_db) -> None:
    from gobby.build.service import build_stop

    result = build_stop(db=temp_db, project_id="0e27d5b7-167e-5a64-8bd9-6b980bd88f06")

    assert result.lifecycle_event.reason == "gobby build stop"
    row = temp_db.fetchone(
        "SELECT reason FROM project_lifecycle_events WHERE project_id = %s",
        ("0e27d5b7-167e-5a64-8bd9-6b980bd88f06",),
    )
    assert row["reason"] == "gobby build stop"


def test_lifecycle_event_id_comes_from_returning_row() -> None:
    from gobby.build.project_controls import _record_project_build_event

    class ReturningCursor:
        lastrowid = None

        def fetchone(self) -> dict[str, Any]:
            return {"id": 42, "created_at": datetime.now(UTC)}

    class RecordingConnection:
        sql = ""
        params: tuple[Any, ...] = ()

        def execute(self, sql: str, params: tuple[Any, ...]) -> ReturningCursor:
            self.sql = sql
            self.params = params
            return ReturningCursor()

    class ReturningDb:
        conn = RecordingConnection()

        @contextmanager
        def transaction(self) -> Iterator[RecordingConnection]:
            yield self.conn

    db = ReturningDb()

    result = _record_project_build_event(
        db,
        project_id="0e27d5b7-167e-5a64-8bd9-6b980bd88f06",
        event="build_resume",
        reason="gobby build resume",
        by_actor="build",
    )

    assert result.id == 42
    assert "RETURNING id, created_at" in db.conn.sql
    assert db.conn.params[:4] == (
        "0e27d5b7-167e-5a64-8bd9-6b980bd88f06",
        "build_resume",
        "gobby build resume",
        "build",
    )


def test_lifecycle_event_appended_on_hub_database(hub_db) -> None:
    from gobby.build.service import build_resume
    from gobby.storage.projects import LocalProjectManager

    project = LocalProjectManager(hub_db).create(
        name="build-controls-hub",
        repo_path="/tmp/build-controls-hub",
    )

    result = build_resume(db=hub_db, project_id=project.id)

    assert result.lifecycle_event.id > 0
    row = hub_db.fetchone(
        "SELECT reason FROM project_lifecycle_events WHERE project_id = %s",
        (project.id,),
    )
    assert row["reason"] == "gobby build resume"


def test_in_flight_agents_unaffected(monkeypatch: pytest.MonkeyPatch, temp_db) -> None:
    killed: list[str] = []
    monkeypatch.setattr(
        "gobby.build.service.kill_agent", lambda run_id: killed.append(run_id), raising=False
    )

    from gobby.build.service import build_stop

    build_stop(db=temp_db, project_id="0e27d5b7-167e-5a64-8bd9-6b980bd88f06")

    assert killed == []


@pytest.mark.asyncio
async def test_kick_no_op_when_dispatcher_disabled() -> None:
    from gobby.build.lifecycle import _kick_dispatcher_tick

    summary = await _kick_dispatcher_tick(dispatcher_enabled=False)

    assert summary.ticks == 0
    assert summary.reason == "automation_disabled"


@pytest.mark.asyncio
async def test_kick_fires_when_dispatcher_enabled() -> None:
    from gobby.build.lifecycle import _kick_dispatcher_tick

    summary = await _kick_dispatcher_tick(dispatcher_enabled=True)

    assert summary.ticks == 0
    assert summary.reason == "database_missing"


def test_no_task_flag_exposed() -> None:
    from gobby.cli.build import build_command

    param_names = {param.name for param in build_command.params}
    assert "task" not in param_names


def test_resume_cleanup_preserves_expired_mutex_for_active_run(temp_db) -> None:
    from gobby.build.control_runtime import _clear_stale_dispatch_mutexes
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import ensure_system_session, system_session_id
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager

    ensure_system_session(temp_db)
    project = LocalProjectManager(temp_db).create(
        name="resume-cleanup-active-run",
        repo_path="/tmp/resume-cleanup-active-run",
    )
    task_manager = LocalTaskManager(temp_db)
    active_task = task_manager.create_task(
        project_id=project.id,
        title="Active",
        category="code",
        validation_criteria="Test task completion is observable.",
    )
    orphan_task = task_manager.create_task(
        project_id=project.id,
        title="Orphan",
        category="code",
        validation_criteria="Test task completion is observable.",
    )
    run = LocalAgentRunManager(temp_db).create(
        parent_session_id=system_session_id(),
        provider="codex",
        prompt="work",
        task_id=active_task.id,
    )
    mutexes = TaskDispatchMutexManager(temp_db)
    expired_at = datetime.now(UTC) - timedelta(minutes=5)
    mutexes.acquire_mutex(
        active_task.id,
        holder="dispatcher",
        kind="heartbeat",
        ttl_seconds=60,
        run_id=run.id,
        now=expired_at,
    )
    mutexes.acquire_mutex(
        orphan_task.id,
        holder="dispatcher",
        kind="heartbeat",
        ttl_seconds=60,
        now=expired_at,
    )

    cleared = _clear_stale_dispatch_mutexes(
        temp_db,
        [active_task.id, orphan_task.id],
        now=datetime.now(UTC),
    )

    assert cleared == 1
    assert mutexes.get_mutex(active_task.id) is not None
    assert mutexes.get_mutex(orphan_task.id) is None


class _RecordingWake:
    """Wake callback recording deliveries with a configurable outcome."""

    def __init__(self, ism_persisted: bool) -> None:
        self._ism_persisted = ism_persisted
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def __call__(
        self, session_id: str, message: str, result: dict[str, object]
    ) -> dict[str, object]:
        self.calls.append((session_id, message, result))
        return {"ism_persisted": self._ism_persisted}


class TestBuildStopWakesWaiter:
    """Plan 1.4.10: build-stop agent cancellation delivers to the waiter."""

    def _harness(self, *, ism_persisted: bool) -> Any:
        from contextlib import nullcontext
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from gobby.events import CompletionEventRegistry

        wake = _RecordingWake(ism_persisted)
        registry = CompletionEventRegistry(wake_callback=wake)
        registry.register("run-123", ["waiter-sess"])

        run = SimpleNamespace(id="run-123", status="running", error=None)
        terminal_run = SimpleNamespace(id="run-123", status="cancelled", error=None)
        run_manager = MagicMock()
        run_manager.cancel.return_value = terminal_run
        run_manager.get.return_value = terminal_run
        db = MagicMock()
        db.bounded_transaction.return_value = nullcontext()
        services = SimpleNamespace(agent_lifecycle_monitor=None, completion_registry=registry)
        return SimpleNamespace(
            wake=wake,
            registry=registry,
            run=run,
            run_manager=run_manager,
            db=db,
            services=services,
        )

    def _record_removals(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
        import gobby.agents.completion_subscribers as subscribers_module

        removals: list[tuple[str, Any]] = []

        def _record(*, db: object, run_id: str, session_ids: Any = None) -> None:
            removals.append((run_id, session_ids))

        monkeypatch.setattr(subscribers_module, "remove_agent_completion_subscribers", _record)
        return removals

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ism_persisted", [True, False])
    async def test_cancelled_agent_delivery_settles_rows(
        self, monkeypatch: pytest.MonkeyPatch, ism_persisted: bool
    ) -> None:
        from unittest.mock import AsyncMock

        from gobby.build import control_runtime as runtime_module

        harness = self._harness(ism_persisted=ism_persisted)
        removals = self._record_removals(monkeypatch)
        monkeypatch.setattr(runtime_module, "LocalAgentRunManager", lambda _db: harness.run_manager)
        monkeypatch.setattr(runtime_module, "kill_agent", AsyncMock(return_value={"success": True}))

        await runtime_module._cancel_active_agents(
            harness.db, [harness.run], services=harness.services
        )

        assert [call[0] for call in harness.wake.calls] == ["waiter-sess"]
        assert harness.wake.calls[0][2]["run_id"] == "run-123"
        if ism_persisted:
            assert removals == [("run-123", ["waiter-sess"])]
        else:
            assert removals == []
        assert harness.registry.is_registered("run-123") is False

    @pytest.mark.asyncio
    async def test_kill_exception_after_capture_commit_still_delivers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        from gobby.build import control_runtime as runtime_module

        harness = self._harness(ism_persisted=True)
        removals = self._record_removals(monkeypatch)
        harness.run_manager.cancel.return_value = None
        monkeypatch.setattr(runtime_module, "LocalAgentRunManager", lambda _db: harness.run_manager)
        monkeypatch.setattr(
            runtime_module,
            "kill_agent",
            AsyncMock(side_effect=RuntimeError("kill exploded after capture commit")),
        )

        await runtime_module._cancel_active_agents(
            harness.db, [harness.run], services=harness.services
        )

        assert [call[0] for call in harness.wake.calls] == ["waiter-sess"]
        assert removals == [("run-123", ["waiter-sess"])]

    @pytest.mark.asyncio
    async def test_none_registry_performs_no_delivery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from gobby.build import control_runtime as runtime_module

        harness = self._harness(ism_persisted=True)
        removals = self._record_removals(monkeypatch)
        monkeypatch.setattr(runtime_module, "LocalAgentRunManager", lambda _db: harness.run_manager)
        monkeypatch.setattr(runtime_module, "kill_agent", AsyncMock(return_value={"success": True}))
        services = SimpleNamespace(agent_lifecycle_monitor=None, completion_registry=None)

        await runtime_module._cancel_active_agents(harness.db, [harness.run], services=services)

        assert harness.wake.calls == []
        assert removals == []


def _automated_task(
    temp_db: HubDatabase,
    project_id: str,
    *,
    title: str,
    task_type: str = "task",
    parent_task_id: str | None = None,
) -> Any:
    from gobby.storage.tasks import LocalTaskManager

    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=project_id,
        title=title,
        task_type=task_type,
        parent_task_id=parent_task_id,
        validation_criteria="Test task completion is observable.",
    )
    manager.initialize_task_manifest(task.id, stage_names=["development"])
    return manager.update_task(task.id, allow_automation=True, isolation="none")


def _seed_parked_daemon_stop_run(
    temp_db: HubDatabase,
    *,
    project_id: str,
    task_id: str,
    prefix: str,
) -> AgentRun:
    """Park a run the way daemon stop does: cancelled, unconsumed, session kept."""
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

    sessions = SessionManager(temp_db)
    parent = sessions.register(
        external_id=f"{prefix}-parent",
        machine_id=None,
        source="test",
        project_id=project_id,
    )
    child = sessions.register(
        external_id=f"{prefix}-child",
        machine_id=None,
        source="codex",
        project_id=project_id,
        parent_session_id=parent.id,
    )
    runs = LocalAgentRunManager(temp_db)
    run = runs.create(
        parent_session_id=parent.id,
        child_session_id=child.id,
        provider="codex",
        prompt="work",
        task_id=task_id,
    )
    temp_db.execute(
        "UPDATE sessions SET agent_run_id = %s, status = 'paused' WHERE id = %s",
        (run.id, child.id),
    )
    runs.start(run.id)
    parked = runs.cancel(run.id, terminal_reason="daemon_stop")
    assert parked is not None
    return parked


class TestBuildStopParkedDaemonStopRuns:
    """build stop gives up parked daemon-stop runs instead of waiting 24h."""

    def _services(self) -> SimpleNamespace:
        from unittest.mock import AsyncMock, MagicMock

        monitor = MagicMock()
        monitor.reap_daemon_stop_orphans = AsyncMock(return_value=1)
        return SimpleNamespace(agent_lifecycle_monitor=monitor, completion_registry=None)

    @pytest.mark.asyncio
    async def test_stop_gives_up_parked_runs_in_subtree(self, temp_db: HubDatabase) -> None:
        from gobby.build.controls import build_stop_target
        from gobby.storage.agents import LocalAgentRunManager
        from gobby.storage.daemon_resume_keys import REAP_REQUESTED_AT_KEY
        from gobby.storage.projects import LocalProjectManager

        project_id = (
            LocalProjectManager(temp_db)
            .create(
                name="stop-parked-subtree",
                repo_path="/tmp/stop-parked-subtree",
            )
            .id
        )
        root = _automated_task(temp_db, project_id, title="Root", task_type="epic")
        child = _automated_task(temp_db, project_id, title="Child", parent_task_id=root.id)
        outside = _automated_task(temp_db, project_id, title="Outside")
        parked = _seed_parked_daemon_stop_run(
            temp_db, project_id=project_id, task_id=child.id, prefix="in-scope"
        )
        outside_parked = _seed_parked_daemon_stop_run(
            temp_db, project_id=project_id, task_id=outside.id, prefix="out-of-scope"
        )
        services = self._services()

        result = await build_stop_target(
            f"#{root.seq_num}", db=temp_db, project_id=project_id, services=services
        )

        assert result.parked_runs_released == 1
        services.agent_lifecycle_monitor.reap_daemon_stop_orphans.assert_awaited_once()
        runs = LocalAgentRunManager(temp_db)
        flagged = runs.get(parked.id)
        assert flagged is not None
        assert flagged.resume_metadata_json is not None
        assert flagged.resume_metadata_json[REAP_REQUESTED_AT_KEY]
        # The reaper's own default selection now picks up the fresh flagged
        # run, so the real reap_daemon_stop_orphans performs the give-up.
        orphan_ids = {
            run.id for run in runs.list_daemon_stop_orphans(machine_id=require_machine_id())
        }
        assert parked.id in orphan_ids
        untouched = runs.get(outside_parked.id)
        assert untouched is not None
        assert not (untouched.resume_metadata_json or {}).get(REAP_REQUESTED_AT_KEY)
        assert outside_parked.id not in orphan_ids

    @pytest.mark.asyncio
    async def test_stop_without_parked_runs_skips_reap(self, temp_db: HubDatabase) -> None:
        from gobby.build.controls import build_stop_target
        from gobby.storage.projects import LocalProjectManager

        project_id = (
            LocalProjectManager(temp_db)
            .create(
                name="stop-no-parked",
                repo_path="/tmp/stop-no-parked",
            )
            .id
        )
        task = _automated_task(temp_db, project_id, title="Task")
        services = self._services()

        result = await build_stop_target(
            f"#{task.seq_num}", db=temp_db, project_id=project_id, services=services
        )

        assert result.parked_runs_released == 0
        services.agent_lifecycle_monitor.reap_daemon_stop_orphans.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_flags_parked_runs_without_lifecycle_monitor(
        self, temp_db: HubDatabase
    ) -> None:
        from gobby.build.controls import build_stop_target
        from gobby.storage.agents import LocalAgentRunManager
        from gobby.storage.daemon_resume_keys import REAP_REQUESTED_AT_KEY
        from gobby.storage.projects import LocalProjectManager

        project_id = (
            LocalProjectManager(temp_db)
            .create(
                name="stop-parked-no-monitor",
                repo_path="/tmp/stop-parked-no-monitor",
            )
            .id
        )
        task = _automated_task(temp_db, project_id, title="Task")
        parked = _seed_parked_daemon_stop_run(
            temp_db, project_id=project_id, task_id=task.id, prefix="no-monitor"
        )

        result = await build_stop_target(
            f"#{task.seq_num}", db=temp_db, project_id=project_id, services=None
        )

        assert result.parked_runs_released == 1
        runs = LocalAgentRunManager(temp_db)
        flagged = runs.get(parked.id)
        assert flagged is not None
        assert flagged.resume_metadata_json is not None
        assert flagged.resume_metadata_json[REAP_REQUESTED_AT_KEY]
        # The durable flag keeps the run eligible for the daemon's next
        # lifecycle tick even though no monitor was available here.
        assert parked.id in {
            run.id for run in runs.list_daemon_stop_orphans(machine_id=require_machine_id())
        }

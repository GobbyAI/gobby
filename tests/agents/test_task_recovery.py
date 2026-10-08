from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.task_recovery import TaskRecoveryHandler
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.workflows.state_manager import SessionVariableManager


@dataclass(frozen=True)
class _Run:
    id: str
    status: str
    task_id: str | None
    child_session_id: str | None
    claimed_session_id: str | None
    provider: str = "codex"
    error: str | None = "failed"
    pid: int | None = None
    terminal_id: str | None = None
    terminal_reason: str | None = None
    resume_metadata_json: dict[str, str] | None = None


@pytest.mark.asyncio
async def test_task_recovery_failure_does_not_log_exception_text(
    temp_db: HubDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "private marker"
    handler = TaskRecoveryHandler(
        LocalTaskManager(temp_db), _RunManager(), _Classifier(), run_db=_run_db
    )
    run = _Run("run-1", "error", "task-1", "child-1", "child-1")

    with (
        caplog.at_level("WARNING", logger="gobby.agents.task_recovery"),
        patch.object(
            handler,
            "resolve_claimed_task_for_run",
            new=AsyncMock(side_effect=RuntimeError(marker)),
        ),
    ):
        recovered = await handler.recover_task_from_terminal_agent(run, outcome="failed")

    assert recovered is False
    assert "RuntimeError" in caplog.text
    assert marker not in caplog.text


class _RunManager:
    def get(self, run_id: str) -> _Run | None:
        return None

    def list_by_status(
        self,
        status: str | None = None,
        limit: int = 100,
        project_id: str | None = None,
    ) -> list[_Run]:
        return []


class _SweepRunManager(_RunManager):
    """Serves fixed terminal runs to the lifecycle sweep by status."""

    def __init__(self, *runs: _Run) -> None:
        self._runs = runs

    def list_by_status(
        self,
        status: str | None = None,
        limit: int = 100,
        project_id: str | None = None,
    ) -> list[_Run]:
        return [run for run in self._runs if run.status == status][:limit]


_MERGE_EXISTING_VARIABLES = SessionVariableManager.merge_existing_variables


class _Classifier:
    def for_provider(self, provider_id: str) -> _Classifier:
        return self

    def is_provider_error(self, error_string: str | None) -> bool:
        return False

    def is_bootstrap_stall(self, error_string: str | None) -> bool:
        return False


async def _run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("consumed", [False, True])
async def test_daemon_stop_original_preserves_resumed_task_claim(
    temp_db: HubDatabase, sample_project: dict[str, Any], consumed: bool
) -> None:
    task_manager = LocalTaskManager(temp_db)
    session = SessionManager(temp_db).register(
        external_id="resume-task-owner",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
    )
    task = task_manager.create_task(
        sample_project["id"], "Resume task", validation_criteria="Ownership survives resume."
    )
    task_manager.claim_task(task.id, session.id)
    successor_id = "dddddddd-dddd-4ddd-8ddd-dddddddd2002"
    mutexes = TaskDispatchMutexManager(temp_db)
    assert mutexes.acquire_mutex(
        task.id, holder="dispatcher", kind="spawn", ttl_seconds=30, run_id=successor_id
    )
    metadata = (
        {
            "daemon_stop_resume_consumed_at": "2026-09-10T23:38:18+00:00",
            "daemon_stop_resume_consumed_by_run_id": successor_id,
        }
        if consumed
        else {}
    )
    original = _Run(
        id="dddddddd-dddd-4ddd-8ddd-dddddddd2001",
        status="cancelled",
        task_id=task.id,
        child_session_id=session.id,
        claimed_session_id=session.id,
        terminal_reason="daemon_stop",
        resume_metadata_json=metadata,
    )
    handler = TaskRecoveryHandler(task_manager, _RunManager(), _Classifier(), run_db=_run_db)

    for _ in range(2):
        assert not await handler.recover_task_from_terminal_agent(original, outcome="cancelled")
        assert task_manager.get_task(task.id).claimed_by_session_id == session.id
        assert mutexes.get_mutex(task.id) is not None


@pytest.mark.asyncio
async def test_failed_non_in_progress_recovery_releases_run_mutex(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    task_manager = LocalTaskManager(temp_db)
    session = SessionManager(temp_db).register(
        external_id="task-recovery-owner",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
    )
    task = task_manager.create_task(
        sample_project["id"],
        "Recover task",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.claim_task(task.id, session.id)
    mutexes = TaskDispatchMutexManager(temp_db)
    assert mutexes.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="spawn",
        ttl_seconds=30,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd2001",
    )
    run = _Run(
        id="dddddddd-dddd-4ddd-8ddd-dddddddd2001",
        status="failed",
        task_id=task.id,
        child_session_id=session.id,
        claimed_session_id=session.id,
    )
    handler = TaskRecoveryHandler(
        task_manager,
        _RunManager(),
        _Classifier(),
        run_db=_run_db,
    )

    recovered = await handler.recover_task_from_terminal_agent(run, outcome="failed")

    assert recovered is True
    assert task_manager.get_task(task.id).claimed_by_session_id is None
    assert mutexes.get_mutex(task.id) is None


@pytest.mark.asyncio
async def test_resolve_claimed_task_requires_child_session_ownership(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    task_manager = LocalTaskManager(temp_db)
    session = SessionManager(temp_db).register(
        external_id="task-recovery-claimed-owner",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
    )
    task = task_manager.create_task(
        sample_project["id"],
        "Recover claimed task",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.claim_task(task.id, session.id)
    handler = TaskRecoveryHandler(
        task_manager,
        _RunManager(),
        _Classifier(),
        run_db=_run_db,
    )

    # Ownership is narrowed to child_session_id (#17367): a run without a child
    # session never resolves, even when claimed_session_id matches the owner.
    childless_run = _Run(
        id="dddddddd-dddd-4ddd-8ddd-dddddddd2002",
        status="failed",
        task_id=task.id,
        child_session_id=None,
        claimed_session_id=session.id,
    )
    assert await handler.resolve_claimed_task_for_run(childless_run) is None

    owning_run = _Run(
        id="dddddddd-dddd-4ddd-8ddd-dddddddd2003",
        status="failed",
        task_id=task.id,
        child_session_id=session.id,
        claimed_session_id=None,
    )
    resolved = await handler.resolve_claimed_task_for_run(owning_run)
    assert resolved is not None
    assert resolved[0] == task.id


def test_clear_claim_session_variables_does_not_materialize_missing_rows(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    session_manager = SessionManager(temp_db)
    missing_session = session_manager.register(
        external_id="task-recovery-missing-variables",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
    )
    existing_session = session_manager.register(
        external_id="task-recovery-existing-variables",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
    )
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        sample_project["id"],
        "Recover variable state",
        validation_criteria="Test task completion is observable.",
    )
    variable_manager = SessionVariableManager(temp_db)
    variable_manager.merge_variables(
        existing_session.id,
        {
            "task_claimed": True,
            "claimed_tasks": {task.id: f"#{task.seq_num}"},
            "active_task_id": task.id,
            "task_edited_files": {task.id: ["src/gobby/example.py"]},
        },
    )
    handler = TaskRecoveryHandler(
        task_manager,
        _RunManager(),
        _Classifier(),
        run_db=_run_db,
    )

    for session in (missing_session, existing_session):
        handler._clear_claim_session_variables(
            _Run(
                id=f"recovery-{session.id}",
                status="cancelled",
                task_id=task.id,
                child_session_id=session.id,
                claimed_session_id=session.id,
            ),
            task.id,
        )

    assert (
        temp_db.fetchone(
            "SELECT 1 FROM session_variables WHERE session_id = %s",
            (missing_session.id,),
        )
        is None
    )
    existing_variables = variable_manager.get_variables(existing_session.id)
    assert task.id not in existing_variables["claimed_tasks"]
    # Recovery pauses the task rather than finishing it, so its edits stay attributed
    # for the parent coordinator's worktree checkpoint (#21897).
    assert existing_variables["task_edited_files"][task.id] == ["src/gobby/example.py"]


def test_release_task_claim_mutex_construction_type_error_falls_back() -> None:
    task_manager = MagicMock()
    task_manager.db = object()
    task_manager.release_task_claim.return_value = "released"
    handler = TaskRecoveryHandler(task_manager, _RunManager(), _Classifier())

    with patch(
        "gobby.agents.task_recovery.RuntimeDispatchMutex",
        side_effect=TypeError("old signature"),
    ):
        assert handler._release_task_claim_with_mutex("task-1") == "released"

    task_manager.release_task_claim.assert_called_once_with("task-1")


def test_release_task_claim_type_error_is_not_swallowed() -> None:
    class _Mutex:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_args: object) -> None:
            return None

    task_manager = MagicMock()
    task_manager.db = object()
    task_manager.release_task_claim.side_effect = TypeError("release failed")
    handler = TaskRecoveryHandler(task_manager, _RunManager(), _Classifier())

    with (
        patch("gobby.agents.task_recovery.RuntimeDispatchMutex", return_value=_Mutex()),
        pytest.raises(TypeError, match="release failed"),
    ):
        handler._release_task_claim_with_mutex("task-1")

    task_manager.release_task_claim.assert_called_once_with("task-1")


async def test_cancelled_run_sweep_keeps_the_live_claim_owner(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    """A coordinator that took over a cancelled worker's task keeps its claim variables."""
    session_manager = SessionManager(temp_db)
    worker = session_manager.register(
        external_id="task-recovery-cancelled-worker",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
    )
    coordinator = session_manager.register(
        external_id="task-recovery-live-coordinator",
        machine_id=None,
        source="claude",
        project_id=sample_project["id"],
    )
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        sample_project["id"],
        "Finish the worker's task",
        validation_criteria="The coordinator keeps its claim.",
    )
    task_manager.claim_task(task.id, coordinator.id)
    variable_manager = SessionVariableManager(temp_db)
    claim = {
        "task_claimed": True,
        "claimed_tasks": {task.id: f"#{task.seq_num}"},
        "active_task_id": task.id,
    }
    for session in (worker, coordinator):
        variable_manager.merge_variables(session.id, dict(claim))
    run = _Run(
        id="recovery-cancelled-worker",
        status="cancelled",
        task_id=task.id,
        child_session_id=worker.id,
        claimed_session_id=coordinator.id,
    )
    handler = TaskRecoveryHandler(
        task_manager, _SweepRunManager(run), _Classifier(), run_db=_run_db
    )

    # Two lifecycle sweeps clear the worker's stale claim exactly once (#23783).
    with patch.object(
        SessionVariableManager,
        "merge_existing_variables",
        autospec=True,
        side_effect=_MERGE_EXISTING_VARIABLES,
    ) as merge_vars:
        for _ in range(2):
            assert await handler.recover_tasks_from_terminal_agents() == 0

    assert [call.args[1] for call in merge_vars.call_args_list] == [worker.id]
    assert task.id not in variable_manager.get_variables(worker.id)["claimed_tasks"]
    kept = variable_manager.get_variables(coordinator.id)
    assert kept["task_claimed"] is True
    assert kept["claimed_tasks"] == {task.id: f"#{task.seq_num}"}
    assert task_manager.get_task(task.id).claimed_by_session_id == coordinator.id


def _register(temp_db: Any, project_id: str, external_id: str) -> str:
    return (
        SessionManager(temp_db)
        .register(external_id=external_id, machine_id=None, source="codex", project_id=project_id)
        .id
    )


@pytest.mark.asyncio
async def test_terminal_sweep_does_not_revisit_settled_runs(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    """A settled terminal run costs no task read or variable write on later sweeps (#23783)."""
    cancelled_child = _register(temp_db, sample_project["id"], "task-recovery-settled-cancel")
    failed_child = _register(temp_db, sample_project["id"], "task-recovery-settled-failed")
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        sample_project["id"],
        "Already released task",
        validation_criteria="The sweep settles.",
    )
    variable_manager = SessionVariableManager(temp_db)
    variable_manager.merge_variables(
        cancelled_child,
        {"task_claimed": True, "claimed_tasks": {task.id: f"#{task.seq_num}"}},
    )
    runs = _SweepRunManager(
        _Run("settled-cancelled", "cancelled", task.id, cancelled_child, cancelled_child),
        _Run("settled-failed", "error", None, failed_child, failed_child),
    )
    handler = TaskRecoveryHandler(task_manager, runs, _Classifier(), run_db=_run_db)

    with (
        patch.object(task_manager, "list_tasks", wraps=task_manager.list_tasks) as list_tasks,
        patch.object(task_manager, "get_task", wraps=task_manager.get_task) as get_task,
        patch.object(
            SessionVariableManager,
            "merge_existing_variables",
            autospec=True,
            side_effect=_MERGE_EXISTING_VARIABLES,
        ) as merge_vars,
    ):
        assert await handler.recover_tasks_from_terminal_agents() == 0
        assert list_tasks.call_count == 1
        get_task.assert_called()
        assert [call.args[1] for call in merge_vars.call_args_list] == [cancelled_child]
        for spy in (list_tasks, get_task, merge_vars):
            spy.reset_mock()

        assert await handler.recover_tasks_from_terminal_agents() == 0

    list_tasks.assert_not_called()
    get_task.assert_not_called()
    merge_vars.assert_not_called()
    assert variable_manager.get_variables(cancelled_child)["claimed_tasks"] == {}


@pytest.mark.asyncio
async def test_claimed_task_lookup_skips_hierarchy_page(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    """A run without task_id finds its claim without the hierarchy ordering (#23783)."""
    child = _register(temp_db, sample_project["id"], "task-recovery-lookup-child")
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        sample_project["id"],
        "Claimed without a run task",
        validation_criteria="The lookup finds it.",
    )
    task_manager.claim_task(task.id, child)
    handler = TaskRecoveryHandler(task_manager, _RunManager(), _Classifier(), run_db=_run_db)
    run = _Run("lookup-run", "error", None, child, child)

    with patch(
        "gobby.storage.tasks._queries._hierarchy_page",
        side_effect=AssertionError("recovery lookup took the hierarchy page"),
    ) as hierarchy_page:
        resolved = await handler.resolve_claimed_task_for_run(run)

    hierarchy_page.assert_not_called()
    assert resolved is not None
    assert resolved[0] == task.id


@pytest.mark.asyncio
async def test_unverified_terminal_run_is_retried_next_sweep(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    """A run whose agent can't be verified dead stays unsettled and keeps its claim."""
    child = _register(temp_db, sample_project["id"], "task-recovery-unverified-child")
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        sample_project["id"],
        "Owned by an unverified agent",
        validation_criteria="Recovery waits for liveness proof.",
    )
    task_manager.claim_task(task.id, child)
    run = _Run("unverified-run", "cancelled", task.id, child, child, terminal_id="term-1")
    handler = TaskRecoveryHandler(
        task_manager, _SweepRunManager(run), _Classifier(), run_db=_run_db
    )

    with patch.object(
        handler,
        "resolve_claimed_task_for_run",
        wraps=handler.resolve_claimed_task_for_run,
    ) as resolve:
        for _ in range(2):
            assert await handler.recover_tasks_from_terminal_agents() == 0

    assert resolve.call_count == 2
    assert task_manager.get_task(task.id).claimed_by_session_id == child


@pytest.mark.asyncio
async def test_sweep_releases_every_claim_before_settling(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    """A dead child holding two claims has both released before its run settles."""
    child = _register(temp_db, sample_project["id"], "task-recovery-two-claims-child")
    task_manager = LocalTaskManager(temp_db)
    tasks = [
        task_manager.create_task(
            sample_project["id"],
            f"Claimed task {index}",
            validation_criteria="Recovery releases it.",
        )
        for index in range(2)
    ]
    for task in tasks:
        task_manager.claim_task(task.id, child)
    run = _Run("eeeeeeee-eeee-4eee-8eee-eeeeeeee2378", "cancelled", None, child, child)
    handler = TaskRecoveryHandler(
        task_manager, _SweepRunManager(run), _Classifier(), run_db=_run_db
    )

    assert await handler.recover_tasks_from_terminal_agents() == 1
    assert await handler.recover_tasks_from_terminal_agents() == 1
    assert await handler.recover_tasks_from_terminal_agents() == 0
    with patch.object(task_manager, "list_tasks", wraps=task_manager.list_tasks) as list_tasks:
        assert await handler.recover_tasks_from_terminal_agents() == 0

    list_tasks.assert_not_called()
    assert [task_manager.get_task(task.id).claimed_by_session_id for task in tasks] == [None, None]

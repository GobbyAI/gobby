import inspect
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest

import gobby.storage.expansion_runs as expansion_runs_module
from gobby.storage.expansion_runs import ExpansionRun, ExpansionRunStatus, LocalExpansionRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.datetime import utc_now

pytestmark = pytest.mark.unit

# projects.id is a native uuid column.
PROJECT_ID = str(uuid.uuid4())


@pytest.fixture
def db(temp_db: HubDatabase):
    database = temp_db
    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_ID, "test_project")
        )
    return database


@pytest.fixture
def task_manager(db):
    return LocalTaskManager(db)


@pytest.fixture
def run_manager(db):
    return LocalExpansionRunManager(db)


@pytest.fixture
def parent_task(task_manager):
    return task_manager.create_task(project_id=PROJECT_ID, title="Parent expansion task")


ALL_STATUSES: tuple[ExpansionRunStatus, ...] = (
    "pending",
    "running",
    "compiled",
    "applying",
    "completed",
    "failed",
    "cancelled",
)
ACTIVE_STATUSES = ALL_STATUSES[:4]


def _transition(
    run_manager: LocalExpansionRunManager,
    transition: str,
    run_id: str,
) -> ExpansionRun | None:
    if transition == "start":
        return run_manager.start(run_id)
    if transition == "save_compiled_spec":
        return run_manager.save_compiled_spec(run_id, {"phases": [], "tasks": []})
    if transition == "mark_applying":
        return run_manager.mark_applying(run_id)
    if transition == "save_apply_result":
        return run_manager.save_apply_result(run_id, task_id_map={}, created_task_ids=[])
    if transition == "fail":
        return run_manager.fail(run_id, "worker failed")
    if transition == "cancel":
        return run_manager.cancel(run_id, "user cancelled")
    raise AssertionError(f"Unknown transition: {transition}")


def test_apply_result_case_condition_binds_boolean_for_postgres() -> None:
    source = inspect.getsource(expansion_runs_module.LocalExpansionRunManager.save_apply_result)

    assert "1 if completed else 0" not in source
    assert "completed_at = CASE WHEN %s THEN %s ELSE completed_at END" in source


def test_append_log_creates_first_entry(run_manager, parent_task) -> None:
    run = run_manager.create(
        parent_task_id=parent_task.id,
        project_id=PROJECT_ID,
        triggering_session_id=None,
        input_source="task",
    )

    updated = run_manager.append_log(run.id, level="info", message="first")

    assert updated is not None
    assert updated.logs is not None
    assert len(updated.logs) == 1
    assert updated.logs[0]["message"] == "first"


def test_append_log_is_atomic_against_stale_reads(run_manager, parent_task) -> None:
    run = run_manager.create(
        parent_task_id=parent_task.id,
        project_id=PROJECT_ID,
        triggering_session_id=None,
        input_source="task",
    )
    stale_snapshot = run_manager.get(run.id)
    assert stale_snapshot is not None
    stale_snapshot.logs = []

    with patch.object(run_manager, "get", return_value=stale_snapshot):
        run_manager.append_log(run.id, level="info", message="first")
        run_manager.append_log(run.id, level="info", message="second")

    persisted = LocalExpansionRunManager(run_manager.db).get(run.id)
    assert persisted is not None
    assert persisted.logs is not None
    assert [entry["message"] for entry in persisted.logs] == ["first", "second"]


@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
def test_cancel_preserves_terminal_run(run_manager, parent_task, terminal_status: str) -> None:
    """Late cancellation cannot overwrite a completed or failed run."""
    run = run_manager.create(
        parent_task_id=parent_task.id,
        project_id=parent_task.project_id,
        triggering_session_id=None,
        input_source="task",
    )
    if terminal_status == "completed":
        run_manager.db.execute(
            "UPDATE expansion_runs SET status = 'applying' WHERE id = %s",
            (run.id,),
        )
        before = run_manager.save_apply_result(
            run.id,
            task_id_map={},
            created_task_ids=[],
        )
    else:
        before = run_manager.fail(run.id, "compile failed")
    assert before is not None

    after = run_manager.cancel(run.id, error="late cancellation")

    assert after is None
    persisted = run_manager.get(run.id)
    assert persisted is not None
    assert persisted.status == terminal_status
    assert persisted.error == before.error
    assert persisted.completed_at == before.completed_at


def test_cancel_is_idempotent_after_first_transition(run_manager, parent_task) -> None:
    """Repeated cancellation preserves the first cancellation metadata."""
    run = run_manager.create(
        parent_task_id=parent_task.id,
        project_id=parent_task.project_id,
        triggering_session_id=None,
        input_source="task",
    )

    first = run_manager.cancel(run.id, error="first cancellation")
    second = run_manager.cancel(run.id, error="second cancellation")

    assert first is not None
    assert second is None
    assert first.status == "cancelled"
    persisted = run_manager.get(run.id)
    assert persisted is not None
    assert persisted.status == "cancelled"
    assert persisted.error == "first cancellation"
    assert persisted.completed_at == first.completed_at


@pytest.mark.parametrize(
    ("transition", "allowed_statuses", "result_status"),
    [
        ("start", ("pending",), "running"),
        ("save_compiled_spec", ("running",), "compiled"),
        ("mark_applying", ("compiled",), "applying"),
        ("save_apply_result", ("applying",), "completed"),
        ("fail", ACTIVE_STATUSES, "failed"),
        ("cancel", ACTIVE_STATUSES, "cancelled"),
    ],
)
def test_state_transitions_require_allowed_status(
    run_manager,
    parent_task,
    transition: str,
    allowed_statuses: tuple[ExpansionRunStatus, ...],
    result_status: ExpansionRunStatus,
) -> None:
    for source_status in ALL_STATUSES:
        run = run_manager.create(
            parent_task_id=parent_task.id,
            project_id=parent_task.project_id,
            triggering_session_id=None,
            input_source="task",
        )
        run_manager.db.execute(
            "UPDATE expansion_runs SET status = %s WHERE id = %s",
            (source_status, run.id),
        )

        result = _transition(run_manager, transition, run.id)
        persisted = run_manager.get(run.id)

        assert persisted is not None
        if source_status in allowed_statuses:
            assert result is not None
            assert persisted.status == result_status
        else:
            assert result is None
            assert persisted.status == source_status


@pytest.mark.parametrize("status", ["running", "applying"])
def test_cleanup_stale_runs_fails_in_flight_run(run_manager, parent_task, status: str) -> None:
    run = run_manager.create(
        parent_task_id=parent_task.id,
        project_id=parent_task.project_id,
        triggering_session_id=None,
        input_source="task",
    )
    stale_at = utc_now() - timedelta(minutes=31)
    run_manager.db.execute(
        "UPDATE expansion_runs SET status = %s, updated_at = %s WHERE id = %s",
        (status, stale_at, run.id),
    )

    assert run_manager.cleanup_stale_runs(timeout_minutes=30) == 1

    cleaned = run_manager.get(run.id)
    assert cleaned is not None
    assert cleaned.status == "failed"
    assert cleaned.completed_at is not None
    assert cleaned.error == "Expansion run exceeded stale timeout (30m)"


def test_cleanup_stale_runs_preserves_recent_and_other_task_runs(
    run_manager, task_manager, parent_task
) -> None:
    other_task = task_manager.create_task(project_id=PROJECT_ID, title="Other expansion task")
    recent = run_manager.create(
        parent_task_id=parent_task.id,
        project_id=parent_task.project_id,
        triggering_session_id=None,
        input_source="task",
    )
    other = run_manager.create(
        parent_task_id=other_task.id,
        project_id=other_task.project_id,
        triggering_session_id=None,
        input_source="task",
    )
    stale_at = utc_now() - timedelta(minutes=31)
    run_manager.db.execute(
        "UPDATE expansion_runs SET status = 'running' WHERE id = %s",
        (recent.id,),
    )
    run_manager.db.execute(
        "UPDATE expansion_runs SET status = 'running', updated_at = %s WHERE id = %s",
        (stale_at, other.id),
    )

    assert run_manager.cleanup_stale_runs(parent_task_id=parent_task.id) == 0
    assert run_manager.get(recent.id).status == "running"
    assert run_manager.get(other.id).status == "running"

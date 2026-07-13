import inspect
import uuid
from unittest.mock import patch

import pytest

import gobby.storage.expansion_runs as expansion_runs_module
from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager

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
        before = run_manager.save_apply_result(
            run.id,
            task_id_map={},
            created_task_ids=[],
        )
    else:
        before = run_manager.fail(run.id, "compile failed")
    assert before is not None

    after = run_manager.cancel(run.id, error="late cancellation")

    assert after is not None
    assert after.status == terminal_status
    assert after.error == before.error
    assert after.completed_at == before.completed_at


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
    assert second is not None
    assert first.status == "cancelled"
    assert second.status == "cancelled"
    assert second.error == "first cancellation"
    assert second.completed_at == first.completed_at

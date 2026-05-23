import inspect
from unittest.mock import patch

import pytest

import gobby.storage.expansion_runs as expansion_runs_module
from gobby.storage.database import LocalDatabase
from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.tasks import LocalTaskManager
from tests.fixtures.migrations import run_migrations

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path):
    database = LocalDatabase(tmp_path / "expansion_runs.db")
    run_migrations(database)
    with database.transaction() as conn:
        conn.execute("INSERT INTO projects (id, name) VALUES (?, ?)", ("p1", "test_project"))
    return database


@pytest.fixture
def task_manager(db):
    return LocalTaskManager(db)


@pytest.fixture
def run_manager(db):
    return LocalExpansionRunManager(db)


@pytest.fixture
def parent_task(task_manager):
    return task_manager.create_task(project_id="p1", title="Parent expansion task")


def test_apply_result_case_condition_binds_boolean_for_postgres() -> None:
    source = inspect.getsource(expansion_runs_module.LocalExpansionRunManager.save_apply_result)

    assert "1 if completed else 0" not in source
    assert "completed_at = CASE WHEN ? THEN ? ELSE completed_at END" in source


def test_append_log_creates_first_entry(run_manager, parent_task) -> None:
    run = run_manager.create(
        parent_task_id=parent_task.id,
        project_id="p1",
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
        project_id="p1",
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

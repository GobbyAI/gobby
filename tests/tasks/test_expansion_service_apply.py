from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.expansion_service import ExpansionService

pytestmark = pytest.mark.unit


@pytest.fixture
def task_manager(temp_db):
    return LocalTaskManager(temp_db)


@pytest.fixture
def run_manager(temp_db):
    return LocalExpansionRunManager(temp_db)


@pytest.fixture
def service(task_manager, run_manager):
    return ExpansionService(task_manager=task_manager, llm_service=MagicMock(), run_manager=run_manager)


def test_apply_run_rolls_back_partial_writes_on_failure(service, task_manager, run_manager, sample_project):
    parent = task_manager.create_task(project_id=sample_project["id"], title="Parent expansion")
    run = run_manager.create(
        parent_task_id=parent.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="task",
    )
    compiled_spec = {
        "phases": [{"id": "phase-1", "title": "Phase 1", "task_ids": ["task-1"]}],
        "tasks": [
            {
                "id": "task-1",
                "phase_id": "phase-1",
                "title": "First child",
                "category": "docs",
                "affected_files": ["docs/plan.md"],
            }
        ],
        "dependencies": [],
    }
    run_manager.save_compiled_spec(run.id, compiled_spec)

    with patch.object(service, "_add_dependency", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="boom"):
            service.apply_run(run.id, session_id=None)

    children = task_manager.list_tasks(parent_task_id=parent.id)
    assert children == []

    affected_files = task_manager.db.fetchone("SELECT COUNT(*) AS count FROM task_affected_files")
    assert affected_files is not None
    assert affected_files["count"] == 0

    dependencies = task_manager.db.fetchone("SELECT COUNT(*) AS count FROM task_dependencies")
    assert dependencies is not None
    assert dependencies["count"] == 0

    refreshed = run_manager.get(run.id)
    assert refreshed is not None
    assert refreshed.status == "compiled"
    assert refreshed.task_id_map is None
    assert refreshed.created_task_ids is None

"""Phase 2 tests for expansion apply behavior."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.expansion_service import ExpansionService

pytestmark = pytest.mark.unit


def test_apply_copies_parent_target_branch_onto_generated_leaves(temp_db, sample_project) -> None:
    from gobby.tasks.expansion import _apply

    task_manager = LocalTaskManager(temp_db)
    artifact_manager = task_manager.artifacts
    run_manager = LocalExpansionRunManager(temp_db)
    service = ExpansionService(
        task_manager=task_manager,
        llm_service=MagicMock(),
        run_manager=run_manager,
    )
    parent = task_manager.create_task(
        project_id=sample_project["id"],
        title="Expansion parent",
        task_type="epic",
    )
    artifact_manager.set_artifact(parent.id, "target_branch", "release/0.4")
    run = run_manager.create(
        parent_task_id=parent.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="task",
    )
    run_manager.save_compiled_spec(
        run.id,
        {
            "phases": [{"id": "phase-1", "title": "Phase 1", "task_ids": ["leaf"]}],
            "tasks": [
                {
                    "id": "leaf",
                    "phase_id": "phase-1",
                    "title": "Implement leaf",
                    "category": "code",
                    "assigned_agent": "backend-developer",
                }
            ],
            "dependencies": [],
        },
    )

    applied = _apply.apply_run(service, run.id, session_id=None)

    child_id = applied.task_id_map["leaf"]
    assert artifact_manager.get_artifacts(child_id).target_branch == "release/0.4"

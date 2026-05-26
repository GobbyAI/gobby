from __future__ import annotations

import pytest

from gobby.storage.tasks._crud import list_automation_candidates
from gobby.storage.tasks._models import Isolation
from gobby.tasks.state_semantics import ACTIVE_STAGE_STATES
from tests.storage.tasks._stage_test_helpers import (
    create_task,
    initialize_manifest,
    set_stage_state,
    spec,
)

pytestmark = pytest.mark.unit


def _task_at_stage(temp_db, sample_project, stage_state: str):
    task = create_task(
        temp_db,
        sample_project,
        title=f"Candidate {stage_state}",
        category="test",
        task_type="task",
    )
    initialize_manifest(temp_db, task.id, [spec("planning", 0)])
    set_stage_state(temp_db, task.id, "planning", stage_state)
    temp_db.execute(
        "UPDATE tasks SET allow_automation = TRUE, isolation = ? WHERE id = ?",
        (Isolation.none.value, task.id),
    )
    return task


def test_list_automation_candidates_includes_stage_actionable_states(
    temp_db,
    sample_project,
) -> None:
    actionable = {
        state: _task_at_stage(temp_db, sample_project, state) for state in ACTIVE_STAGE_STATES
    }

    candidate_ids = {
        task.id for task in list_automation_candidates(temp_db, project_id=sample_project["id"])
    }

    assert {task.id for task in actionable.values()} <= candidate_ids


def test_list_automation_candidates_excludes_done_and_null_current_stage(
    temp_db,
    sample_project,
) -> None:
    done = _task_at_stage(temp_db, sample_project, "done")
    no_manifest = create_task(
        temp_db,
        sample_project,
        title="No manifest",
        category="test",
        task_type="task",
    )
    temp_db.execute(
        "UPDATE tasks SET allow_automation = TRUE, isolation = ? WHERE id = ?",
        (Isolation.none.value, no_manifest.id),
    )
    temp_db.execute("DELETE FROM task_stage_states WHERE task_id = ?", (no_manifest.id,))

    candidate_ids = {
        task.id for task in list_automation_candidates(temp_db, project_id=sample_project["id"])
    }

    assert done.id not in candidate_ids
    assert no_manifest.id not in candidate_ids

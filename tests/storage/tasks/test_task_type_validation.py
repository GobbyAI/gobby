"""Phase 5 task-type validation contracts."""

from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager
from tests.phase5_contract_helpers import NEW_TASK_TYPES

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("task_type", NEW_TASK_TYPES)
def test_new_types_accepted(temp_db, sample_project, task_type: str) -> None:
    manager = LocalTaskManager(temp_db)

    task = manager.create_task(
        project_id=sample_project["id"],
        title=f"Create {task_type}",
        task_type=task_type,
        validation_criteria="Test task completion is observable.",
    )

    assert task.task_type == task_type


def test_review_anchor_rejected(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)

    with pytest.raises(ValueError, match="Invalid task_type 'review_anchor'"):
        manager.create_task(
            project_id=sample_project["id"],
            title="Retired review anchor",
            task_type="review_anchor",
            validation_criteria="Test task completion is observable.",
        )


def test_review_anchor_rejected_on_update(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Ordinary task",
        task_type="task",
        validation_criteria="Test task completion is observable.",
    )

    with pytest.raises(ValueError, match="Invalid task_type 'review_anchor'"):
        manager.update_task(task.id, task_type="review_anchor")

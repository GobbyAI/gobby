"""Red tests for build cascade dispatch state naming."""

from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._build_cascade import cascade_build_state_to_subtree

pytestmark = pytest.mark.unit


def test_cascade_uses_unattended_field(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Cascade unattended",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    manager.initialize_task_manifest(task.id, stage_names=["development"])

    cascade_build_state_to_subtree(
        temp_db,
        task.id,
        isolation="none",
        unattended=True,
        allow_automation=True,
        skip_stages=["qa"],
    )
    row = temp_db.fetchone("SELECT unattended FROM tasks WHERE id = %s", (task.id,))

    assert row["unattended"] == 1

"""Red tests for build cascade dispatch state naming."""

from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._crud import cascade_build_state_to_subtree

pytestmark = pytest.mark.unit


def test_cascade_uses_unattended_field(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Cascade unattended",
        task_type="epic",
    )

    cascade_build_state_to_subtree(
        temp_db,
        task.id,
        isolation="none",
        unattended=True,
        skip_stage_labels=["stage-:qa"],
        allow_automation=True,
    )
    row = temp_db.fetchone("SELECT unattended FROM tasks WHERE id = ?", (task.id,))

    assert row["unattended"] == 1


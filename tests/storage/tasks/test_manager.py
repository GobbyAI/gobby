"""Tests for LocalTaskManager task creation helpers."""

from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def test_create_task_is_metadata_only(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)

    task = manager.create_task(
        project_id=sample_project["id"],
        title="Metadata-only task",
        task_type="task",
    )

    assert manager.stage_states.list_for_task(task.id) == []
    assert manager.stage_states.current_stage(task.id) is None


def test_initialize_task_manifest_supports_stage_override_with_caps(
    temp_db, sample_project
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Override stages",
        task_type="task",
    )

    manager.initialize_task_manifest(
        task.id,
        stage_names=["development", "pr", "merge"],
        stage_caps=[{"stage_name": "development", "max_review_rounds": 2}],
    )

    rows = manager.stage_states.list_for_task(task.id)
    assert [row.stage_name for row in rows] == ["development", "pr", "merge"]
    assert rows[0].max_review_rounds == 2


def test_initialize_task_manifest_rejects_unknown_stage_override(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Bad stages",
        task_type="task",
    )

    with pytest.raises(ValueError, match="Unknown stage 'missing'"):
        manager.initialize_task_manifest(task.id, stage_names=["development", "missing"])

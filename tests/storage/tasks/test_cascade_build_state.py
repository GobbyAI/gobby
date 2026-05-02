"""Tests for build dispatch state cascades in task storage."""

from __future__ import annotations

import pytest

from gobby.storage.tasks import Isolation, LocalTaskManager, cascade_build_state_to_subtree
from tests.phase5_contract_helpers import source_text

pytestmark = pytest.mark.unit


def test_cascade_build_state_updates_subtree_without_agent_or_lifecycle_fields(
    temp_db,
    sample_project,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    epic = task_manager.create_task(
        project_id=sample_project["id"],
        title="Automated epic",
        task_type="epic",
        category="planning",
        labels=["keep-me"],
    )
    child_epic = task_manager.create_task(
        project_id=sample_project["id"],
        title="Child epic",
        parent_task_id=epic.id,
        task_type="epic",
        category="planning",
    )
    leaf = task_manager.create_task(
        project_id=sample_project["id"],
        title="Leaf task",
        parent_task_id=child_epic.id,
        category="code",
        assigned_agent="backend-developer",
        additional_skills=["sql-review"],
    )
    sibling = task_manager.create_task(
        project_id=sample_project["id"],
        title="Sibling task",
        parent_task_id=epic.id,
        category="docs",
    )

    kwargs = {
        "isolation": Isolation.clone,
        "unattended": True,
        "allow_automation": True,
    }

    updated_count = cascade_build_state_to_subtree(temp_db, epic.id, **kwargs)

    assert updated_count == 4
    for task_id in (epic.id, child_epic.id, leaf.id, sibling.id):
        task = task_manager.get_task(task_id)
        assert task.allow_automation is True
        assert task.unattended is True
        assert task.isolation is Isolation.clone
        assert not any(label.startswith("stage-:") for label in task.labels or [])

    updated_epic = task_manager.get_task(epic.id)
    updated_leaf = task_manager.get_task(leaf.id)
    assert "keep-me" in (updated_epic.labels or [])
    assert updated_leaf.assigned_agent == "backend-developer"
    assert updated_leaf.additional_skills == ["sql-review"]


def test_cascade_uses_initialize_manifest() -> None:
    source = source_text("src/gobby/storage/tasks/_crud.py")

    assert "initialize_manifest(" in source


def test_cascade_no_legacy_label_writes() -> None:
    source = source_text("src/gobby/storage/tasks/_crud.py")

    assert "skip_stage_labels" not in source
    assert "_normalize_skip_stage_labels" not in source
    assert "stage-:" not in source

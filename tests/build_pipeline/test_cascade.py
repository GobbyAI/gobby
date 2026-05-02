"""Tests for build-time cascade behavior."""

from __future__ import annotations

import pytest

from gobby.build.service import BuildOptions, BuildResult, build
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


async def _build(input_ref: str, opts: BuildOptions, db: object, project_id: str) -> BuildResult:
    return await build(input_ref, opts, db=db, project_id=project_id)


def _options(**overrides: object) -> BuildOptions:
    values = {
        "profile": "full-unattended",
        "skip_stages": ["test_arch", "qa"],
        "isolation": "clone",
        "unattended": True,
        "composer_yolo": False,
        "target_branch": "main",
        "assigned_agent": None,
    }
    values.update(overrides)
    return BuildOptions(**values)


def _tree(task_manager: LocalTaskManager, project_id: str) -> tuple[object, list[object]]:
    epic = task_manager.create_task(
        project_id=project_id,
        title="Automated epic",
        task_type="epic",
        category="planning",
    )
    child_epic = task_manager.create_task(
        project_id=project_id,
        title="Child epic",
        parent_task_id=epic.id,
        task_type="epic",
        category="planning",
    )
    leaf_a = task_manager.create_task(
        project_id=project_id,
        title="Config leaf",
        parent_task_id=child_epic.id,
        task_type="task",
        category="config",
    )
    leaf_b = task_manager.create_task(
        project_id=project_id,
        title="Docs leaf",
        parent_task_id=epic.id,
        task_type="task",
        category="docs",
    )
    task_manager.update_task(
        leaf_a.id,
        assigned_agent="documentation-specialist",
        additional_skills=["release-notes"],
        lifecycle="open",
    )
    task_manager.update_task(
        leaf_b.id,
        assigned_agent="backend-developer",
        additional_skills=["api-review"],
        lifecycle="holistic_review",
    )
    return epic, [child_epic, leaf_a, leaf_b]


@pytest.mark.asyncio
async def test_build_epic_cascades_resolved_dispatch_state_to_subtree(
    temp_db,
    sample_project,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    epic, descendants = _tree(task_manager, sample_project["id"])

    result = await _build(
        f"#{epic.seq_num}",
        _options(),
        db=temp_db,
        project_id=sample_project["id"],
    )

    assert result.created is False
    for task in [
        task_manager.get_task(epic.id),
        *[task_manager.get_task(item.id) for item in descendants],
    ]:
        assert task.allow_automation is True
        assert task.isolation == "clone"
        assert task.unattended is True
        assert {"stage-:test_arch"}.issubset(set(task.labels))
        assert "stage-:qa" not in set(task.labels)


@pytest.mark.asyncio
async def test_build_epic_does_not_cascade_agent_skills_or_lifecycle(
    temp_db,
    sample_project,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    epic, descendants = _tree(task_manager, sample_project["id"])
    leaf_a = descendants[1]
    leaf_b = descendants[2]

    await _build(
        f"#{epic.seq_num}",
        _options(assigned_agent="planner"),
        db=temp_db,
        project_id=sample_project["id"],
    )

    updated_epic = task_manager.get_task(epic.id)
    updated_leaf_a = task_manager.get_task(leaf_a.id)
    updated_leaf_b = task_manager.get_task(leaf_b.id)
    assert updated_epic.assigned_agent is None
    assert updated_epic.additional_skills is None
    assert updated_leaf_a.assigned_agent == "documentation-specialist"
    assert updated_leaf_a.additional_skills == ["release-notes"]
    assert updated_leaf_a.lifecycle == "open"
    assert updated_leaf_b.assigned_agent == "backend-developer"
    assert updated_leaf_b.additional_skills == ["api-review"]
    assert updated_leaf_b.lifecycle == "holistic_review"

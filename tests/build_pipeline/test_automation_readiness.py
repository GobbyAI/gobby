from __future__ import annotations

import pytest

from gobby.build.service import BuildOptions, build
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._automation import list_automation_candidates

pytestmark = pytest.mark.unit


def _options(**overrides: object) -> BuildOptions:
    values = {
        "quick": False,
        "skip_stages": [],
        "isolation": "none",
        "no_merge": False,
        "pr": None,
        "target_branch": "main",
        "assigned_agent": None,
    }
    values.update(overrides)
    return BuildOptions(**values)


def _expanded_epic(
    task_manager: LocalTaskManager,
    project_id: str,
) -> tuple[Task, list[Task]]:
    epic = task_manager.create_task(
        project_id=project_id,
        title="Readiness epic",
        task_type="epic",
        category="planning",
        validation_criteria="Test task completion is observable.",
    )
    leaf_code = task_manager.create_task(
        project_id=project_id,
        title="Code leaf",
        parent_task_id=epic.id,
        task_type="task",
        category="code",
        validation_criteria="Test task completion is observable.",
    )
    leaf_test = task_manager.create_task(
        project_id=project_id,
        title="Test leaf",
        parent_task_id=epic.id,
        task_type="task",
        category="test",
        validation_criteria="Test task completion is observable.",
    )
    return epic, [leaf_code, leaf_test]


@pytest.mark.asyncio
async def test_build_readiness_cascades_manifests_and_current_stage_projection(
    temp_db,
    sample_git_project,
) -> None:
    from gobby.agents.sync import sync_bundled_agents

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    epic, leaves = _expanded_epic(task_manager, sample_git_project["id"])

    result = await build(
        f"#{epic.seq_num}",
        _options(),
        db=temp_db,
        project_id=sample_git_project["id"],
    )

    subtree = [task_manager.get_task(epic.id), *[task_manager.get_task(leaf.id) for leaf in leaves]]
    assert result.created is False
    assert result.manifest is not None
    assert result.tick_dispatched >= 1
    epic_artifacts = task_manager.artifacts.get_artifacts(epic.id)
    for task in subtree:
        assert task.allow_automation is True
        assert task.unattended is False
        assert getattr(task.isolation, "value", task.isolation) == "worktree"
        artifacts = task_manager.artifacts.get_artifacts(task.id)
        expected_target = "main" if task.id == epic.id else epic_artifacts.integration_branch
        assert artifacts.target_branch == expected_target
        rows = task_manager.stage_states.list_for_task(task.id)
        assert rows
        assert "test_arch" not in {row.stage_name for row in rows}
        current = task_manager.stage_states.current_stage(task.id)
        assert current is not None
        assert current.position == min(
            (row.position for row in rows if row.state != "done"),
            default=current.position,
        )

    candidate_ids = {
        task.id for task in list_automation_candidates(temp_db, project_id=sample_git_project["id"])
    }
    assert {task.id for task in subtree} <= candidate_ids

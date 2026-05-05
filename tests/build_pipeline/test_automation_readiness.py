from __future__ import annotations

import pytest

from gobby.build.service import BuildOptions, build
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._crud import list_automation_candidates

pytestmark = pytest.mark.unit


def _options(**overrides: object) -> BuildOptions:
    values = {
        "quick": False,
        "skip_stages": ["test_arch"],
        "isolation": "clone",
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
    )
    leaf_code = task_manager.create_task(
        project_id=project_id,
        title="Code leaf",
        parent_task_id=epic.id,
        task_type="task",
        category="code",
    )
    leaf_test = task_manager.create_task(
        project_id=project_id,
        title="Test leaf",
        parent_task_id=epic.id,
        task_type="task",
        category="test",
    )
    return epic, [leaf_code, leaf_test]


@pytest.mark.asyncio
async def test_build_readiness_cascades_manifests_and_current_stage_projection(
    temp_db,
    sample_project,
) -> None:
    from gobby.agents.sync import sync_bundled_agents

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    epic, leaves = _expanded_epic(task_manager, sample_project["id"])

    result = await build(
        f"#{epic.seq_num}",
        _options(),
        db=temp_db,
        project_id=sample_project["id"],
    )

    subtree = [task_manager.get_task(epic.id), *[task_manager.get_task(leaf.id) for leaf in leaves]]
    assert result.created is False
    assert result.manifest is not None
    assert result.tick_dispatched == len(subtree)
    for task in subtree:
        assert task.allow_automation is True
        assert task.unattended is False
        assert task.isolation == "clone"
        assert task_manager.artifacts.get_artifacts(task.id).target_branch == "main"
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
        task.id for task in list_automation_candidates(temp_db, project_id=sample_project["id"])
    }
    assert {task.id for task in subtree} <= candidate_ids

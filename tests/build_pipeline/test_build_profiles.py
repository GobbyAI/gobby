from __future__ import annotations

import pytest

from gobby.build.service import BuildOptions, build
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def _options(**overrides: object) -> BuildOptions:
    values = {
        "profile": "full",
        "skip_stages": [],
        "isolation": "none",
        "unattended": False,
        "composer_yolo": False,
        "target_branch": None,
        "assigned_agent": "backend-developer",
    }
    values.update(overrides)
    return BuildOptions(**values)


async def test_quick_profile_skips_research_and_holistic_qa(temp_db, sample_project) -> None:
    task_manager = LocalTaskManager(temp_db)
    epic = task_manager.create_task(
        project_id=sample_project["id"],
        title="Quick profile epic",
        category="planning",
        task_type="epic",
    )

    result = await build(
        f"#{epic.seq_num}",
        _options(profile="quick"),
        db=temp_db,
        project_id=sample_project["id"],
    )

    stage_names = [row["stage_name"] for row in result.manifest or []]
    assert result.applied_stages_skipped == ["research", "holistic_qa"]
    assert "research" not in stage_names
    assert "holistic_qa" not in stage_names
    assert {"ideation", "development", "merge"}.issubset(stage_names)


@pytest.mark.parametrize("profile", ["review", "full"])
async def test_review_and_full_profiles_skip_nothing(
    temp_db,
    sample_project,
    profile: str,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    epic = task_manager.create_task(
        project_id=sample_project["id"],
        title=f"{profile} profile epic",
        category="planning",
        task_type="epic",
    )

    result = await build(
        f"#{epic.seq_num}",
        _options(profile=profile),
        db=temp_db,
        project_id=sample_project["id"],
    )

    assert result.applied_stages_skipped == []
    assert [row["stage_name"] for row in result.manifest or []] == [
        "ideation",
        "research",
        "architecture",
        "prd",
        "planning",
        "test_arch",
        "expansion",
        "development",
        "holistic_qa",
        "pr",
        "merge",
    ]


async def test_full_yolo_profile_sets_unattended(temp_db, sample_project) -> None:
    task_manager = LocalTaskManager(temp_db)
    leaf = task_manager.create_task(
        project_id=sample_project["id"],
        title="Full yolo leaf",
        category="test",
        task_type="task",
    )

    await build(
        f"#{leaf.seq_num}",
        _options(profile="full-yolo"),
        db=temp_db,
        project_id=sample_project["id"],
    )

    assert task_manager.get_task(leaf.id).unattended is True

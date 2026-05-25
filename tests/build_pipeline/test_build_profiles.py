from __future__ import annotations

from typing import Any

import pytest

from gobby.build.service import BuildOptions, build
from gobby.storage.build_profiles import BuildProfileLoader, BuildProfileManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def _options(**overrides: object) -> BuildOptions:
    values = {
        "quick": False,
        "skip_stages": [],
        "isolation": "none",
        "no_merge": False,
        "pr": None,
        "target_branch": None,
        "assigned_agent": "backend-developer",
    }
    values.update(overrides)
    return BuildOptions(**values)


async def test_quick_runs_one_step_and_disables_automation(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    task_manager = LocalTaskManager(temp_db)
    epic = task_manager.create_task(
        project_id=sample_project["id"],
        title="Quick build epic",
        category="planning",
        task_type="epic",
    )

    result = await build(
        f"#{epic.seq_num}",
        _options(quick=True),
        db=temp_db,
        project_id=sample_project["id"],
    )

    stage_names = [row["stage_name"] for row in result.manifest or []]
    assert result.applied_stages_skipped == []
    assert {"ideation", "development", "merge"}.issubset(stage_names)
    assert task_manager.get_task(epic.id).allow_automation is False


async def test_default_build_skips_nothing(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    task_manager = LocalTaskManager(temp_db)
    epic = task_manager.create_task(
        project_id=sample_project["id"],
        title="Default build epic",
        category="planning",
        task_type="epic",
    )

    result = await build(
        f"#{epic.seq_num}",
        _options(),
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
        "expansion",
        "development",
        "holistic_qa",
        "pr",
        "merge",
    ]


async def test_research_leaf_defaults_to_research_stage(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    task_manager = LocalTaskManager(temp_db)
    leaf = task_manager.create_task(
        project_id=sample_project["id"],
        title="Research leaf",
        category="research",
        task_type="task",
    )

    result = await build(
        f"#{leaf.seq_num}",
        _options(),
        db=temp_db,
        project_id=sample_project["id"],
    )

    assert [row["stage_name"] for row in result.manifest or []] == ["research"]


async def test_submit_profile_records_same_repo_delivery_campaign(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Submit leaf",
        category="code",
        task_type="feature",
        validation_criteria="opens a PR",
    )

    await build(
        f"#{task.seq_num}",
        _options(profile="submit", profile_explicit=True),
        db=temp_db,
        project_id=sample_project["id"],
    )

    row = temp_db.fetchone(
        """
        SELECT delivery_mode, source_repo, target_repo
        FROM task_delivery_campaigns
        WHERE task_id = ?
        """,
        (task.id,),
    )
    assert row["delivery_mode"] == "pull_request"
    assert row["source_repo"] == "test/test-project"
    assert row["target_repo"] == "test/test-project"


async def test_submit_profile_records_cross_repo_delivery_campaign(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    BuildProfileLoader().sync(temp_db)
    BuildProfileManager(temp_db).create(
        name="submit",
        display_label="Submit Override",
        description="Submit to upstream repo.",
        skip_stages=["merge"],
        isolation="worktree",
        unattended=False,
        delivery_mode="pull_request",
        delivery_target_repo="upstream/test-project",
        source="project",
        project_id=sample_project["id"],
    )
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Cross repo submit leaf",
        category="code",
        task_type="feature",
        validation_criteria="opens an upstream PR",
    )

    await build(
        f"#{task.seq_num}",
        _options(profile="submit", profile_explicit=True),
        db=temp_db,
        project_id=sample_project["id"],
    )

    row = temp_db.fetchone(
        """
        SELECT delivery_mode, source_repo, target_repo
        FROM task_delivery_campaigns
        WHERE task_id = ?
        """,
        (task.id,),
    )
    assert row["delivery_mode"] == "pull_request"
    assert row["source_repo"] == "test/test-project"
    assert row["target_repo"] == "upstream/test-project"

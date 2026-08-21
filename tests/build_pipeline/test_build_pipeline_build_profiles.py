from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from gobby.build.profiles import resolve_build_profile_options
from gobby.build.service import BuildOptions, build
from gobby.storage.build_profiles import BuildProfileLoader, BuildProfileManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def _options(**overrides: Any) -> BuildOptions:
    base = BuildOptions(
        quick=False,
        skip_stages=[],
        isolation="none",
        isolation_explicit=True,
        no_merge=False,
        pr=None,
        target_branch=None,
        assigned_agent="backend-developer",
    )
    return replace(base, **overrides)


async def test_quick_runs_one_step_and_disables_automation(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    task_manager = LocalTaskManager(temp_db)
    epic = task_manager.create_task(
        project_id=sample_project["id"],
        title="Quick build epic",
        category="planning",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
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
        validation_criteria="Test task completion is observable.",
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
        "epic_qa",
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
        validation_criteria="Test task completion is observable.",
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
        _options(profile="submit"),
        db=temp_db,
        project_id=sample_project["id"],
    )

    row = temp_db.fetchone(
        """
        SELECT delivery_mode, source_repo, target_repo
        FROM task_delivery_campaigns
        WHERE task_id = %s
        """,
        (task.id,),
    )
    assert row is not None
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
        _options(profile="submit"),
        db=temp_db,
        project_id=sample_project["id"],
    )

    row = temp_db.fetchone(
        """
        SELECT delivery_mode, source_repo, target_repo
        FROM task_delivery_campaigns
        WHERE task_id = %s
        """,
        (task.id,),
    )
    assert row is not None
    assert row["delivery_mode"] == "pull_request"
    assert row["source_repo"] == "test/test-project"
    assert row["target_repo"] == "upstream/test-project"


def test_explicit_delivery_options_override_profile_defaults(
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

    resolved = resolve_build_profile_options(
        _options(
            profile="submit",
            delivery_mode="auto",
            delivery_mode_explicit=True,
            delivery_target_repo=None,
            delivery_target_repo_explicit=True,
        ),
        db=temp_db,
        project_id=sample_project["id"],
    )

    assert resolved.delivery_mode == "auto"
    assert resolved.delivery_mode_explicit is True
    assert resolved.delivery_target_repo is None
    assert resolved.delivery_target_repo_explicit is True


def test_plan_enhancement_rounds_inherits_profile_default_when_not_explicit(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    BuildProfileLoader().sync(temp_db)
    BuildProfileManager(temp_db).create(
        name="enhance",
        display_label="Enhance",
        description="Profile carrying a non-zero plan-enhancement default.",
        skip_stages=[],
        isolation="worktree",
        unattended=False,
        plan_enhancement_rounds=3,
        source="project",
        project_id=sample_project["id"],
    )

    resolved = resolve_build_profile_options(
        _options(profile="enhance"),
        db=temp_db,
        project_id=sample_project["id"],
    )

    assert resolved.plan_enhancement_rounds == 3
    assert resolved.plan_enhancement_rounds_explicit is False


def test_explicit_zero_plan_enhancement_rounds_overrides_profile_default(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    BuildProfileLoader().sync(temp_db)
    BuildProfileManager(temp_db).create(
        name="enhance",
        display_label="Enhance",
        description="Profile carrying a non-zero plan-enhancement default.",
        skip_stages=[],
        isolation="worktree",
        unattended=False,
        plan_enhancement_rounds=3,
        source="project",
        project_id=sample_project["id"],
    )

    # Explicit 0 must win over the profile default of 3. A truthiness-based
    # overlay would have wrongly kept 3; the _explicit marker keeps it at 0.
    resolved = resolve_build_profile_options(
        _options(
            profile="enhance",
            plan_enhancement_rounds=0,
            plan_enhancement_rounds_explicit=True,
        ),
        db=temp_db,
        project_id=sample_project["id"],
    )

    assert resolved.plan_enhancement_rounds == 0
    assert resolved.plan_enhancement_rounds_explicit is True

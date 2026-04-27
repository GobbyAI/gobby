"""Red tests for the Phase 3 shared build service contract."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager

if TYPE_CHECKING:
    from gobby.build.service import BuildOptions, BuildResult

pytestmark = pytest.mark.unit


async def _build(input_ref: str, opts: object, db: object, project_id: str) -> BuildResult:
    from gobby.build.service import build

    return await build(input_ref, opts, db=db, project_id=project_id)


def _options(**overrides: object) -> BuildOptions:
    from gobby.build.service import BuildOptions

    values = {
        "profile": "auto",
        "skip_stages": [],
        "isolation": "worktree",
        "yolo": False,
        "max_review_rounds": 3,
        "target_branch": None,
        "assigned_agent": None,
    }
    values.update(overrides)
    return BuildOptions(**values)


def _project(temp_db, tmp_path: Path) -> tuple[str, Path]:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    project = LocalProjectManager(temp_db).create(name="phase-3", repo_path=str(repo_path))
    return project.id, repo_path


@pytest.mark.asyncio
async def test_build_rejects_unknown_skip_stage_with_valid_values(
    temp_db,
    tmp_path: Path,
) -> None:
    project_id, _repo_path = _project(temp_db, tmp_path)

    with pytest.raises(ValueError, match="skip.*dev.*plan_review.*test_arch.*pr"):
        await _build(
            "#1",
            _options(skip_stages=["dev"]),
            db=temp_db,
            project_id=project_id,
        )


@pytest.mark.asyncio
async def test_build_rejects_quick_profile_for_plan_file_input(
    temp_db,
    tmp_path: Path,
) -> None:
    project_id, _repo_path = _project(temp_db, tmp_path)
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n")

    with pytest.raises(ValueError, match="quick profile.*plan files.*review or full"):
        await _build(
            str(plan_file),
            _options(profile="quick", isolation="none"),
            db=temp_db,
            project_id=project_id,
        )


@pytest.mark.asyncio
async def test_build_rejects_non_none_isolation_for_single_leaf(
    temp_db,
    sample_project,
) -> None:
    leaf = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Single automated leaf",
        category="code",
        task_type="task",
    )

    with pytest.raises(ValueError, match="isolation requires an epic.*leaf.*none"):
        await _build(
            f"#{leaf.seq_num}",
            _options(isolation="worktree"),
            db=temp_db,
            project_id=sample_project["id"],
        )


@pytest.mark.asyncio
async def test_build_rejects_isolation_change_on_epic_with_existing_artifact(
    temp_db,
    sample_project,
) -> None:
    from gobby.storage.tasks import TaskArtifactManager

    task_manager = LocalTaskManager(temp_db)
    epic = task_manager.create_task(
        project_id=sample_project["id"],
        title="Built epic",
        category="planning",
        task_type="epic",
    )
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        epic.id,
        worktree_path="/tmp/gobby-worktree",
        worktree_id="worktree-row-1",
        base_commit_sha="abc123",
    )

    with pytest.raises(ValueError, match="already has worktree artifact.*/tmp/gobby-worktree"):
        await _build(
            f"#{epic.seq_num}",
            _options(isolation="clone"),
            db=temp_db,
            project_id=sample_project["id"],
        )


@pytest.mark.asyncio
async def test_build_validates_target_branch_exists(
    temp_db,
    tmp_path: Path,
) -> None:
    project_id, repo_path = _project(temp_db, tmp_path)
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n")

    import subprocess

    subprocess.run(["git", "init", "-b", "main"], cwd=repo_path, check=True, capture_output=True)

    with pytest.raises(ValueError, match="target branch.*missing.*main"):
        await _build(
            str(plan_file),
            _options(target_branch="missing"),
            db=temp_db,
            project_id=project_id,
        )


@pytest.mark.asyncio
async def test_build_validates_clones_dir_when_clone_isolation(
    temp_db,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, _repo_path = _project(temp_db, tmp_path)
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n")
    clones_dir = tmp_path / "clones"
    clones_dir.mkdir()
    monkeypatch.setattr("gobby.build.service.os.access", lambda _path, _mode: False)

    with pytest.raises(ValueError, match="clones_dir.*writable"):
        await _build(
            str(plan_file),
            _options(isolation="clone", clones_dir=clones_dir),
            db=temp_db,
            project_id=project_id,
        )


@pytest.mark.asyncio
async def test_build_plan_file_creates_planning_epic_artifacts_labels_and_kicks_tick(
    temp_db,
    tmp_path: Path,
) -> None:
    project_id, _repo_path = _project(temp_db, tmp_path)
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n")

    result = await _build(
        str(plan_file),
        _options(skip_stages=["plan_review", "pr"], isolation="worktree", target_branch="main"),
        db=temp_db,
        project_id=project_id,
    )

    task_manager = LocalTaskManager(temp_db)
    task = task_manager.get_task(result.task_id)
    artifacts = task_manager.artifacts.get_artifacts(task.id)
    events = task_manager.lifecycle_events.list_events(task.id)

    assert result.created is True
    assert result.initial_lifecycle == "test_arch"
    assert result.applied_stages_skipped == ["plan_review", "pr"]
    assert result.tick_dispatched >= 0
    assert task.task_type == "epic"
    assert task.category == "planning"
    assert task.allow_automation is True
    assert task.lifecycle == "test_arch"
    assert task.isolation == "worktree"
    assert set(task.labels) >= {"stage-:plan_review", "stage-:pr"}
    assert artifacts.plan_file_path == str(plan_file)
    assert artifacts.target_branch == "main"
    assert events[-1].reason == "gobby build"


@pytest.mark.asyncio
async def test_build_leaf_forces_none_isolation_and_sets_agent_and_lifecycle(
    temp_db,
    sample_project,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    leaf = task_manager.create_task(
        project_id=sample_project["id"],
        title="Solo leaf",
        category="test",
        task_type="task",
    )

    result = await _build(
        f"#{leaf.seq_num}",
        _options(profile="quick", isolation="none", assigned_agent="backend-developer"),
        db=temp_db,
        project_id=sample_project["id"],
    )

    updated = task_manager.get_task(leaf.id)
    assert result.created is False
    assert result.initial_lifecycle == "in_development"
    assert updated.allow_automation is True
    assert updated.lifecycle == "in_development"
    assert updated.isolation == "none"
    assert updated.assigned_agent == "backend-developer"


@pytest.mark.asyncio
async def test_build_leaf_rejects_non_automated_category(
    temp_db,
    sample_project,
) -> None:
    leaf = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Planning is not an automated leaf category",
        category="planning",
        task_type="task",
    )

    with pytest.raises(ValueError, match="category.*planning.*code.*config.*docs.*test"):
        await _build(
            f"#{leaf.seq_num}",
            _options(profile="quick", isolation="none"),
            db=temp_db,
            project_id=sample_project["id"],
        )

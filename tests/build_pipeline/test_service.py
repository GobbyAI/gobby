"""Red tests for the Phase 3 shared build service contract."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from gobby.storage.database import LocalDatabase
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
        "unattended": False,
        "composer_yolo": False,
        "target_branch": None,
        "assigned_agent": None,
    }
    values.update(overrides)
    return BuildOptions(**values)


def _project(temp_db: LocalDatabase, tmp_path: Path) -> tuple[str, Path]:
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

    with pytest.raises(ValueError, match="skip.*dev.*plan_review.*pr.*test_arch"):
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
async def test_build_plan_file_creates_planning_epic_artifacts_manifest_and_kicks_tick(
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
    assert result.initial_lifecycle == "ideation"
    assert result.applied_stages_skipped == ["planning", "pr"]
    assert result.tick_dispatched >= 0
    assert task.task_type == "epic"
    assert task.category == "planning"
    assert task.allow_automation is True
    assert task.isolation == "worktree"
    assert not any(label.startswith("stage-:") for label in task.labels or [])
    assert artifacts.plan_file_path == str(plan_file)
    assert artifacts.target_branch == "main"
    assert events[-1].reason == "gobby build"
    assert [row.stage_name for row in task_manager.stage_states.list_for_task(task.id)] == [
        "ideation",
        "research",
        "architecture",
        "prd",
        "test_arch",
        "expansion",
        "development",
        "holistic_qa",
        "merge",
    ]


@pytest.mark.asyncio
async def test_build_persists_stage_caps_on_manifest_rows(temp_db, tmp_path: Path) -> None:
    from gobby.config.build import StageCapOverride

    project_id, _repo_path = _project(temp_db, tmp_path)
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n")

    result = await _build(
        str(plan_file),
        _options(
            stage_caps=[
                StageCapOverride("expansion", max_work_attempts=4),
                StageCapOverride("development", max_review_rounds=6),
                StageCapOverride("merge", max_work_attempts=2),
                StageCapOverride("holistic_qa", max_review_rounds=5),
                StageCapOverride("pr", max_review_rounds=7),
            ],
        ),
        db=temp_db,
        project_id=project_id,
    )

    rows = {
        row.stage_name: row
        for row in LocalTaskManager(temp_db).stage_states.list_for_task(result.task_id)
    }
    assert rows["expansion"].max_work_attempts == 4
    assert rows["development"].max_review_rounds == 6
    assert rows["merge"].max_work_attempts == 2
    assert rows["holistic_qa"].max_review_rounds == 5
    assert rows["pr"].max_review_rounds == 7
    assert result.stage_manifest is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage_name", "cap_name"),
    [
        ("development", "max_review_rounds"),
        ("expansion", "max_work_attempts"),
    ],
)
async def test_build_rejects_stage_caps_below_one(
    temp_db,
    tmp_path: Path,
    stage_name: str,
    cap_name: str,
) -> None:
    from gobby.config.build import StageCapOverride

    project_id, _repo_path = _project(temp_db, tmp_path)
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n")
    override = (
        StageCapOverride(stage_name, max_work_attempts=0)
        if cap_name == "max_work_attempts"
        else StageCapOverride(stage_name, max_review_rounds=0)
    )

    with pytest.raises(ValueError, match=f"{cap_name}.*greater than or equal to 1"):
        await _build(
            str(plan_file),
            _options(stage_caps=[override]),
            db=temp_db,
            project_id=project_id,
        )


@pytest.mark.asyncio
async def test_build_leaf_forces_none_isolation_and_sets_agent(
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
    assert result.initial_lifecycle == "development"
    assert updated.allow_automation is True
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


@pytest.mark.asyncio
async def test_build_task_ref_requires_reset_for_existing_expansion_output(
    temp_db,
    sample_project,
) -> None:
    from gobby.storage.expansion_runs import LocalExpansionRunManager

    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(
        project_id=sample_project["id"],
        title="Expanded epic",
        category="planning",
        task_type="epic",
    )
    child = task_manager.create_task(
        project_id=sample_project["id"],
        title="Generated child",
        parent_task_id=parent.id,
        category="code",
        task_type="task",
    )
    run = LocalExpansionRunManager(temp_db).create(
        parent_task_id=parent.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="task",
    )
    LocalExpansionRunManager(temp_db).save_apply_result(
        run.id,
        task_id_map={"child": child.id},
        created_task_ids=[child.id],
    )

    with pytest.raises(ValueError, match="reset-expansion-output"):
        await _build(
            f"#{parent.seq_num}",
            _options(isolation="none"),
            db=temp_db,
            project_id=sample_project["id"],
        )

    result = await _build(
        f"#{parent.seq_num}",
        _options(isolation="none", reset_expansion_output=True),
        db=temp_db,
        project_id=sample_project["id"],
    )

    assert result.task_id == parent.id
    assert temp_db.fetchone("SELECT 1 FROM tasks WHERE id = ?", (child.id,)) is None

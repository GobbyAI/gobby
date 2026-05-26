"""Red tests for the Phase 3 shared build service contract."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from gobby.storage.hub.protocol import HubDatabase
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
        "quick": False,
        "skip_stages": [],
        "isolation": "worktree",
        "no_merge": False,
        "pr": None,
        "target_branch": None,
        "assigned_agent": None,
    }
    values.update(overrides)
    return BuildOptions(**values)


def _project(temp_db: HubDatabase, tmp_path: Path) -> tuple[str, Path]:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    project = LocalProjectManager(temp_db).create(name="phase-3", repo_path=str(repo_path))
    return project.id, repo_path


def _disable_dispatcher_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.build.service import DispatcherTickSummary

    async def no_tick(*_args: object, **_kwargs: object) -> DispatcherTickSummary:
        return DispatcherTickSummary()

    monkeypatch.setattr("gobby.build.lifecycle._kick_dispatcher_tick", no_tick)


def _table_counts(temp_db: HubDatabase, *tables: str) -> dict[str, int]:
    return {
        table: int(temp_db.fetchone(f"SELECT COUNT(*) AS count FROM {table}")["count"])
        for table in tables
    }


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "README.md").write_text("initial\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)


@pytest.mark.asyncio
async def test_build_coordinator_summary_survives_and_root_attaches_before_tick(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """Coordinator session is attached as run root and summary is visible before tick."""
    from gobby.build.service import DispatcherTickSummary
    from gobby.storage.build_history import BuildHistoryStorage
    from gobby.storage.sessions import SessionManager

    project_id, _repo_path = _project(temp_db, tmp_path)
    task = LocalTaskManager(temp_db).create_task(
        project_id=project_id,
        title="Coordinated build",
        task_type="epic",
    )
    coordinator = SessionManager(temp_db).register(
        external_id="coord-ext",
        machine_id="machine-1",
        source="codex",
        project_id=project_id,
        title="Coordinator",
    )
    seen: dict[str, object] = {}

    async def fake_tick(*args: object, **_kwargs: object) -> DispatcherTickSummary:
        db = args[0]
        tick_project_id = str(args[1])
        run = BuildHistoryStorage(db).latest_run_for_input(tick_project_id, f"#{task.seq_num}")
        seen["root_task_id"] = run.root_task_id if run else None
        seen["summary"] = run.summary if run else None
        return DispatcherTickSummary()

    monkeypatch.setattr("gobby.build.lifecycle._kick_dispatcher_tick", fake_tick)

    await _build(
        f"#{task.seq_num}",
        _options(isolation="none", coordinator_session_ref=f"#{coordinator.seq_num}"),
        db=temp_db,
        project_id=project_id,
    )
    run = BuildHistoryStorage(temp_db).latest_run_for_input(project_id, f"#{task.seq_num}")

    assert seen["root_task_id"] == task.id
    assert seen["summary"] == {
        "coordinator_session_id": coordinator.id,
        "isolation": "none",
        "quick": False,
    }
    assert run is not None
    assert run.root_task_id == task.id
    assert run.summary["coordinator_session_id"] == coordinator.id


@pytest.mark.asyncio
async def test_build_rejects_coordinator_from_another_project(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """Coordinator sessions from another project reject the build with ValueError."""
    from gobby.storage.sessions import SessionManager

    project_id, _repo_path = _project(temp_db, tmp_path)
    other_repo = tmp_path / "other"
    other_repo.mkdir()
    other_project = LocalProjectManager(temp_db).create(
        name="other-coordinator-project",
        repo_path=str(other_repo),
    )
    task = LocalTaskManager(temp_db).create_task(
        project_id=project_id,
        title="Coordinated build",
        task_type="epic",
    )
    coordinator = SessionManager(temp_db).register(
        external_id="other-coord-ext",
        machine_id="machine-1",
        source="codex",
        project_id=other_project.id,
        title="Other Coordinator",
    )

    with pytest.raises(ValueError, match="must belong to the build project"):
        await _build(
            f"#{task.seq_num}",
            _options(isolation="none", coordinator_session_ref=coordinator.id),
            db=temp_db,
            project_id=project_id,
        )


@pytest.mark.asyncio
async def test_build_rejects_unknown_skip_stage_with_valid_values(
    temp_db,
    tmp_path: Path,
) -> None:
    project_id, _repo_path = _project(temp_db, tmp_path)

    with pytest.raises(ValueError, match="skip.*dev.*plan_review.*pr"):
        await _build(
            "#1",
            _options(skip_stages=["dev"]),
            db=temp_db,
            project_id=project_id,
        )


@pytest.mark.asyncio
async def test_build_rejects_retired_test_arch_stage(
    temp_db,
    tmp_path: Path,
) -> None:
    from gobby.config.build import StageCapOverride

    project_id, _repo_path = _project(temp_db, tmp_path)
    task = LocalTaskManager(temp_db).create_task(
        project_id=project_id,
        title="Retired stage",
        task_type="epic",
        category="planning",
    )

    with pytest.raises(ValueError, match="invalid skip stage test_arch"):
        await _build(
            f"#{task.seq_num}",
            _options(skip_stages=["test_arch"]),
            db=temp_db,
            project_id=project_id,
        )

    with pytest.raises(ValueError, match="unknown stage: test_arch"):
        await _build(
            f"#{task.seq_num}",
            _options(isolation="none", stage_caps=[StageCapOverride(stage_name="test_arch")]),
            db=temp_db,
            project_id=project_id,
        )


@pytest.mark.asyncio
async def test_plan_file_quick_initializes_planning_pulse(
    temp_db,
    tmp_path: Path,
) -> None:
    project_id, _repo_path = _project(temp_db, tmp_path)
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n")

    result = await _build(
        str(plan_file),
        _options(quick=True, isolation="none"),
        db=temp_db,
        project_id=project_id,
    )

    assert result.initial_lifecycle == "planning"
    assert result.manifest is not None
    assert [row["stage_name"] for row in result.manifest] == ["planning"]


@pytest.mark.asyncio
async def test_approved_plan_file_seed_starts_at_expansion(
    temp_db,
    tmp_path: Path,
) -> None:
    project_id, _repo_path = _project(temp_db, tmp_path)
    plan_file = tmp_path / "approved-plan.md"
    plan_file.write_text("# Plan\n")

    result = await _build(
        str(plan_file),
        _options(quick=True, isolation="none", planning_seed_state="approved"),
        db=temp_db,
        project_id=project_id,
    )

    assert result.initial_lifecycle == "expansion"
    assert result.manifest is not None
    assert [row["stage_name"] for row in result.manifest] == ["expansion"]


@pytest.mark.asyncio
async def test_needs_review_plan_file_seed_sets_planning_review_round_count(
    temp_db,
    tmp_path: Path,
) -> None:
    project_id, _repo_path = _project(temp_db, tmp_path)
    plan_file = tmp_path / "needs-review-plan.md"
    plan_file.write_text("# Plan\n")

    result = await _build(
        str(plan_file),
        _options(
            quick=True,
            isolation="none",
            planning_seed_state="needs_review",
            completed_plan_review_rounds=2,
        ),
        db=temp_db,
        project_id=project_id,
    )

    task_manager = LocalTaskManager(temp_db)
    planning = task_manager.stage_states.get(result.task_id, "planning")

    assert result.initial_lifecycle == "planning"
    assert planning is not None
    assert planning.state == "needs_review"
    assert planning.review_round_count == 2


@pytest.mark.asyncio
async def test_build_accepts_isolation_for_single_leaf_and_merges_by_default(
    temp_db,
    sample_project,
) -> None:
    leaf = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Single automated leaf",
        category="code",
        task_type="task",
    )

    result = await _build(
        f"#{leaf.seq_num}",
        _options(isolation="worktree"),
        db=temp_db,
        project_id=sample_project["id"],
    )

    assert result.manifest is not None
    assert [row["stage_name"] for row in result.manifest] == ["development", "merge"]


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

    with pytest.raises(
        ValueError,
        match=r"already has a worktree artifact; clear existing build artifacts",
    ):
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
    monkeypatch.setattr("gobby.build.validation.os.access", lambda _path, _mode: False)

    with pytest.raises(ValueError, match="clones_dir.*writable"):
        await _build(
            str(plan_file),
            _options(isolation="clone", clones_dir=clones_dir),
            db=temp_db,
            project_id=project_id,
        )


@pytest.mark.asyncio
async def test_build_rejects_no_merge_without_isolation(
    temp_db,
    sample_project,
) -> None:
    leaf = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="No merge leaf",
        category="code",
        task_type="task",
    )

    with pytest.raises(ValueError, match="--no-merge requires"):
        await _build(
            f"#{leaf.seq_num}",
            _options(isolation="none", no_merge=True),
            db=temp_db,
            project_id=sample_project["id"],
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
        _options(skip_stages=["pr"], isolation="worktree", target_branch="main"),
        db=temp_db,
        project_id=project_id,
    )

    task_manager = LocalTaskManager(temp_db)
    task = task_manager.get_task(result.task_id)
    artifacts = task_manager.artifacts.get_artifacts(task.id)
    events = task_manager.lifecycle_events.list_events(task.id)

    assert result.created is True
    assert result.initial_lifecycle == "planning"
    assert result.applied_stages_skipped == ["pr"]
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
        "planning",
        "expansion",
        "development",
        "holistic_qa",
        "merge",
    ]


@pytest.mark.asyncio
async def test_build_plan_file_planning_spawn_uses_main_context_for_worktree_build(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    tmp_path: Path,
) -> None:
    from gobby.agents.sync import sync_bundled_agents
    from gobby.build.service import build
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

    project_id, _repo_path = _project(temp_db, tmp_path)
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n")
    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    spawn_kwargs: dict[str, object] = {}

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        spawn_kwargs.update(kwargs)
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=str(kwargs["task_id"]),
            run_id="run-build-planner",
        )
        return {"success": True, "run_id": run.id, "isolation": kwargs["isolation"]}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    result = await build(
        str(plan_file),
        _options(quick=True, isolation="worktree", target_branch="main"),
        db=temp_db,
        project_id=project_id,
        services=SimpleNamespace(
            database=temp_db,
            task_manager=task_manager,
            session_manager=session_manager,
            agent_runner=SimpleNamespace(),
        ),
    )
    task = task_manager.get_task(result.task_id)

    assert task.isolation == "worktree"
    assert spawn_kwargs["agent_lookup_name"] == "planner"
    assert spawn_kwargs["isolation"] == "none"
    assert spawn_kwargs["worktree_id"] is None
    assert spawn_kwargs["clone_id"] is None


@pytest.mark.asyncio
async def test_build_plan_file_dry_run_rolls_back_preview_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    tmp_path: Path,
) -> None:
    from gobby.config.build import StageCapOverride

    project_id, _repo_path = _project(temp_db, tmp_path)
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n")
    tables = (
        "tasks",
        "task_stage_states",
        "task_lifecycle_events",
        "build_runs",
        "build_history_events",
        "task_dispatch_mutex",
    )
    before = _table_counts(temp_db, *tables)

    async def fail_tick(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("dry-run must not call dispatcher tick")

    monkeypatch.setattr("gobby.build.lifecycle._kick_dispatcher_tick", fail_tick)

    result = await _build(
        str(plan_file),
        _options(
            dry_run=True,
            isolation="none",
            stage_caps=[StageCapOverride("planning", max_work_attempts=4)],
        ),
        db=temp_db,
        project_id=project_id,
    )
    second = await _build(
        str(plan_file),
        _options(
            dry_run=True,
            isolation="none",
            stage_caps=[StageCapOverride("planning", max_work_attempts=4)],
        ),
        db=temp_db,
        project_id=project_id,
    )

    assert _table_counts(temp_db, *tables) == before
    assert result.dry_run is True
    assert result.created is True
    assert result.task_id == "dry-run:plan-file"
    assert result.dispatcher_tick.reason == "dry_run"
    assert result.tick_dispatched == 0
    assert result.manifest is not None
    assert result.manifest == second.manifest
    assert [row["stage_name"] for row in result.manifest] == ["planning"]
    assert result.manifest[0]["max_work_attempts"] == 4


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
async def test_build_passes_active_agent_cap_separately_from_stage_work_cap(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    tmp_path: Path,
) -> None:
    from gobby.build.service import DispatcherTickSummary
    from gobby.config.build import StageCapOverride

    project_id, _repo_path = _project(temp_db, tmp_path)
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n")
    tick_kwargs: list[dict[str, object]] = []

    async def fake_tick(*_args: object, **kwargs: object) -> DispatcherTickSummary:
        tick_kwargs.append(dict(kwargs))
        return DispatcherTickSummary()

    monkeypatch.setattr("gobby.build.lifecycle._kick_dispatcher_tick", fake_tick)

    result = await _build(
        str(plan_file),
        _options(
            isolation="none",
            max_active_agents=4,
            stage_caps=[StageCapOverride("planning", max_work_attempts=6)],
        ),
        db=temp_db,
        project_id=project_id,
    )

    rows = LocalTaskManager(temp_db).stage_states.list_for_task(result.task_id)
    assert rows[0].max_work_attempts == 6
    assert tick_kwargs[0]["max_active_agents"] == 4


@pytest.mark.asyncio
async def test_explicit_build_tick_bypasses_paused_dispatcher_cron(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    tmp_path: Path,
) -> None:
    from gobby.build.service import DispatcherTickSummary, build_stop

    project_id, _repo_path = _project(temp_db, tmp_path)
    task = LocalTaskManager(temp_db).create_task(
        project_id=project_id,
        title="Docs leaf",
        category="docs",
        task_type="task",
    )
    tick_kwargs: list[dict[str, object]] = []

    async def fake_tick(*_args: object, **kwargs: object) -> DispatcherTickSummary:
        tick_kwargs.append(dict(kwargs))
        return DispatcherTickSummary(ticks=1)

    monkeypatch.setattr("gobby.build.lifecycle._kick_dispatcher_tick", fake_tick)
    build_stop(db=temp_db, project_id=project_id)

    result = await _build(
        str(task.seq_num),
        _options(isolation="none", quick=True),
        db=temp_db,
        project_id=project_id,
    )

    assert result.tick_dispatched == 1
    assert tick_kwargs[0]["dispatcher_enabled"] is True


@pytest.mark.asyncio
async def test_max_retries_zero_sets_one_attempt_per_resolved_stage(
    temp_db,
    tmp_path: Path,
) -> None:
    project_id, _repo_path = _project(temp_db, tmp_path)
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n")

    result = await _build(
        str(plan_file),
        _options(isolation="none", max_retries=0),
        db=temp_db,
        project_id=project_id,
    )

    rows = LocalTaskManager(temp_db).stage_states.list_for_task(result.task_id)
    assert rows
    assert {row.max_work_attempts for row in rows} == {1}
    assert {row.max_review_rounds for row in rows} == {1}


@pytest.mark.asyncio
async def test_stage_override_wins_over_max_retries_default(
    temp_db,
    tmp_path: Path,
) -> None:
    from gobby.config.build import StageCapOverride

    project_id, _repo_path = _project(temp_db, tmp_path)
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n")

    result = await _build(
        str(plan_file),
        _options(
            isolation="none",
            max_retries=0,
            stage_caps=[StageCapOverride("planning", max_work_attempts=4)],
        ),
        db=temp_db,
        project_id=project_id,
    )

    row = LocalTaskManager(temp_db).stage_states.list_for_task(result.task_id)[0]
    assert row.max_work_attempts == 4
    assert row.max_review_rounds == 1


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
async def test_build_rejects_negative_max_retries(
    temp_db,
    tmp_path: Path,
) -> None:
    project_id, _repo_path = _project(temp_db, tmp_path)
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n")

    with pytest.raises(ValueError, match="max_retries.*greater than or equal to 0"):
        await _build(
            str(plan_file),
            _options(max_retries=-1),
            db=temp_db,
            project_id=project_id,
        )


@pytest.mark.asyncio
async def test_build_leaf_uses_category_primary_stage_and_sets_agent(
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
        _options(isolation="none", assigned_agent="backend-developer"),
        db=temp_db,
        project_id=sample_project["id"],
    )

    updated = task_manager.get_task(leaf.id)
    rows = task_manager.stage_states.list_for_task(leaf.id)
    assert result.created is False
    assert result.initial_lifecycle == "development"
    assert [row.stage_name for row in rows] == ["development"]
    assert updated.allow_automation is True
    assert updated.isolation == "none"
    assert updated.assigned_agent == "backend-developer"


@pytest.mark.asyncio
async def test_build_existing_leaf_omitted_backend_defaults_to_worktree(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    _disable_dispatcher_tick(monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    leaf = task_manager.create_task(
        project_id=sample_project["id"],
        title="Docs leaf",
        category="docs",
        task_type="task",
    )
    task_manager.update_task(leaf.id, isolation="none")
    task_manager.initialize_task_manifest(leaf.id, stage_names=["development"])

    await _build(
        f"#{leaf.seq_num}",
        _options(quick=True, isolation="worktree", isolation_explicit=False),
        db=temp_db,
        project_id=sample_project["id"],
    )

    updated = task_manager.get_task(leaf.id)
    assert updated.isolation == "worktree"
    assert [row.stage_name for row in task_manager.stage_states.list_for_task(leaf.id)] == [
        "development"
    ]


@pytest.mark.asyncio
async def test_build_existing_leaf_explicit_isolation_overrides_task_isolation(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    _disable_dispatcher_tick(monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    leaf = task_manager.create_task(
        project_id=sample_project["id"],
        title="Explicit isolation leaf",
        category="code",
        task_type="task",
    )
    task_manager.update_task(leaf.id, isolation="none")
    task_manager.initialize_task_manifest(leaf.id, stage_names=["development", "merge"])

    await _build(
        f"#{leaf.seq_num}",
        _options(isolation="worktree", isolation_explicit=True),
        db=temp_db,
        project_id=sample_project["id"],
    )

    updated = task_manager.get_task(leaf.id)
    assert updated.isolation == "worktree"


@pytest.mark.asyncio
async def test_build_rerun_same_manifest_preserves_active_stage(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    _disable_dispatcher_tick(monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    leaf = task_manager.create_task(
        project_id=sample_project["id"],
        title="Same shape leaf",
        category="code",
        task_type="task",
    )
    await _build(
        f"#{leaf.seq_num}",
        _options(isolation="none"),
        db=temp_db,
        project_id=sample_project["id"],
    )
    started = task_manager.stage_states.start_stage(
        leaf.id,
        "development",
        by_session_id=None,
    )
    assert started.stage_name == "development"
    assert started.state == "in_progress"

    await _build(
        f"#{leaf.seq_num}",
        _options(isolation="none"),
        db=temp_db,
        project_id=sample_project["id"],
    )

    current = task_manager.stage_states.current_stage(leaf.id)
    assert current is not None
    assert current.stage_name == "development"
    assert current.state == "in_progress"
    assert current.entered_at == started.entered_at


@pytest.mark.asyncio
async def test_build_rejects_skip_stage_on_existing_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    _disable_dispatcher_tick(monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    leaf = task_manager.create_task(
        project_id=sample_project["id"],
        title="Different shape active leaf",
        category="code",
        task_type="task",
    )
    await _build(
        f"#{leaf.seq_num}",
        _options(isolation="none"),
        db=temp_db,
        project_id=sample_project["id"],
    )
    task_manager.stage_states.start_stage(leaf.id, "development", by_session_id=None)

    with pytest.raises(ValueError, match="--skip-stage can only shape a new lifecycle"):
        await _build(
            f"#{leaf.seq_num}",
            _options(isolation="none", skip_stages=["merge"]),
            db=temp_db,
            project_id=sample_project["id"],
        )


@pytest.mark.asyncio
async def test_build_existing_lifecycle_stage_caps_update_rows(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.config.build import StageCapOverride

    _disable_dispatcher_tick(monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    leaf = task_manager.create_task(
        project_id=sample_project["id"],
        title="Pristine reseed leaf",
        category="code",
        task_type="task",
    )
    task_manager.initialize_task_manifest(leaf.id, stage_names=["development", "pr", "merge"])

    await _build(
        f"#{leaf.seq_num}",
        _options(
            isolation="none",
            stage_caps=[StageCapOverride("development", max_review_rounds=4)],
        ),
        db=temp_db,
        project_id=sample_project["id"],
    )

    rows = task_manager.stage_states.list_for_task(leaf.id)
    assert [row.stage_name for row in rows] == ["development", "pr", "merge"]
    assert rows[0].max_review_rounds == 4


@pytest.mark.asyncio
async def test_build_task_ref_dry_run_rolls_back_existing_lifecycle_mutations(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.config.build import StageCapOverride

    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Existing lifecycle preview",
        category="code",
        task_type="task",
    )
    task_manager.initialize_task_manifest(task.id, stage_names=["development", "pr"])
    tables = (
        "tasks",
        "task_stage_states",
        "task_lifecycle_events",
        "build_runs",
        "build_history_events",
        "task_dispatch_mutex",
    )
    before_counts = _table_counts(temp_db, *tables)
    before_task = task_manager.get_task(task.id)
    before_rows = task_manager.stage_states.list_for_task(task.id)

    async def fail_tick(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("dry-run must not call dispatcher tick")

    monkeypatch.setattr("gobby.build.lifecycle._kick_dispatcher_tick", fail_tick)

    result = await _build(
        f"#{task.seq_num}",
        _options(
            dry_run=True,
            isolation="none",
            assigned_agent="backend-developer",
            stage_caps=[StageCapOverride("development", max_work_attempts=7)],
            max_retries=1,
        ),
        db=temp_db,
        project_id=sample_project["id"],
    )

    after_task = task_manager.get_task(task.id)
    after_rows = task_manager.stage_states.list_for_task(task.id)

    assert _table_counts(temp_db, *tables) == before_counts
    assert result.dry_run is True
    assert result.created is False
    assert result.task_id == task.id
    assert result.dispatcher_tick.reason == "dry_run"
    assert result.manifest is not None
    manifest_by_stage = {row["stage_name"]: row for row in result.manifest}
    assert manifest_by_stage["development"]["max_work_attempts"] == 7
    assert manifest_by_stage["pr"]["max_work_attempts"] == 2
    assert after_task.allow_automation == before_task.allow_automation
    assert after_task.assigned_agent == before_task.assigned_agent
    assert [
        (row.stage_name, row.max_work_attempts, row.max_review_rounds) for row in after_rows
    ] == [(row.stage_name, row.max_work_attempts, row.max_review_rounds) for row in before_rows]


@pytest.mark.asyncio
async def test_build_epic_cascade_initializes_child_from_resolved_scope(
    temp_db,
    sample_project,
) -> None:
    from gobby.config.build import StageCapOverride

    task_manager = LocalTaskManager(temp_db)
    epic = task_manager.create_task(
        project_id=sample_project["id"],
        title="Scoped build epic",
        category="planning",
        task_type="epic",
    )
    child = task_manager.create_task(
        project_id=sample_project["id"],
        title="Scoped child",
        parent_task_id=epic.id,
        category="code",
        task_type="task",
    )

    await _build(
        f"#{epic.seq_num}",
        _options(
            isolation="none",
            stage_caps=[StageCapOverride("development", max_review_rounds=8)],
        ),
        db=temp_db,
        project_id=sample_project["id"],
    )

    child_rows = task_manager.stage_states.list_for_task(child.id)
    assert [row.stage_name for row in child_rows] == ["development"]
    assert child_rows[0].max_review_rounds == 8


@pytest.mark.asyncio
async def test_build_epic_creates_integration_worktrees_and_targets_nearest_branch(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    tmp_path: Path,
) -> None:
    _disable_dispatcher_tick(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project_id, repo_path = _project(temp_db, tmp_path)
    _init_git_repo(repo_path)
    task_manager = LocalTaskManager(temp_db)
    root = task_manager.create_task(
        project_id=project_id,
        title="Root Integration",
        category="planning",
        task_type="epic",
    )
    child_epic = task_manager.create_task(
        project_id=project_id,
        title="Child Integration",
        parent_task_id=root.id,
        category="planning",
        task_type="epic",
    )
    leaf = task_manager.create_task(
        project_id=project_id,
        title="Leaf Work",
        parent_task_id=child_epic.id,
        category="code",
        task_type="task",
    )

    await _build(
        f"#{root.seq_num}",
        _options(isolation="worktree", target_branch="main"),
        db=temp_db,
        project_id=project_id,
    )

    root_artifacts = task_manager.artifacts.get_artifacts(root.id)
    child_artifacts = task_manager.artifacts.get_artifacts(child_epic.id)
    leaf_artifacts = task_manager.artifacts.get_artifacts(leaf.id)
    root_worktree = temp_db.fetchone(
        "SELECT * FROM worktrees WHERE id = ?",
        (root_artifacts.integration_workspace_id,),
    )
    child_worktree = temp_db.fetchone(
        "SELECT * FROM worktrees WHERE id = ?",
        (child_artifacts.integration_workspace_id,),
    )

    assert root_artifacts.integration_branch is not None
    assert root_artifacts.target_branch == "main"
    assert child_artifacts.integration_branch is not None
    assert child_artifacts.target_branch == root_artifacts.integration_branch
    assert leaf_artifacts.target_branch == child_artifacts.integration_branch
    assert root_worktree["workspace_role"] == "integration"
    assert root_worktree["base_branch"] == "main"
    assert child_worktree["workspace_role"] == "integration"
    assert child_worktree["base_branch"] == root_artifacts.integration_branch


@pytest.mark.asyncio
async def test_epic_no_merge_skips_only_root_promotion(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    tmp_path: Path,
) -> None:
    _disable_dispatcher_tick(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project_id, repo_path = _project(temp_db, tmp_path)
    _init_git_repo(repo_path)
    task_manager = LocalTaskManager(temp_db)
    root = task_manager.create_task(
        project_id=project_id,
        title="No merge root",
        category="planning",
        task_type="epic",
    )
    child = task_manager.create_task(
        project_id=project_id,
        title="Child keeps merge",
        parent_task_id=root.id,
        category="code",
        task_type="task",
    )

    await _build(
        f"#{root.seq_num}",
        _options(isolation="worktree", no_merge=True, target_branch="main"),
        db=temp_db,
        project_id=project_id,
    )

    root_stages = [row.stage_name for row in task_manager.stage_states.list_for_task(root.id)]
    child_stages = [row.stage_name for row in task_manager.stage_states.list_for_task(child.id)]
    assert "merge" not in root_stages
    assert "merge" in child_stages


@pytest.mark.asyncio
async def test_existing_epic_cascade_forces_child_merge_with_legacy_root_manifest(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    tmp_path: Path,
) -> None:
    _disable_dispatcher_tick(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project_id, repo_path = _project(temp_db, tmp_path)
    _init_git_repo(repo_path)
    task_manager = LocalTaskManager(temp_db)
    root = task_manager.create_task(
        project_id=project_id,
        title="Legacy Integration Root",
        category="planning",
        task_type="epic",
    )
    child = task_manager.create_task(
        project_id=project_id,
        title="Legacy Child",
        parent_task_id=root.id,
        category="docs",
        task_type="task",
    )
    task_manager.initialize_task_manifest(root.id, stage_names=["development"])

    await _build(
        f"#{root.seq_num}",
        _options(isolation="worktree", target_branch="main"),
        db=temp_db,
        project_id=project_id,
    )

    child_stages = [row.stage_name for row in task_manager.stage_states.list_for_task(child.id)]
    assert child_stages == ["development", "merge"]


@pytest.mark.asyncio
async def test_build_epic_cascade_preserves_active_child_with_cap_drift(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    tmp_path: Path,
) -> None:
    from gobby.storage.tasks._stage_types import StageManifestSpec

    _disable_dispatcher_tick(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project_id, repo_path = _project(temp_db, tmp_path)
    _init_git_repo(repo_path)
    task_manager = LocalTaskManager(temp_db)
    root = task_manager.create_task(
        project_id=project_id,
        title="Root with reopened child",
        category="planning",
        task_type="epic",
    )
    child = task_manager.create_task(
        project_id=project_id,
        title="Reopened child",
        parent_task_id=root.id,
        category="code",
        task_type="feature",
    )
    task_manager.stage_states.initialize_manifest(
        child.id,
        [
            StageManifestSpec("development", 0),
            StageManifestSpec("merge", 1, max_work_attempts=12),
        ],
        by_session_id=None,
    )
    task_manager.stage_states.start_stage(child.id, "development", by_session_id="dispatcher")

    await _build(
        f"#{root.seq_num}",
        _options(isolation="worktree", target_branch="main"),
        db=temp_db,
        project_id=project_id,
    )

    child_rows = task_manager.stage_states.list_for_task(child.id)
    assert [(row.stage_name, row.position) for row in child_rows] == [
        ("development", 0),
        ("pr", 1),
        ("merge", 2),
    ]
    assert child_rows[0].state == "in_progress"
    assert child_rows[0].entered_by_session_id == "dispatcher"
    assert child_rows[2].max_work_attempts is None


@pytest.mark.asyncio
async def test_build_epic_cascade_skips_closed_descendants_with_existing_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.config.build import StageCapOverride

    _disable_dispatcher_tick(monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    epic = task_manager.create_task(
        project_id=sample_project["id"],
        title="Mixed epic",
        category="planning",
        task_type="epic",
    )
    closed_child = task_manager.create_task(
        project_id=sample_project["id"],
        title="Already closed docs child",
        parent_task_id=epic.id,
        category="docs",
        task_type="task",
    )
    open_child = task_manager.create_task(
        project_id=sample_project["id"],
        title="Open docs child",
        parent_task_id=epic.id,
        category="docs",
        task_type="task",
    )
    task_manager.update_task(closed_child.id, isolation="worktree")
    task_manager.initialize_task_manifest(
        closed_child.id,
        stage_names=["development", "pr", "merge"],
    )
    task_manager.close_task(closed_child.id, reason="completed", force=True)

    await _build(
        f"#{epic.seq_num}",
        _options(
            quick=True,
            isolation="none",
            stage_caps=[StageCapOverride("development", max_work_attempts=6)],
        ),
        db=temp_db,
        project_id=sample_project["id"],
    )

    closed_after = task_manager.get_task(closed_child.id)
    closed_rows = task_manager.stage_states.list_for_task(closed_child.id)
    open_rows = task_manager.stage_states.list_for_task(open_child.id)

    assert closed_after.allow_automation is False
    assert closed_after.isolation == "worktree"
    assert [row.stage_name for row in closed_rows] == ["development", "pr", "merge"]
    assert [row.stage_name for row in open_rows] == ["development"]
    assert open_rows[0].max_work_attempts == 6


@pytest.mark.asyncio
async def test_build_epic_cascade_skips_busy_descendant_manifest_initialization(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.config.build import StageCapOverride
    from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager

    _disable_dispatcher_tick(monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    epic = task_manager.create_task(
        project_id=sample_project["id"],
        title="Busy epic",
        category="planning",
        task_type="epic",
    )
    busy_child = task_manager.create_task(
        project_id=sample_project["id"],
        title="Busy docs child",
        parent_task_id=epic.id,
        category="docs",
        task_type="task",
    )
    open_child = task_manager.create_task(
        project_id=sample_project["id"],
        title="Open docs child",
        parent_task_id=epic.id,
        category="docs",
        task_type="task",
    )
    task_manager.initialize_task_manifest(
        busy_child.id,
        stage_names=["development", "pr", "merge"],
    )
    assert TaskDispatchMutexManager(temp_db).acquire_mutex(
        busy_child.id,
        holder="agent-run",
        kind="development",
        ttl_seconds=3600,
        run_id="run-busy",
    )

    await _build(
        f"#{epic.seq_num}",
        _options(
            quick=True,
            isolation="none",
            stage_caps=[StageCapOverride("development", max_work_attempts=6)],
        ),
        db=temp_db,
        project_id=sample_project["id"],
    )

    busy_rows = task_manager.stage_states.list_for_task(busy_child.id)
    open_rows = task_manager.stage_states.list_for_task(open_child.id)

    assert [row.stage_name for row in busy_rows] == ["development", "pr", "merge"]
    assert [row.stage_name for row in open_rows] == ["development"]
    assert open_rows[0].max_work_attempts == 6


@pytest.mark.asyncio
async def test_build_leaf_with_services_creates_agent_run_by_completion(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from types import SimpleNamespace

    from gobby.agents.sync import sync_bundled_agents
    from gobby.build.service import build
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    leaf = task_manager.create_task(
        project_id=sample_project["id"],
        title="Solo leaf with dispatch",
        category="code",
        task_type="task",
    )

    async def fake_spawn_agent_impl(**kwargs):
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=kwargs["parent_session_id"],
            provider="codex",
            prompt=kwargs["prompt"],
            agent_name=kwargs["agent_lookup_name"],
            task_id=leaf.id,
            run_id="run-build-leaf",
        )
        return {"success": True, "run_id": run.id, "isolation": "none"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
    )

    result = await build(
        f"#{leaf.seq_num}",
        _options(quick=True, isolation="none", assigned_agent="backend-developer"),
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )

    run = LocalAgentRunManager(temp_db).get("run-build-leaf")
    assert run is not None
    assert run.agent_name == "backend-developer"
    assert run.task_id == leaf.id
    assert result.dispatcher_tick.ticks >= 2
    assert result.dispatcher_tick.executed >= 2


@pytest.mark.asyncio
async def test_build_leaf_rejects_non_automated_category(
    temp_db,
    sample_project,
) -> None:
    leaf = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Manual is not an automated leaf category",
        category="manual",
        task_type="task",
    )

    with pytest.raises(ValueError, match="category manual cannot be automated"):
        await _build(
            f"#{leaf.seq_num}",
            _options(isolation="none"),
            db=temp_db,
            project_id=sample_project["id"],
        )


@pytest.mark.asyncio
async def test_build_task_ref_automates_existing_expansion_output(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.storage.expansion_runs import LocalExpansionRunManager

    _disable_dispatcher_tick(monkeypatch)
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

    result = await _build(
        f"#{parent.seq_num}",
        _options(isolation="none"),
        db=temp_db,
        project_id=sample_project["id"],
    )

    assert result.task_id == parent.id
    assert result.initial_lifecycle == "development"
    assert result.manifest is not None
    assert [row["stage_name"] for row in result.manifest] == [
        "development",
        "holistic_qa",
        "pr",
        "merge",
    ]
    assert temp_db.fetchone("SELECT 1 FROM tasks WHERE id = ?", (child.id,)) is not None
    parent_rows = task_manager.stage_states.list_for_task(parent.id)
    child_rows = task_manager.stage_states.list_for_task(child.id)
    assert [row.stage_name for row in parent_rows] == [
        "development",
        "holistic_qa",
        "pr",
        "merge",
    ]
    assert [row.stage_name for row in child_rows] == ["development", "pr", "merge"]


@pytest.mark.asyncio
async def test_build_task_ref_repairs_legacy_expanded_epic_manifest_without_pr(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.storage.expansion_runs import LocalExpansionRunManager
    from gobby.storage.tasks import StageManifestSpec

    _disable_dispatcher_tick(monkeypatch)
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
    task_manager.stage_states.initialize_manifest(
        parent.id,
        [
            StageManifestSpec(stage_name=name, position=position)
            for position, name in enumerate(
                [
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
            )
        ],
        by_session_id=None,
    )
    for stage_name in ("ideation", "research", "architecture"):
        task_manager.stage_states.start_stage(parent.id, stage_name, by_session_id=None)
        task_manager.stage_states.complete_stage(parent.id, stage_name, by_session_id=None)
    task_manager.stage_states.start_stage(parent.id, "prd", by_session_id=None)
    task_manager.stage_states.initialize_manifest(
        child.id,
        [
            StageManifestSpec(stage_name="development", position=0),
            StageManifestSpec(stage_name="pr", position=1),
            StageManifestSpec(stage_name="merge", position=2),
        ],
        by_session_id=None,
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

    result = await _build(
        f"#{parent.seq_num}",
        _options(isolation="none", skip_stages=["pr"], skip_stages_explicit=True),
        db=temp_db,
        project_id=sample_project["id"],
    )

    assert result.initial_lifecycle == "development"
    assert result.applied_stages_skipped == ["pr"]
    assert result.manifest is not None
    assert [row["stage_name"] for row in result.manifest] == [
        "development",
        "holistic_qa",
        "merge",
    ]
    parent_rows = task_manager.stage_states.list_for_task(parent.id)
    child_rows = task_manager.stage_states.list_for_task(child.id)
    assert [row.stage_name for row in parent_rows] == ["development", "holistic_qa", "merge"]
    assert [row.stage_name for row in child_rows] == ["development", "merge"]


@pytest.mark.asyncio
async def test_build_task_ref_removes_skipped_pr_from_progressed_child_epic(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.storage.expansion_runs import LocalExpansionRunManager
    from gobby.storage.tasks import StageManifestSpec

    _disable_dispatcher_tick(monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(
        project_id=sample_project["id"],
        title="Expanded epic",
        category="planning",
        task_type="epic",
    )
    child = task_manager.create_task(
        project_id=sample_project["id"],
        title="Generated child epic",
        parent_task_id=parent.id,
        category="planning",
        task_type="epic",
    )
    manifest = [
        StageManifestSpec(stage_name="development", position=0),
        StageManifestSpec(stage_name="holistic_qa", position=1),
        StageManifestSpec(stage_name="pr", position=2),
        StageManifestSpec(stage_name="merge", position=3),
    ]
    task_manager.stage_states.initialize_manifest(parent.id, manifest, by_session_id=None)
    task_manager.stage_states.initialize_manifest(child.id, manifest, by_session_id=None)
    temp_db.execute(
        """
        UPDATE task_stage_states
           SET state = 'done',
               entered_at = '2026-05-22T19:00:00+00:00',
               completed_at = '2026-05-22T19:01:00+00:00',
               updated_at = '2026-05-22T19:01:00+00:00'
         WHERE task_id = ? AND stage_name IN ('development', 'holistic_qa')
        """,
        (child.id,),
    )
    temp_db.execute(
        """
        INSERT INTO sessions (id, external_id, machine_id, source, project_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("reviewer-session", "reviewer-session", "machine-1", "test", sample_project["id"]),
    )
    temp_db.execute(
        """
        UPDATE tasks
           SET assignee = ?, claimed_by_session_id = ?
         WHERE id = ?
        """,
        ("reviewer-session", "reviewer-session", child.id),
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

    result = await _build(
        f"#{parent.seq_num}",
        _options(isolation="none", skip_stages=["pr"], skip_stages_explicit=True),
        db=temp_db,
        project_id=sample_project["id"],
    )

    assert result.applied_stages_skipped == ["pr"]
    child_rows = task_manager.stage_states.list_for_task(child.id)
    assert [row.stage_name for row in child_rows] == ["development", "holistic_qa", "merge"]
    assert task_manager.stage_states.current_stage(child.id).stage_name == "merge"
    updated_child = task_manager.get_task(child.id)
    assert updated_child.claimed_by_session_id is None
    assert updated_child.assignee is None


@pytest.mark.asyncio
async def test_build_task_ref_removes_auto_started_skipped_pr_from_child_epic(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.storage.expansion_runs import LocalExpansionRunManager
    from gobby.storage.tasks import StageManifestSpec

    _disable_dispatcher_tick(monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(
        project_id=sample_project["id"],
        title="Expanded epic",
        category="planning",
        task_type="epic",
    )
    child = task_manager.create_task(
        project_id=sample_project["id"],
        title="Generated child epic",
        parent_task_id=parent.id,
        category="planning",
        task_type="epic",
    )
    manifest = [
        StageManifestSpec(stage_name="development", position=0),
        StageManifestSpec(stage_name="holistic_qa", position=1),
        StageManifestSpec(stage_name="pr", position=2),
        StageManifestSpec(stage_name="merge", position=3),
    ]
    task_manager.stage_states.initialize_manifest(parent.id, manifest, by_session_id=None)
    task_manager.stage_states.initialize_manifest(child.id, manifest, by_session_id=None)
    temp_db.execute(
        """
        UPDATE task_stage_states
           SET state = 'done',
               entered_at = '2026-05-22T19:00:00+00:00',
               completed_at = '2026-05-22T19:01:00+00:00',
               updated_at = '2026-05-22T19:01:00+00:00'
         WHERE task_id = ? AND stage_name IN ('development', 'holistic_qa')
        """,
        (child.id,),
    )
    temp_db.execute(
        """
        UPDATE task_stage_states
           SET state = 'in_progress',
               entered_at = '2026-05-22T19:02:00+00:00',
               entered_by_session_id = 'dispatcher',
               work_attempt_count = 1,
               updated_at = '2026-05-22T19:02:00+00:00'
         WHERE task_id = ? AND stage_name = 'pr'
        """,
        (child.id,),
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

    result = await _build(
        f"#{parent.seq_num}",
        _options(isolation="none", skip_stages=["pr"], skip_stages_explicit=True),
        db=temp_db,
        project_id=sample_project["id"],
    )

    assert result.applied_stages_skipped == ["pr"]
    child_rows = task_manager.stage_states.list_for_task(child.id)
    assert [row.stage_name for row in child_rows] == ["development", "holistic_qa", "merge"]
    assert task_manager.stage_states.current_stage(child.id).stage_name == "merge"


@pytest.mark.asyncio
async def test_build_resume_cascades_skipped_pr_before_workspace_refresh(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.storage.expansion_runs import LocalExpansionRunManager
    from gobby.storage.tasks import StageManifestSpec

    _disable_dispatcher_tick(monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(
        project_id=sample_project["id"],
        title="Expanded epic",
        category="planning",
        task_type="epic",
    )
    child = task_manager.create_task(
        project_id=sample_project["id"],
        title="Generated child epic",
        parent_task_id=parent.id,
        category="planning",
        task_type="epic",
    )
    manifest = [
        StageManifestSpec(stage_name="development", position=0),
        StageManifestSpec(stage_name="holistic_qa", position=1),
        StageManifestSpec(stage_name="pr", position=2),
        StageManifestSpec(stage_name="merge", position=3),
    ]
    task_manager.stage_states.initialize_manifest(parent.id, manifest, by_session_id=None)
    task_manager.stage_states.initialize_manifest(child.id, manifest, by_session_id=None)
    temp_db.execute(
        """
        UPDATE task_stage_states
           SET state = 'done',
               entered_at = '2026-05-22T19:00:00+00:00',
               completed_at = '2026-05-22T19:01:00+00:00',
               updated_at = '2026-05-22T19:01:00+00:00'
         WHERE task_id = ? AND stage_name IN ('development', 'holistic_qa')
        """,
        (child.id,),
    )
    temp_db.execute(
        """
        UPDATE task_stage_states
           SET state = 'in_progress',
               entered_at = '2026-05-22T19:02:00+00:00',
               entered_by_session_id = 'dispatcher',
               work_attempt_count = 1,
               updated_at = '2026-05-22T19:02:00+00:00'
         WHERE task_id = ? AND stage_name = 'pr'
        """,
        (child.id,),
    )
    task_manager.artifacts.set_artifacts_atomic(parent.id, target_branch="main")
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

    def fail_workspace_refresh(**_kwargs: object) -> None:
        raise RuntimeError("workspace refresh failed")

    monkeypatch.setattr(
        "gobby.build.lifecycle.ensure_epic_integration_workspaces",
        fail_workspace_refresh,
    )

    with pytest.raises(RuntimeError, match="workspace refresh failed"):
        await _build(
            f"#{parent.seq_num}",
            _options(isolation="worktree", skip_stages=["pr"], skip_stages_explicit=True),
            db=temp_db,
            project_id=sample_project["id"],
        )

    child_rows = task_manager.stage_states.list_for_task(child.id)
    assert [row.stage_name for row in child_rows] == ["development", "holistic_qa", "merge"]
    assert task_manager.stage_states.current_stage(child.id).stage_name == "merge"


@pytest.mark.asyncio
async def test_build_task_ref_can_reset_existing_expansion_output(
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

    result = await _build(
        f"#{parent.seq_num}",
        _options(isolation="none", reset_expansion_output=True),
        db=temp_db,
        project_id=sample_project["id"],
    )

    assert result.task_id == parent.id
    assert temp_db.fetchone("SELECT 1 FROM tasks WHERE id = ?", (child.id,)) is None

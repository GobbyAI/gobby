"""Unit coverage for build lifecycle resume decisions."""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from gobby.build.dispatch_tick import DispatcherTickSummary
from gobby.build.observability import get_build_status
from gobby.build.options import BuildOptions
from gobby.build.resume_lifecycle import (
    apply_stage_caps_to_existing_lifecycle,
    repair_expanded_epic_root_manifest_for_resume,
    resume_existing_lifecycle,
)
from gobby.build.runtime_hooks import RuntimeHooks
from gobby.build.workspace_git import _workspace_path
from gobby.config.build import StageCapOverride
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.tasks._runtime_mutex import DispatchMutexUnavailableError
from gobby.storage.worktrees import LocalWorktreeManager
from tests.storage.tasks._stage_test_helpers import initialize_manifest, spec


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _init_repo(path: Path) -> None:
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    (path / "README.md").write_text("initial\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


@pytest.mark.asyncio
async def test_development_resume_ticks_with_active_child_epic_integration_workspace(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = LocalProjectManager(temp_db).create(
        "resume-active-integration",
        repo_path=str(repo),
    )
    task_manager = LocalTaskManager(temp_db)
    root = task_manager.create_task(
        project_id=project.id,
        title="Existing web build",
        category="planning",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(
        root.id,
        stage_names=["development", "epic_qa", "merge"],
    )
    child = task_manager.create_task(
        project_id=project.id,
        title="Child API epic",
        category="planning",
        task_type="epic",
        parent_task_id=root.id,
        validation_criteria="Test task completion is observable.",
    )

    child_integration_branch = "gobby/integration/child-api"
    integration_path = (
        Path.home()
        / ".gobby"
        / "worktrees"
        / repo.name
        / child_integration_branch.replace("/", "-")
    )
    integration_path.mkdir(parents=True)
    _init_repo(integration_path)
    worktree = LocalWorktreeManager(temp_db).create(
        project_id=project.id,
        branch_name=child_integration_branch,
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=child.id,
        workspace_role="integration",
    )
    task_manager.artifacts.set_artifacts_atomic(
        child.id,
        integration_branch=child_integration_branch,
        integration_workspace_id=worktree.id,
        target_branch="main",
    )
    temp_db.execute(
        "INSERT INTO sessions "
        "(id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, NOW(), NOW())",
        (
            "ac25647a-384a-5232-8d09-117e2043e20b",
            "ext-active-resume",
            "21000000-0000-4000-8000-000000000001",
            "codex",
            project.id,
        ),
    )
    run_manager = LocalAgentRunManager(temp_db)
    run = run_manager.create(
        parent_session_id="ac25647a-384a-5232-8d09-117e2043e20b",
        provider="codex",
        prompt="review",
        agent_name="backend-developer",
        task_id=child.id,
        run_id="dd49abf3-d60c-533c-8edc-4056c77eba8d",
    )
    run_manager.update_runtime(run.id, worktree_id=worktree.id)

    tick_called = False

    async def tick(*_args: object, **_kwargs: object) -> DispatcherTickSummary:
        nonlocal tick_called
        tick_called = True
        return DispatcherTickSummary()

    runtime = RuntimeHooks(
        dispatcher_tick=tick,
        build_dispatcher_tick=tick,
        attach_build_run_root=lambda *_args: None,
    )

    before = get_build_status(f"#{root.seq_num}", db=temp_db, project_id=project.id)
    assert before["artifact_health"]["ok"] is True

    await resume_existing_lifecycle(
        task_manager,
        root,
        BuildOptions(isolation="worktree", target_branch="main"),
        [],
        [],
        temp_db,
        project.id,
        None,
        "main",
        None,
        runtime=runtime,
    )

    assert tick_called is True


@pytest.mark.asyncio
async def test_development_resume_leaves_invalid_child_epic_workspace_for_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = LocalProjectManager(temp_db).create(
        "resume-integration-refresh",
        repo_path=str(repo),
    )
    task_manager = LocalTaskManager(temp_db)
    root = task_manager.create_task(
        project_id=project.id,
        title="Existing web build",
        category="planning",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(
        root.id,
        stage_names=["development", "epic_qa", "merge"],
    )
    child = task_manager.create_task(
        project_id=project.id,
        title="Child API epic",
        category="planning",
        task_type="epic",
        parent_task_id=root.id,
        validation_criteria="Test task completion is observable.",
    )

    child_integration_branch = "gobby/integration/child-api"
    # Match production _workspace_path (GOBBY_HOME-based) so the repair path
    # recreates the workspace where this test plants the stale directory.
    invalid_path = _workspace_path("worktrees", repo.name, child_integration_branch)
    shutil.rmtree(invalid_path, ignore_errors=True)
    invalid_path.mkdir(parents=True)
    (invalid_path / "not-git.txt").write_text("stale integration workspace\n")
    worktree = LocalWorktreeManager(temp_db).create(
        project_id=project.id,
        branch_name=child_integration_branch,
        worktree_path=str(invalid_path),
        base_branch="main",
        task_id=child.id,
        workspace_role="integration",
    )
    task_manager.artifacts.set_artifacts_atomic(
        child.id,
        integration_branch=child_integration_branch,
        integration_workspace_id=worktree.id,
        target_branch="main",
    )

    async def tick(*_args: object, **_kwargs: object) -> DispatcherTickSummary:
        return DispatcherTickSummary()

    runtime = RuntimeHooks(
        dispatcher_tick=tick,
        build_dispatcher_tick=tick,
        attach_build_run_root=lambda *_args: None,
    )

    before = get_build_status(f"#{root.seq_num}", db=temp_db, project_id=project.id)
    assert before["artifact_health"]["ok"] is False
    assert before["artifact_health"]["items"][0]["task_id"] == child.id

    await resume_existing_lifecycle(
        task_manager,
        root,
        BuildOptions(isolation="worktree", target_branch="main"),
        [],
        [],
        temp_db,
        project.id,
        None,
        "main",
        None,
        runtime=runtime,
    )

    after = get_build_status(f"#{root.seq_num}", db=temp_db, project_id=project.id)
    repaired = LocalWorktreeManager(temp_db).get_by_branch(project.id, child_integration_branch)

    assert after["artifact_health"]["ok"] is False
    assert after["artifact_health"]["issue_count"] == 1
    assert repaired is not None
    assert repaired.id == worktree.id


def test_apply_stage_caps_to_existing_lifecycle_uses_dispatch_mutex(
    temp_db,
    sample_project,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        sample_project["id"],
        "Resume caps",
        validation_criteria="Test task completion is observable.",
    )
    initialize_manifest(temp_db, task.id, [spec("development", 0)])
    mutexes = TaskDispatchMutexManager(temp_db)
    assert mutexes.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="spawn",
        ttl_seconds=30,
    )
    opts = BuildOptions(stage_caps=[StageCapOverride("development", max_work_attempts=4)])

    with pytest.raises(DispatchMutexUnavailableError):
        apply_stage_caps_to_existing_lifecycle(task_manager, task.id, opts)


def test_repair_expanded_epic_root_manifest_replaces_under_dispatch_mutex(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Resume expanded epic",
        category="code",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    initialize_manifest(
        temp_db,
        task.id,
        [
            spec("planning", 0),
            spec("development", 1),
            spec("epic_qa", 2),
            spec("pr", 3),
            spec("merge", 4),
        ],
    )
    monkeypatch.setattr(
        "gobby.build.resume_lifecycle.has_existing_expansion_output",
        lambda *_args: True,
    )

    original_acquire = TaskDispatchMutexManager.acquire_mutex
    heartbeat_attempts: list[bool] = []

    def acquire_and_probe(
        self: TaskDispatchMutexManager,
        task_id: str,
        holder: str,
        kind: str,
        ttl_seconds: int,
        run_id: str | None = None,
        now: datetime | str | None = None,
    ) -> bool:
        acquired = original_acquire(self, task_id, holder, kind, ttl_seconds, run_id, now)
        if acquired and task_id == task.id and kind == "stage_state:replace_manifest":
            heartbeat_attempts.append(
                original_acquire(
                    TaskDispatchMutexManager(temp_db),
                    task_id,
                    "heartbeat",
                    "heartbeat",
                    30,
                )
            )
        return acquired

    monkeypatch.setattr(TaskDispatchMutexManager, "acquire_mutex", acquire_and_probe)

    repaired = repair_expanded_epic_root_manifest_for_resume(
        task_manager,
        task,
        BuildOptions(isolation="worktree"),
        skip_stages=[],
    )

    assert repaired is True
    assert heartbeat_attempts == [False]
    rows = task_manager.stage_states.list_for_task(task.id)
    assert [(row.stage_name, row.position) for row in rows] == [
        ("development", 0),
        ("epic_qa", 1),
        ("pr", 2),
        ("merge", 3),
    ]


def test_manifest_replacement_skips_when_expected_shape_changed(
    temp_db,
    sample_project,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Stale manifest shape",
        category="code",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    initialize_manifest(
        temp_db,
        task.id,
        [spec("planning", 0), spec("development", 1)],
    )
    rows = task_manager.stage_states.list_for_task(task.id)
    expected_shape = [
        (row.stage_name, row.position, row.max_work_attempts, row.max_review_rounds) for row in rows
    ]
    temp_db.execute(
        """
        UPDATE task_stage_states
           SET max_work_attempts = %s
         WHERE task_id = %s AND stage_name = %s
        """,
        (9, task.id, "planning"),
    )

    replaced = task_manager.stage_states.replace_manifest(
        task.id,
        [spec("development", 0)],
        expected_existing_shape=expected_shape,
        from_state="manifest:planning,development",
        reason="test_manifest_replacement_shape_guard",
        by_session_id=None,
        by_actor="build",
    )

    assert replaced is None
    rows = task_manager.stage_states.list_for_task(task.id)
    assert [(row.stage_name, row.position, row.max_work_attempts) for row in rows] == [
        ("planning", 0, 9),
        ("development", 1, None),
    ]

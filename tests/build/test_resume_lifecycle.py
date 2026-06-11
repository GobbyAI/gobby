"""Unit coverage for build lifecycle resume decisions."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gobby.build.dispatch_tick import DispatcherTickSummary
from gobby.build.observability import get_build_status
from gobby.build.options import BuildOptions
from gobby.build.resume_lifecycle import (
    _resume_epic_workspace_refresh_required,
    resume_existing_lifecycle,
)
from gobby.build.runtime_hooks import RuntimeHooks
from gobby.build.workspace_common import BuildWorkspaceError
from gobby.build.workspaces import ensure_epic_integration_workspaces
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager


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


def test_development_resume_only_needs_dispatcher_tick() -> None:
    assert _resume_epic_workspace_refresh_required("development") is False
    assert _resume_epic_workspace_refresh_required("planning") is False
    assert _resume_epic_workspace_refresh_required(None) is False


def test_delivery_resume_refreshes_epic_workspace() -> None:
    assert _resume_epic_workspace_refresh_required("holistic_qa") is True
    assert _resume_epic_workspace_refresh_required("pr") is True
    assert _resume_epic_workspace_refresh_required("merge") is True


@pytest.mark.asyncio
async def test_development_resume_blocks_active_child_epic_integration_workspace(
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
    )
    task_manager.initialize_task_manifest(
        root.id,
        stage_names=["development", "holistic_qa", "merge"],
    )
    child = task_manager.create_task(
        project_id=project.id,
        title="Child API epic",
        category="planning",
        task_type="epic",
        parent_task_id=root.id,
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
        ("parent-session", "ext-active-resume", "machine-1", "codex", project.id),
    )
    run_manager = LocalAgentRunManager(temp_db)
    run = run_manager.create(
        parent_session_id="parent-session",
        provider="codex",
        prompt="review",
        agent_name="backend-developer",
        task_id=child.id,
        run_id="run-active-resume-integration",
    )
    run_manager.update_runtime(run.id, worktree_id=worktree.id)

    def ensure_epic(**_kwargs: object) -> None:
        raise AssertionError("resume must block before refreshing an active workspace")

    def ensure_parent(**_kwargs: object) -> None:
        raise AssertionError("epic resume must not use leaf parent integration refresh")

    async def tick(*_args: object, **_kwargs: object) -> DispatcherTickSummary:
        raise AssertionError("resume must not tick after active workspace conflict")

    runtime = RuntimeHooks(
        dispatcher_tick=tick,
        ensure_epic_integration_workspaces=ensure_epic,
        ensure_task_parent_integration_workspace=ensure_parent,
        build_dispatcher_tick=tick,
        attach_build_run_root=lambda *_args: None,
    )

    before = get_build_status(f"#{root.seq_num}", db=temp_db, project_id=project.id)
    assert before["artifact_health"]["ok"] is True

    with pytest.raises(BuildWorkspaceError, match="active run run-active-resume-integration"):
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


@pytest.mark.asyncio
async def test_development_resume_refreshes_invalid_child_epic_integration_artifact(
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
    )
    task_manager.initialize_task_manifest(
        root.id,
        stage_names=["development", "holistic_qa", "merge"],
    )
    child = task_manager.create_task(
        project_id=project.id,
        title="Child API epic",
        category="planning",
        task_type="epic",
        parent_task_id=root.id,
    )

    child_integration_branch = "gobby/integration/child-api"
    invalid_path = (
        Path.home()
        / ".gobby"
        / "worktrees"
        / repo.name
        / child_integration_branch.replace("/", "-")
    )
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

    def ensure_parent(**_kwargs: object) -> None:
        raise AssertionError("epic resume must not use leaf parent integration refresh")

    async def tick(*_args: object, **_kwargs: object) -> DispatcherTickSummary:
        return DispatcherTickSummary()

    runtime = RuntimeHooks(
        dispatcher_tick=tick,
        ensure_epic_integration_workspaces=ensure_epic_integration_workspaces,
        ensure_task_parent_integration_workspace=ensure_parent,
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

    assert after["artifact_health"]["ok"] is True
    assert after["artifact_health"]["issue_count"] == 0
    assert repaired is not None
    assert repaired.id != worktree.id
    assert _git(invalid_path, "rev-parse", "--is-inside-work-tree") == "true"

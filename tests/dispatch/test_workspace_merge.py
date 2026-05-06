from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gobby.dispatch.actions import MergeWorkspaceAction
from gobby.dispatch.workspace_merge import execute_merge_workspace
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager

pytestmark = pytest.mark.unit


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "README.md").write_text("initial\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


def test_execute_merge_workspace_merges_worktree_and_completes_stage(temp_db, tmp_path: Path):
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    task_path = tmp_path / "task"
    repo.mkdir()
    _init_repo(repo)
    _git(repo, "worktree", "add", "-b", "integration/root", str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/leaf", str(task_path), "integration/root")
    _git(task_path, "config", "user.email", "test@example.com")
    _git(task_path, "config", "user.name", "Test User")
    (task_path / "feature.txt").write_text("feature\n")
    _git(task_path, "add", "feature.txt")
    _git(task_path, "commit", "-m", "feature")

    project = LocalProjectManager(temp_db).create("merge-project", repo_path=str(repo))
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(project_id=project.id, title="Parent", task_type="epic")
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Leaf",
        parent_task_id=parent.id,
        category="code",
        task_type="task",
    )
    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")

    worktrees = LocalWorktreeManager(temp_db)
    worktrees.create(
        project_id=project.id,
        branch_name="integration/root",
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    source = worktrees.create(
        project_id=project.id,
        branch_name="task/leaf",
        worktree_path=str(task_path),
        base_branch="integration/root",
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(task_path),
        worktree_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="integration/root",
    )

    merge_sha = execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=leaf.id,
            task_ref=f"#{leaf.seq_num}",
            backend="worktree",
            target_branch="integration/root",
            source_workspace_id=source.id,
        ),
        db=temp_db,
    )

    assert merge_sha == _git(integration_path, "rev-parse", "HEAD")
    assert (integration_path / "feature.txt").read_text() == "feature\n"
    assert task_manager.stage_states.get(leaf.id, "merge").state == "done"
    assert worktrees.get(source.id).status == "merged"

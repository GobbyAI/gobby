"""Build clean branch cleanup behavior."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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
    (path / "README.md").write_text("initial\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


def _branches(path: Path) -> set[str]:
    output = _git(path, "branch", "--format=%(refname:short)")
    return {line.strip() for line in output.splitlines() if line.strip()}


def test_branch_cleanup_ignores_branch_already_deleted(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    tmp_path: Path,
) -> None:
    from gobby.build import branch_cleanup
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager

    project = LocalProjectManager(temp_db).create("branch-race", repo_path=str(tmp_path))
    task = LocalTaskManager(temp_db).create_task(
        project_id=project.id,
        title="already cleaned branch",
        task_type="task",
        category="code",
    )
    branch = branch_cleanup.default_task_branch_name(task)

    monkeypatch.setattr(branch_cleanup, "local_branches", lambda _repo_path: {branch})
    monkeypatch.setattr(branch_cleanup, "current_branch", lambda _repo_path: "main")

    def branch_deleted_by_peer(
        _repo_path: Path,
        _args: list[str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["git", "branch", "-D", branch],
            returncode=1,
            stdout="",
            stderr=f"error: branch '{branch}' not found",
        )

    monkeypatch.setattr(branch_cleanup, "git", branch_deleted_by_peer)

    deleted, errors = branch_cleanup.delete_orphan_build_branches(temp_db, project.id, [task])

    assert deleted == 0
    assert errors == []


@pytest.mark.asyncio
async def test_clean_deletes_stale_task_branch(temp_db, tmp_path: Path) -> None:
    from gobby.build.branch_cleanup import default_task_branch_name
    from gobby.build.controls import build_clean_target
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    project = LocalProjectManager(temp_db).create("branch-clean", repo_path=str(repo))
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=project.id,
        title="docs: audit long guide",
        task_type="task",
        category="docs",
    )
    stale_branch = default_task_branch_name(task)
    _git(repo, "branch", stale_branch)
    _git(repo, "branch", "task-999-unrelated")

    result = await build_clean_target(
        f"#{task.seq_num}",
        db=temp_db,
        project_id=project.id,
        yes=True,
        force=True,
    )

    branches = _branches(repo)
    assert stale_branch not in branches
    assert "task-999-unrelated" in branches
    assert result.branches_deleted == 1


@pytest.mark.asyncio
async def test_clean_deletes_stale_integration_branch(temp_db, tmp_path: Path) -> None:
    from gobby.build.branch_cleanup import integration_branch_name
    from gobby.build.controls import build_clean_target
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    project = LocalProjectManager(temp_db).create("integration-clean", repo_path=str(repo))
    task_manager = LocalTaskManager(temp_db)
    epic = task_manager.create_task(
        project_id=project.id,
        title="docs: complete 0.4.0 guides audit E2E",
        task_type="epic",
        category="docs",
    )
    stale_branch = integration_branch_name(epic)
    _git(repo, "branch", stale_branch)

    result = await build_clean_target(
        f"#{epic.seq_num}",
        db=temp_db,
        project_id=project.id,
        yes=True,
        force=True,
    )

    assert stale_branch not in _branches(repo)
    assert result.branches_deleted == 1


@pytest.mark.asyncio
async def test_clean_clears_dangling_integration_workspace_id(
    temp_db,
    tmp_path: Path,
) -> None:
    from gobby.build.branch_cleanup import integration_branch_name
    from gobby.build.controls import build_clean_target
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    project = LocalProjectManager(temp_db).create("integration-clean", repo_path=str(repo))
    task_manager = LocalTaskManager(temp_db)
    epic = task_manager.create_task(
        project_id=project.id,
        title="recover missing integration metadata",
        task_type="epic",
        category="code",
    )
    stale_branch = integration_branch_name(epic)
    stale_worktree_id = "wt-missing-row"
    _git(repo, "branch", stale_branch)
    task_manager.artifacts.set_artifacts_atomic(
        epic.id,
        integration_branch=stale_branch,
        integration_workspace_id=stale_worktree_id,
        target_branch="main",
    )

    result = await build_clean_target(
        f"#{epic.seq_num}",
        db=temp_db,
        project_id=project.id,
        yes=True,
        force=True,
    )
    artifacts = task_manager.artifacts.get_artifacts(epic.id)

    assert artifacts.integration_workspace_id is None
    assert stale_branch not in _branches(repo)
    assert result.branches_deleted == 1
    assert any(
        artifact.artifact_id == stale_worktree_id
        and artifact.source == "task_artifacts_integration"
        and artifact.deleted
        for artifact in result.artifacts
    )

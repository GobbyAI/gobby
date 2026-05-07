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


@pytest.mark.asyncio
async def test_clean_deletes_stale_task_branch(temp_db, tmp_path: Path) -> None:
    from gobby.build.controls import _default_branch_dir_name, build_clean_target
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
    stale_branch = _default_branch_dir_name(task)
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
    from gobby.build.controls import _integration_branch_name, build_clean_target
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
    stale_branch = _integration_branch_name(epic)
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

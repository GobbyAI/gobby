"""Build clean branch cleanup behavior."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import Project

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


def _checkout_project(
    temp_db: HubDatabase,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
) -> Project:
    from tests.fixtures.isolated_checkout import install_isolated_checkout_project

    isolated = install_isolated_checkout_project(temp_db, root, name=name, monkeypatch=monkeypatch)
    monkeypatch.setattr(
        "gobby.storage.worktrees.require_machine_id",
        lambda: isolated.machine_id,
    )
    return isolated.project


def test_branch_cleanup_ignores_branch_already_deleted(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    tmp_path: Path,
) -> None:
    from gobby.build import branch_cleanup
    from gobby.storage.tasks import LocalTaskManager

    project = _checkout_project(temp_db, tmp_path, monkeypatch, name="branch-race")
    task = LocalTaskManager(temp_db).create_task(
        project_id=project.id,
        title="already cleaned branch",
        task_type="task",
        category="code",
        validation_criteria="Branch cleanup tolerates an already-deleted branch.",
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


def test_branch_cleanup_refuses_missing_project_repo_path(  # tdd-red window
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
) -> None:
    from gobby.build import branch_cleanup
    from gobby.storage.project_checkouts import CheckoutNotFoundError
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager
    from tests.fixtures.isolated_checkout import insert_isolated_machine, patch_local_machine_id

    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create("missing-repo")
    task = LocalTaskManager(temp_db).create_task(
        project_id=project.id,
        title="missing project repo path",
        task_type="task",
        category="code",
        validation_criteria="Branch cleanup rejects a missing repository path.",
    )

    def fail_git_operation(*_args: object, **_kwargs: object) -> None:
        pytest.fail("branch cleanup must not inspect or delete branches without a checkout")

    monkeypatch.setattr(branch_cleanup, "local_branches", fail_git_operation)
    monkeypatch.setattr(branch_cleanup, "current_branch", fail_git_operation)
    monkeypatch.setattr(branch_cleanup, "git", fail_git_operation)

    with pytest.raises(CheckoutNotFoundError):
        branch_cleanup.project_path(temp_db, project.id)

    deleted, errors = branch_cleanup.delete_orphan_build_branches(temp_db, project.id, [task])

    assert deleted == 0
    assert errors
    assert all("repo_path" not in error for error in errors)
    assert any("checkout" in error.lower() for error in errors)


@pytest.mark.asyncio
async def test_clean_deletes_stale_task_branch(
    temp_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.build.branch_cleanup import default_task_branch_name
    from gobby.build.controls import build_clean_target
    from gobby.storage.tasks import LocalTaskManager

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    project = _checkout_project(temp_db, repo, monkeypatch, name="branch-clean")
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=project.id,
        title="docs: audit long guide",
        task_type="task",
        category="docs",
        validation_criteria="Test task completion is observable.",
    )
    stale_branch = default_task_branch_name(task)
    _git(repo, "branch", stale_branch)
    manual_branch = f"task-{task.seq_num}-manual"
    _git(repo, "branch", manual_branch)
    _git(repo, "branch", "ff3e4973-2f46-574f-898e-bcd778083b49")

    result = await build_clean_target(
        f"#{task.seq_num}",
        db=temp_db,
        project_id=project.id,
        yes=True,
        force=True,
    )

    branches = _branches(repo)
    assert stale_branch not in branches
    assert manual_branch in branches
    assert "ff3e4973-2f46-574f-898e-bcd778083b49" in branches
    assert result.branches_deleted == 1


@pytest.mark.asyncio
async def test_clean_deletes_stale_integration_branch(
    temp_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.build.branch_cleanup import integration_branch_name
    from gobby.build.controls import build_clean_target
    from gobby.storage.tasks import LocalTaskManager

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    project = _checkout_project(temp_db, repo, monkeypatch, name="integration-clean")
    task_manager = LocalTaskManager(temp_db)
    epic = task_manager.create_task(
        project_id=project.id,
        title="docs: complete 0.4.0 guides audit E2E",
        task_type="epic",
        category="docs",
        validation_criteria="Test task completion is observable.",
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.build.branch_cleanup import integration_branch_name
    from gobby.build.controls import build_clean_target
    from gobby.storage.tasks import LocalTaskManager

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    project = _checkout_project(temp_db, repo, monkeypatch, name="integration-clean")
    task_manager = LocalTaskManager(temp_db)
    epic = task_manager.create_task(
        project_id=project.id,
        title="recover missing integration metadata",
        task_type="epic",
        category="code",
        validation_criteria="Test task completion is observable.",
    )
    stale_branch = integration_branch_name(epic)
    stale_worktree_id = "5a540ab5-0ba4-5e8e-ad09-93e63ed828fd"
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


@pytest.mark.asyncio
async def test_clean_force_defers_dirty_descendant_worktree(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.build.controls import build_clean_target
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.worktrees import LocalWorktreeManager

    repo = tmp_path / "repo"
    worktree_path = tmp_path / "cbea7016-42cc-58a2-a555-6399b7c3d051"
    repo.mkdir()
    _init_repo(repo)

    project = _checkout_project(temp_db, repo, monkeypatch, name="dirty-clean")
    task_manager = LocalTaskManager(temp_db)
    root = task_manager.create_task(
        project_id=project.id,
        title="Root",
        task_type="epic",
        category="planning",
        validation_criteria="Test task completion is observable.",
    )
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Dirty leaf",
        parent_task_id=root.id,
        task_type="task",
        category="code",
        validation_criteria="Test task completion is observable.",
    )

    _git(repo, "worktree", "add", "-b", "task/dirty-leaf", str(worktree_path), "main")
    worktrees = LocalWorktreeManager(temp_db)
    worktree = worktrees.create(
        project_id=project.id,
        branch_name="task/dirty-leaf",
        worktree_path=str(worktree_path),
        base_branch="main",
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(worktree_path),
        worktree_id=worktree.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="main",
    )
    (worktree_path / "dirty.txt").write_text("uncommitted work\n", encoding="utf-8")

    result = await build_clean_target(
        f"#{root.seq_num}",
        db=temp_db,
        project_id=project.id,
        yes=True,
        force=True,
    )

    artifact = next(
        artifact
        for artifact in result.artifacts
        if artifact.task_id == leaf.id and artifact.artifact_id == worktree.id
    )
    stored_artifacts = task_manager.artifacts.get_artifacts(leaf.id)

    assert worktree_path.exists()
    assert worktrees.get(worktree.id) is not None
    assert stored_artifacts.worktree_id == worktree.id
    assert artifact.deferred
    assert artifact.cleanup_reason == "dirty_open_task_deferred"
    assert not artifact.deleted
    assert result.branches_deleted == 0


@pytest.mark.asyncio
async def test_clean_dirty_worktree_override_deletes_descendant_worktree(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.build.controls import build_clean_target
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.worktrees import LocalWorktreeManager

    repo = tmp_path / "repo"
    worktree_path = tmp_path / "cbea7016-42cc-58a2-a555-6399b7c3d051"
    repo.mkdir()
    _init_repo(repo)

    project = _checkout_project(temp_db, repo, monkeypatch, name="dirty-clean-override")
    task_manager = LocalTaskManager(temp_db)
    root = task_manager.create_task(
        project_id=project.id,
        title="Root",
        task_type="epic",
        category="planning",
        validation_criteria="Test task completion is observable.",
    )
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Dirty leaf",
        parent_task_id=root.id,
        task_type="task",
        category="code",
        validation_criteria="Test task completion is observable.",
    )

    _git(repo, "worktree", "add", "-b", "task/dirty-leaf", str(worktree_path), "main")
    worktrees = LocalWorktreeManager(temp_db)
    worktree = worktrees.create(
        project_id=project.id,
        branch_name="task/dirty-leaf",
        worktree_path=str(worktree_path),
        base_branch="main",
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(worktree_path),
        worktree_id=worktree.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="main",
    )
    (worktree_path / "dirty.txt").write_text("uncommitted work\n", encoding="utf-8")

    result = await build_clean_target(
        f"#{root.seq_num}",
        db=temp_db,
        project_id=project.id,
        yes=True,
        force=True,
        delete_dirty_worktrees=True,
    )

    artifact = next(
        artifact
        for artifact in result.artifacts
        if artifact.task_id == leaf.id and artifact.artifact_id == worktree.id
    )
    stored_artifacts = task_manager.artifacts.get_artifacts(leaf.id)

    assert not worktree_path.exists()
    assert worktrees.get(worktree.id) is None
    assert stored_artifacts.worktree_id is None
    assert not artifact.deferred
    assert artifact.deleted


def test_get_project_path_uses_machine_checkout(  # tdd-red window
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.build.control_artifacts import get_project_path
    from tests.fixtures.isolated_checkout import install_isolated_checkout_project

    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "artifacts-root", name="artifact-checkout", monkeypatch=monkeypatch
    )

    assert get_project_path(temp_db, isolated.project.id) == Path(isolated.root_path)


def test_get_project_path_fails_closed_without_checkout(  # tdd-red window
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.build.control_artifacts import get_project_path
    from gobby.storage.project_checkouts import CheckoutNotFoundError
    from gobby.storage.projects import LocalProjectManager
    from tests.fixtures.isolated_checkout import insert_isolated_machine, patch_local_machine_id

    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create("missing-artifact-checkout")

    with pytest.raises(CheckoutNotFoundError):
        get_project_path(temp_db, project.id)

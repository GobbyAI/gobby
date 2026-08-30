"""Tests for reverse worktree and clone registry reconciliation."""

from __future__ import annotations

import asyncio
import logging
import subprocess
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.clones import git as clone_git
from gobby.runner_maintenance import isolation as isolation_loop
from gobby.runner_maintenance import isolation_reconciliation as reconciliation
from gobby.runner_maintenance.isolation_reconciliation import (
    IsolationReconciliationResult,
    reconcile_isolation_registry,
)
from gobby.storage.clones import LocalCloneManager
from gobby.storage.hub.protocol import (
    HubDatabase,
    IsolationRegistryReconciliation,
)
from gobby.storage.projects import Project
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.worktrees.git import WorktreeGitManager, WorktreeInfo
from gobby.worktrees.git import _status as worktree_git_status
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

pytestmark = pytest.mark.integration


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _create_repository(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "README.md").write_text("repository\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


def _create_stray_worktree(repo: Path, path: Path, branch: str) -> None:
    _git(repo, "worktree", "add", "-b", branch, str(path), "HEAD")


def _create_stray_clone(repo: Path, path: Path, *, detached: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo.parent, "clone", str(repo), str(path))
    if detached:
        _git(path, "checkout", "--detach", "HEAD")


@pytest.mark.asyncio
async def test_reconciliation_adopts_each_stray_once_and_logs_one_summary(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = tmp_path / "repo"
    worktree_path = tmp_path / "stray-worktree"
    clones_root = tmp_path / "clones"
    clone_path = clones_root / "reconcile-project" / "stray-clone"
    nested_clone_path = clones_root / "reconcile-project" / "group" / "nested-clone"
    _create_repository(repo)
    _create_stray_worktree(repo, worktree_path, "task/reconcile")
    _create_stray_clone(repo, clone_path, detached=True)
    _create_stray_clone(repo, nested_clone_path)
    isolated = install_isolated_checkout_project(
        temp_db,
        repo,
        name="reconcile-project",
        monkeypatch=monkeypatch,
    )
    project = isolated.project
    monkeypatch.setattr(
        "gobby.storage.worktrees.require_machine_id",
        lambda: isolated.machine_id,
    )
    monkeypatch.setattr(
        "gobby.storage.clones.require_machine_id",
        lambda: isolated.machine_id,
    )
    monkeypatch.setattr(clone_git, "CLONES_ROOT", clones_root)
    db_calls: list[str] = []

    async def run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        db_calls.append(func.__qualname__)
        return await asyncio.to_thread(func, *args, **kwargs)

    caplog.set_level(logging.INFO, logger=reconciliation.__name__)

    first = await reconcile_isolation_registry(
        temp_db,
        machine_id=isolated.machine_id,
        run_db=run_db,
    )
    second = await reconcile_isolation_registry(
        temp_db,
        machine_id=isolated.machine_id,
        run_db=run_db,
    )

    assert first == IsolationReconciliationResult(worktrees_adopted=1, clones_adopted=1)
    assert second == IsolationReconciliationResult()
    worktree = LocalWorktreeManager(temp_db).get_by_path(str(worktree_path.resolve()))
    clone = LocalCloneManager(temp_db).get_by_path(str(clone_path.resolve()))
    assert worktree is not None
    assert worktree.project_id == project.id
    assert worktree.machine_id == isolated.machine_id
    assert worktree.branch_name == "task/reconcile"
    assert worktree.base_branch == "main"
    assert clone is not None
    assert clone.project_id == project.id
    assert clone.machine_id == isolated.machine_id
    assert clone.branch_name is None
    assert clone.base_branch == "main"
    assert clone.remote_url == str(repo)
    assert LocalCloneManager(temp_db).get_by_path(str(nested_clone_path.resolve())) is None
    assert "LocalProjectCheckoutManager.list_for_machine" in db_calls
    assert "LocalProjectManager.get" in db_calls
    assert "LocalWorktreeManager.register_adopted" in db_calls
    assert "LocalCloneManager.register_adopted" in db_calls
    summaries = [
        record
        for record in caplog.records
        if record.message.startswith("Isolation registry reconciliation adopted")
    ]
    assert len(summaries) == 1


@pytest.mark.asyncio
async def test_worktree_scan_skips_primary_bare_prunable_and_reserved_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    valid_path = tmp_path / "valid"
    worktrees = [
        WorktreeInfo(path=str(repo), branch="main", commit="a"),
        WorktreeInfo(path=str(tmp_path / "bare"), branch=None, commit="a", is_bare=True),
        WorktreeInfo(
            path=str(tmp_path / "prunable"),
            branch="task/prunable",
            commit="a",
            prunable=True,
        ),
        WorktreeInfo(path=str(tmp_path / "_orphaned-old"), branch="task/old", commit="a"),
        WorktreeInfo(path=str(tmp_path / "_migrated-old"), branch="task/old", commit="a"),
        WorktreeInfo(path=str(valid_path), branch="task/valid", commit="a"),
    ]
    inspected = WorktreeInfo(
        path=str(valid_path.resolve()),
        branch="task/valid",
        commit="a",
    )
    inspected_paths: list[Path] = []
    registrations: list[tuple[object, ...]] = []

    def inspect_worktree(path: Path) -> WorktreeInfo:
        inspected_paths.append(path)
        return inspected

    def register_adopted(*args: object) -> tuple[object, bool]:
        registrations.append(args)
        return object(), True

    def list_worktrees(
        _manager: object,
        *,
        failure_log_level: int,
    ) -> list[WorktreeInfo]:
        assert failure_log_level == logging.DEBUG
        return worktrees

    manager = SimpleNamespace(
        inspect_worktree=inspect_worktree,
        get_default_branch=lambda: "trunk",
    )
    monkeypatch.setattr(reconciliation, "WorktreeGitManager", lambda _path: manager)
    monkeypatch.setattr(worktree_git_status, "list_worktrees", list_worktrees)
    storage = cast(
        "LocalWorktreeManager",
        SimpleNamespace(register_adopted=register_adopted),
    )
    project = cast(
        "Project",
        SimpleNamespace(id="project-1", name="project"),
    )

    async def run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    adopted = await reconciliation._reconcile_project_worktrees(
        project,
        str(repo),
        storage,
        run_db=run_db,
    )

    assert adopted == 1
    assert inspected_paths == [valid_path.resolve()]
    assert registrations == [("project-1", "task/valid", str(valid_path.resolve()), "trunk")]


@pytest.mark.asyncio
async def test_worktree_scan_skips_git_probe_failure_without_error_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    mock_run = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=["git", "worktree", "list", "--porcelain"],
            returncode=128,
            stdout="",
            stderr="fatal: not a git repository",
        )
    )
    monkeypatch.setattr(WorktreeGitManager, "_run_git", mock_run)
    storage = cast(
        "LocalWorktreeManager",
        SimpleNamespace(register_adopted=MagicMock()),
    )
    project = cast(
        "Project",
        SimpleNamespace(id="project-1", name="project"),
    )
    caplog.set_level(logging.DEBUG, logger="gobby.worktrees.git._status")

    adopted = await reconciliation._reconcile_project_worktrees(
        project,
        str(repo),
        storage,
        run_db=None,
    )

    assert adopted == 0
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert any(
        record.levelno == logging.DEBUG and "Failed to list worktrees" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_reconciliation_ignores_hidden_and_unregistered_projects(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clones_root = tmp_path / "clones"
    monkeypatch.setattr(clone_git, "CLONES_ROOT", clones_root)

    hidden_repo = tmp_path / "hidden-repo"
    hidden_worktree = tmp_path / "hidden-worktree"
    hidden_clone = clones_root / "_orphaned-hidden" / "clone"
    _create_repository(hidden_repo)
    _create_stray_worktree(hidden_repo, hidden_worktree, "task/hidden")
    _create_stray_clone(hidden_repo, hidden_clone)
    hidden = install_isolated_checkout_project(
        temp_db,
        hidden_repo,
        name="_orphaned-hidden",
        monkeypatch=monkeypatch,
    )

    unregistered_repo = tmp_path / "unregistered-repo"
    unregistered_worktree = tmp_path / "unregistered-worktree"
    unregistered_clone = clones_root / "unregistered" / "clone"
    _create_repository(unregistered_repo)
    _create_stray_worktree(unregistered_repo, unregistered_worktree, "task/unregistered")
    _create_stray_clone(unregistered_repo, unregistered_clone)

    foreign_repo = tmp_path / "foreign-repo"
    foreign_worktree = tmp_path / "foreign-worktree"
    foreign_clone = clones_root / "foreign" / "clone"
    _create_repository(foreign_repo)
    _create_stray_worktree(foreign_repo, foreign_worktree, "task/foreign")
    _create_stray_clone(foreign_repo, foreign_clone)
    install_isolated_checkout_project(
        temp_db,
        foreign_repo,
        name="foreign",
    )

    result = await reconcile_isolation_registry(temp_db, machine_id=hidden.machine_id)

    assert result == IsolationReconciliationResult()
    assert LocalWorktreeManager(temp_db).get_by_path(str(hidden_worktree.resolve())) is None
    assert LocalCloneManager(temp_db).get_by_path(str(hidden_clone.resolve())) is None
    assert LocalWorktreeManager(temp_db).get_by_path(str(unregistered_worktree.resolve())) is None
    assert LocalCloneManager(temp_db).get_by_path(str(unregistered_clone.resolve())) is None
    assert LocalWorktreeManager(temp_db).get_by_path(str(foreign_worktree.resolve())) is None
    assert LocalCloneManager(temp_db).get_by_path(str(foreign_clone.resolve())) is None


@pytest.mark.asyncio
async def test_reconciliation_uses_machine_scoped_advisory_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gobby.storage.workspace_machine_scope.require_machine_id",
        lambda: "machine-42",
    )
    db = MagicMock(spec=HubDatabase)
    observed: list[IsolationRegistryReconciliation] = []

    @asynccontextmanager
    async def advisory_lock(
        lock: IsolationRegistryReconciliation,
    ) -> AsyncIterator[None]:
        observed.append(lock)
        yield

    db.advisory_lock.side_effect = advisory_lock
    reconcile_once = AsyncMock(return_value=IsolationReconciliationResult())
    monkeypatch.setattr(reconciliation, "_reconcile_isolation_registry", reconcile_once)

    result = await reconcile_isolation_registry(db, machine_id="machine-42")

    assert result == IsolationReconciliationResult()
    assert observed == [IsolationRegistryReconciliation(machine_id="machine-42")]
    reconcile_once.assert_awaited_once_with(db, "machine-42", run_db=None)


@pytest.mark.asyncio
async def test_reconciliation_rejects_foreign_machine_before_checkout_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gobby.storage.workspace_machine_scope.require_machine_id",
        lambda: "local-machine",
    )
    db = MagicMock(spec=HubDatabase)

    with pytest.raises(MachineOwnershipMismatchError):
        await reconcile_isolation_registry(db, machine_id="foreign-machine")

    db.advisory_lock.assert_not_called()


@pytest.mark.asyncio
async def test_hourly_isolation_loop_runs_reverse_reconciliation(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reconcile = AsyncMock(return_value=IsolationReconciliationResult())
    monkeypatch.setattr(isolation_loop, "reconcile_isolation_registry", reconcile)
    shutdown_checks = 0

    def is_shutdown_requested() -> bool:
        nonlocal shutdown_checks
        shutdown_checks += 1
        return shutdown_checks > 1

    await isolation_loop.cleanup_expired_isolation_loop(
        temp_db,
        is_shutdown_requested,
        interval_hours=0,
    )

    assert shutdown_checks == 2
    reconcile.assert_awaited_once_with(temp_db, run_db=None)

"""Tests for runner isolation maintenance cleanup."""

from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.runner_maintenance import (
    _cleanup_missing_isolation_records,
    cleanup_expired_isolation_loop,
)
from gobby.storage.clones import LocalCloneManager
from gobby.storage.executor import DatabaseExecutor
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import Project
from gobby.storage.worktrees import LocalWorktreeManager
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

pytestmark = pytest.mark.unit


def _install_project(
    temp_db: HubDatabase,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Project:
    isolated = install_isolated_checkout_project(
        temp_db,
        root,
        name="proj-1",
        monkeypatch=monkeypatch,
    )
    monkeypatch.setattr(
        "gobby.storage.worktrees.require_machine_id",
        lambda: isolated.machine_id,
    )
    monkeypatch.setattr(
        "gobby.storage.clones.require_machine_id",
        lambda: isolated.machine_id,
    )
    return isolated.project


def test_cleanup_missing_isolation_records_removes_dead_paths(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worktree and clone records with missing directories are removed."""
    project = _install_project(temp_db, tmp_path / "repo", monkeypatch)
    worktrees = LocalWorktreeManager(temp_db)
    clones = LocalCloneManager(temp_db)

    existing_worktree_path = tmp_path / "existing-worktree"
    existing_worktree_path.mkdir()
    existing_clone_path = tmp_path / "existing-clone"
    existing_clone_path.mkdir()

    missing_worktree = worktrees.create(
        project_id=project.id,
        branch_name="task/missing-worktree",
        worktree_path=str(tmp_path / "missing-worktree"),
    )
    existing_worktree = worktrees.create(
        project_id=project.id,
        branch_name="task/existing-worktree",
        worktree_path=str(existing_worktree_path),
    )
    missing_clone = clones.create(
        project_id=project.id,
        branch_name="task/missing-clone",
        clone_path=str(tmp_path / "missing-clone"),
    )
    existing_clone = clones.create(
        project_id=project.id,
        branch_name="task/existing-clone",
        clone_path=str(existing_clone_path),
    )

    counts = _cleanup_missing_isolation_records(worktrees, clones)

    assert counts == {"worktrees": 1, "clones": 1}
    assert worktrees.get(missing_worktree.id) is None
    assert clones.get(missing_clone.id) is None
    assert worktrees.get(existing_worktree.id) is not None
    assert clones.get(existing_clone.id) is not None


@pytest.mark.asyncio
async def test_expired_isolation_loop_uses_bounded_db_runner(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing-record cleanup in the periodic loop keeps PostgreSQL handles bounded."""
    project = _install_project(temp_db, tmp_path / "repo", monkeypatch)
    LocalWorktreeManager(temp_db).create(
        project_id=project.id,
        branch_name="task/missing-worktree",
        worktree_path=str(tmp_path / "missing-worktree"),
    )
    LocalCloneManager(temp_db).create(
        project_id=project.id,
        branch_name="task/missing-clone",
        clone_path=str(tmp_path / "missing-clone"),
    )

    executor = DatabaseExecutor(max_workers=2, thread_name_prefix="isolation-db")
    shutdown_checks = 0

    def is_shutdown_requested() -> bool:
        nonlocal shutdown_checks
        shutdown_checks += 1
        return shutdown_checks > 1

    try:
        await asyncio.wait_for(
            cleanup_expired_isolation_loop(
                temp_db,
                is_shutdown_requested,
                interval_hours=0,
                run_db=executor.run,
            ),
            timeout=2,
        )
        stats = executor.stats()
        assert stats.submitted == stats.completed
        assert stats.active == 0
        assert stats.queued == 0
        assert stats.threads <= executor.max_workers
    finally:
        executor.shutdown()
        executor.join()


@pytest.mark.asyncio
async def test_expired_isolation_loop_deletes_only_safe_clones(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _install_project(temp_db, tmp_path / "repo", monkeypatch)
    clones = LocalCloneManager(temp_db)
    active_path = tmp_path / "expired-active-clone"
    active_path.mkdir()
    active = clones.create(
        project_id=project.id,
        branch_name="task/expired-active-clone",
        clone_path=str(active_path),
        cleanup_after=datetime(2020, 1, 1, tzinfo=UTC),
    )
    merged_path = tmp_path / "expired-merged-clone"
    merged_path.mkdir()
    merged = clones.create(
        project_id=project.id,
        branch_name="task/expired-merged-clone",
        clone_path=str(merged_path),
    )
    clones.mark_merged(merged.id, cleanup_after=datetime(2020, 1, 1, tzinfo=UTC))
    shutdown_checks = 0

    def is_shutdown_requested() -> bool:
        nonlocal shutdown_checks
        shutdown_checks += 1
        return shutdown_checks > 1

    await cleanup_expired_isolation_loop(
        temp_db,
        is_shutdown_requested,
        interval_hours=0,
    )

    assert active_path.exists()
    assert clones.get(active.id) is not None
    assert not merged_path.exists()
    assert clones.get(merged.id) is None


@pytest.mark.asyncio
async def test_expired_isolation_loop_runs_git_in_parent_repo(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expired cleanup targets the recorded repo when daemon cwd is unrelated."""
    repo_path = tmp_path / "repo"
    worktree_path = tmp_path / "expired-worktree"
    repo_path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_path, check=True)
    (repo_path / "tracked.txt").write_text("tracked\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_path, check=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "task/expired", str(worktree_path)],
        cwd=repo_path,
        check=True,
    )

    project = _install_project(temp_db, repo_path, monkeypatch)
    worktrees = LocalWorktreeManager(temp_db)
    worktree = worktrees.create(
        project_id=project.id,
        branch_name="task/expired",
        worktree_path=str(worktree_path),
    )
    worktrees.mark_merged(worktree.id)

    unrelated_path = tmp_path / "unrelated"
    unrelated_path.mkdir()
    monkeypatch.chdir(unrelated_path)
    from gobby.runner_maintenance import isolation as runner_maintenance

    run_git = runner_maintenance._run_git_command
    git_cwds: list[str] = []

    def record_git_cwd(args: list[str], *, cwd: str) -> int:
        git_cwds.append(cwd)
        return run_git(args, cwd=cwd)

    monkeypatch.setattr(runner_maintenance, "_run_git_command", record_git_cwd)
    shutdown_checks = 0

    def is_shutdown_requested() -> bool:
        nonlocal shutdown_checks
        shutdown_checks += 1
        return shutdown_checks > 1

    await cleanup_expired_isolation_loop(
        temp_db,
        is_shutdown_requested,
        interval_hours=0,
    )

    worktree_list = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    branch_list = subprocess.run(
        ["git", "branch", "--list", "task/expired"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert str(worktree_path) not in worktree_list.stdout
    assert branch_list.stdout == ""
    assert worktrees.get(worktree.id) is None
    assert git_cwds == [str(repo_path)] * 3


@pytest.mark.asyncio
async def test_expired_isolation_loop_logs_git_cleanup_failures(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nonzero prune and branch results retain actionable cleanup evidence."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    project = _install_project(temp_db, repo_path, monkeypatch)
    worktrees = LocalWorktreeManager(temp_db)
    worktree = worktrees.create(
        project_id=project.id,
        branch_name="task/expired",
        worktree_path=str(tmp_path / "expired-worktree"),
    )
    worktrees.mark_merged(worktree.id)

    git_commands: list[tuple[list[str], str]] = []

    def run_git(args: list[str], *, cwd: str) -> int:
        git_commands.append((args, cwd))
        if args[-2:] == ["worktree", "prune"]:
            return 7
        if "branch" in args:
            return 9
        return 0

    monkeypatch.setattr("gobby.runner_maintenance.isolation._run_git_command", run_git)
    caplog.set_level("WARNING")
    shutdown_checks = 0

    def is_shutdown_requested() -> bool:
        nonlocal shutdown_checks
        shutdown_checks += 1
        return shutdown_checks > 1

    await cleanup_expired_isolation_loop(
        temp_db,
        is_shutdown_requested,
        interval_hours=0,
    )

    assert git_commands == [
        (
            ["git", "worktree", "remove", "--force", str(tmp_path / "expired-worktree")],
            str(repo_path),
        ),
        (["git", "worktree", "prune"], str(repo_path)),
        (["git", "branch", "-D", "task/expired"], str(repo_path)),
    ]
    assert f"git worktree prune failed in {repo_path} (exit code 7)" in caplog.messages
    assert (
        f"git branch deletion failed for task/expired in {repo_path} (exit code 9)"
        in caplog.messages
    )

"""A worktree interrupted mid-deletion is finished by the next start's recovery pass (#25838)."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from gobby.storage.worktrees import LocalWorktreeManager, Worktree
from gobby.worktrees.deletion import (
    DeletionSurface,
    WorktreeDeletionRequest,
    WorktreeDeletionResult,
    delete_worktree_transaction,
)
from gobby.worktrees.executor import DestructiveBoundary
from gobby.worktrees.git import WorktreeGitManager
from gobby.worktrees.recovery import recover_deleting_worktrees

pytestmark = pytest.mark.integration

PROJECT_ID = "11111111-1111-4111-8111-111111110001"


class _Interrupted(BaseException):
    """Stands in for a process interruption, which bypasses Exception handlers."""


def _interrupt(*_args: object, **_kwargs: object) -> None:
    raise _Interrupted("interrupted")


class _Rows:
    """In-memory worktree rows with the storage calls the deletion path makes."""

    def __init__(self, *rows: Worktree) -> None:
        self.db: None = None
        self.rows = {row.id: row for row in rows}
        self.interrupt_next_delete = False

    def get(self, worktree_id: str) -> Worktree | None:
        return self.rows.get(worktree_id)

    def update(self, worktree_id: str, **fields: Any) -> Worktree | None:
        row = self.rows.get(worktree_id)
        if row is None:
            return None
        self.rows[worktree_id] = replace(row, **fields)
        return self.rows[worktree_id]

    def delete(self, worktree_id: str) -> bool:
        if self.interrupt_next_delete:
            self.interrupt_next_delete = False
            raise _Interrupted("interrupted before the record delete")
        return self.rows.pop(worktree_id, None) is not None

    def list_worktrees(self, *, status: str | None = None, **_filters: Any) -> list[Worktree]:
        return [row for row in self.rows.values() if status is None or row.status == status]

    @contextmanager
    def lock_for_cleanup(
        self, worktree_id: str, *, expired_only: bool = True
    ) -> Iterator[Worktree | None]:
        yield self.rows.get(worktree_id)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()


def _row(
    worktree_id: str,
    path: Path,
    *,
    status: str = "active",
    branch: str | None = "task-branch",
) -> Worktree:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Worktree(
        id=worktree_id,
        project_id=PROJECT_ID,
        task_id=None,
        branch_name=branch,
        worktree_path=str(path),
        base_branch="main",
        agent_session_id=None,
        status=status,
        created_at=now,
        updated_at=now,
    )


def _request(
    surface: DeletionSurface, *, merged_into: str | None = "main"
) -> WorktreeDeletionRequest:
    return WorktreeDeletionRequest(worktree_id="wt-1", surface=surface, merged_into=merged_into)


def _transaction(
    git: WorktreeGitManager,
    rows: _Rows,
    request: WorktreeDeletionRequest,
    boundary: DestructiveBoundary | None = None,
) -> Any:
    return delete_worktree_transaction(
        boundary or DestructiveBoundary(),
        request=request,
        worktree_storage=cast(LocalWorktreeManager, rows),
        resolve_git_manager=lambda _worktree: git,
        task_manager=None,
    )


async def _recover(
    git: WorktreeGitManager,
    rows: _Rows,
    *,
    should_stop: Callable[[], bool] = lambda: False,
) -> list[WorktreeDeletionResult]:
    return await recover_deleting_worktrees(
        worktree_storage=cast(LocalWorktreeManager, rows),
        resolve_git_manager=lambda _worktree: git,
        task_manager=None,
        executor=None,
        should_stop=should_stop,
    )


@pytest.fixture(autouse=True)
def _no_live_session_or_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gobby.worktrees.deletion.refuse_occupied_worktree", lambda *_a, **_k: None)
    monkeypatch.setattr("gobby.worktrees.deletion.emit_worktree_event", lambda *_a, **_k: None)


@pytest.fixture
def landed(tmp_path: Path) -> tuple[Path, Path]:
    """A repo whose feature worktree branch is merged into main; returns (repo, worktree)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("base\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-b", "task-branch", str(worktree), "main")
    (worktree / "feature.txt").write_text("feature\n")
    _git(worktree, "add", "feature.txt")
    _git(worktree, "commit", "-m", "feature work")
    _git(repo, "merge", "--no-ff", "task-branch", "-m", "land feature")
    return repo, worktree


async def test_interrupt_after_git_removal_leaves_record_deleting_for_recovery(
    landed: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, worktree = landed
    git = WorktreeGitManager(repo)
    rows = _Rows(_row("wt-1", worktree))
    monkeypatch.setattr("gobby.worktrees.deletion.cleanup_checkout_cargo_target_dir", _interrupt)

    with pytest.raises(_Interrupted):
        await _transaction(git, rows, _request(DeletionSurface.TEARDOWN))

    assert not worktree.exists()
    assert rows.rows["wt-1"].status == "deleting"

    results = await _recover(git, rows)

    assert [result.success for result in results] == [True]
    assert "wt-1" not in rows.rows
    assert _git(repo, "branch", "--list", "task-branch") == ""


async def test_interrupt_before_record_delete_leaves_record_deleting_for_recovery(
    landed: tuple[Path, Path],
) -> None:
    repo, worktree = landed
    git = WorktreeGitManager(repo)
    rows = _Rows(_row("wt-1", worktree))
    rows.interrupt_next_delete = True

    with pytest.raises(_Interrupted):
        await _transaction(git, rows, _request(DeletionSurface.TEARDOWN))

    assert not worktree.exists()
    assert rows.rows["wt-1"].status == "deleting"

    results = await _recover(git, rows)

    assert [result.success for result in results] == [True]
    assert "wt-1" not in rows.rows


async def test_recovery_finishes_a_tree_with_only_tracked_deletions(
    landed: tuple[Path, Path],
) -> None:
    repo, worktree = landed
    git = WorktreeGitManager(repo)
    (worktree / "tracked.txt").unlink()
    rows = _Rows(_row("wt-1", worktree, status="deleting"))

    refused = await _transaction(git, rows, _request(DeletionSurface.TEARDOWN))

    assert refused.success is False
    assert refused.uncommitted_changes is True
    assert worktree.exists()

    results = await _recover(git, rows)

    assert [result.success for result in results] == [True]
    assert not worktree.exists()
    assert "wt-1" not in rows.rows
    assert _git(repo, "branch", "--list", "task-branch") == ""


async def test_recovery_refuses_a_modified_tracked_file_and_logs_the_worktree(
    landed: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    repo, worktree = landed
    git = WorktreeGitManager(repo)
    (worktree / "tracked.txt").write_text("edited\n")
    rows = _Rows(_row("wt-1", worktree, status="deleting"))

    with caplog.at_level(logging.WARNING):
        results = await _recover(git, rows)

    assert [result.success for result in results] == [False]
    assert results[0].uncommitted_changes is True
    assert (worktree / "tracked.txt").read_text() == "edited\n"
    assert rows.rows["wt-1"].status == "deleting"
    assert "wt-1" in caplog.text
    assert "recovery refused" in caplog.text


async def test_cancelled_before_mutation_restores_the_prior_status(
    landed: tuple[Path, Path],
) -> None:
    repo, worktree = landed
    git = WorktreeGitManager(repo)
    rows = _Rows(_row("wt-1", worktree, status="merged"))
    boundary = DestructiveBoundary()
    assert boundary.cancel_before_mutation()

    result = await _transaction(git, rows, _request(DeletionSurface.TEARDOWN), boundary)

    assert result.abandoned is True
    assert worktree.exists()
    assert rows.rows["wt-1"].status == "merged"


async def test_missing_checkout_probe_refusal_restores_the_prior_status(
    landed: tuple[Path, Path], tmp_path: Path
) -> None:
    repo, _worktree = landed
    git = WorktreeGitManager(repo)
    rows = _Rows(_row("wt-1", tmp_path / "never-created", status="merged", branch=None))

    result = await _transaction(git, rows, _request(DeletionSurface.TEARDOWN, merged_into=None))

    assert result.success is False
    assert result.error == "Cannot verify missing worktree without its source branch"
    assert rows.rows["wt-1"].status == "merged"


async def test_git_failure_after_the_git_call_keeps_the_record_deleting(
    landed: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, worktree = landed
    git = WorktreeGitManager(repo)
    refused = SimpleNamespace(success=False, message="git refused the removal", error="git_refused")
    monkeypatch.setattr(git, "delete_worktree", AsyncMock(return_value=refused))
    rows = _Rows(_row("wt-1", worktree, status="merged"))

    result = await _transaction(git, rows, _request(DeletionSurface.TEARDOWN))

    assert result.success is False
    assert worktree.exists()
    assert rows.rows["wt-1"].status == "deleting"


async def test_recovery_is_stop_aware_idempotent_and_leaves_active_rows_alone(
    landed: tuple[Path, Path], tmp_path: Path
) -> None:
    repo, worktree = landed
    git = WorktreeGitManager(repo)
    active = _row("wt-2", tmp_path / "other", branch="other-branch")
    rows = _Rows(_row("wt-1", worktree, status="deleting"), active)

    assert await _recover(git, rows, should_stop=lambda: True) == []
    assert "wt-1" in rows.rows

    results = await _recover(git, rows)

    assert [result.success for result in results] == [True]
    assert set(rows.rows) == {"wt-2"}
    assert await _recover(git, rows) == []

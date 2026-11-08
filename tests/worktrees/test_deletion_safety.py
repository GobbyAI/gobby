"""Safety guarantees of the shared worktree deletion transaction (#25840).

One deletion mutates a tree at a time, a removal is reported done only once Git and the
filesystem confirm it, and a daemon surface never forces Git to delete a dirty tree.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager, Worktree
from gobby.utils import daemon_git
from gobby.worktrees.deletion import (
    DeletionSurface,
    WorktreeDeletionRequest,
    delete_worktree_transaction,
)
from gobby.worktrees.executor import DestructiveBoundary
from gobby.worktrees.git import WorktreeGitManager
from gobby.worktrees.git._lifecycle import delete_worktree
from gobby.worktrees.git._runner import GitRunner

pytestmark = pytest.mark.unit

WORKTREE_ID = "wt-25840"
BRANCH = "task-25840"


class _Artifacts:
    def clear_worktree_references(self, _worktree_id: str) -> int:
        return 0


class _Storage:
    """Worktree record store with the cleanup-lock surface the daemon surfaces use."""

    def __init__(self, row: Worktree, *, delete_error: Exception | None = None) -> None:
        self.rows: dict[str, Worktree] = {row.id: row}
        self.delete_error = delete_error
        self.db = SimpleNamespace(fetchall=lambda _sql, _params: [])

    def get(self, worktree_id: str) -> Worktree | None:
        return self.rows.get(worktree_id)

    def delete(self, worktree_id: str) -> bool:
        if self.delete_error is not None:
            raise self.delete_error
        return self.rows.pop(worktree_id, None) is not None

    @contextlib.contextmanager
    def lock_for_cleanup(
        self, worktree_id: str, *, expired_only: bool = True
    ) -> Iterator[Worktree | None]:
        yield self.rows.get(worktree_id)


class _Git:
    """Fake Git whose non-forced removal refuses any tree with pending changes.

    Status is the explicit ``tracked_lines`` plus every untracked file on disk, so the
    porcelain output follows the filesystem the way ``git status`` does.
    """

    def __init__(
        self,
        tree: Path,
        *,
        tracked_lines: tuple[str, ...] = (),
        lie_about_removal: bool = False,
        gate: asyncio.Event | None = None,
        unreaped: asyncio.Future[None] | None = None,
    ) -> None:
        self.tree = tree
        self.repo_path = tree.parent
        self.tracked_lines = tracked_lines
        self.lie_about_removal = lie_about_removal
        self.gate = gate
        self.unreaped = unreaped
        self.started = asyncio.Event()
        self.status_calls = 0
        self.delete_calls = 0
        self.force_calls: list[bool] = []

    def porcelain(self) -> str:
        if not self.tree.exists():
            return ""
        untracked = [
            f"?? {child.name}" for child in sorted(self.tree.iterdir()) if child.name != "README.md"
        ]
        lines = [*self.tracked_lines, *untracked]
        return "".join(f"{line}\n" for line in lines)

    async def get_worktree_status(self, _path: str) -> SimpleNamespace:
        self.status_calls += 1
        return SimpleNamespace(
            branch=BRANCH,
            has_uncommitted_changes=bool(self.porcelain().strip()),
        )

    async def run_git_command(
        self, args: list[str], cwd: str | None = None, timeout: int | None = None
    ) -> SimpleNamespace:
        if args[:2] == ["status", "--porcelain"]:
            return SimpleNamespace(returncode=0, stdout=self.porcelain(), stderr="")
        if args[:1] == ["show-ref"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if args[:2] == ["worktree", "list"]:
            return SimpleNamespace(returncode=0, stdout=f"worktree {self.repo_path}\0", stderr="")
        raise AssertionError(f"unexpected git call: {args}")

    async def delete_worktree(self, path: str, **kwargs: Any) -> SimpleNamespace:
        self.delete_calls += 1
        if self.unreaped is not None and not self.unreaped.done():
            # The Git child ignored its kill: the Git layer reports it, then the call times out.
            daemon_git.note_unreaped_child(self.unreaped)
            return SimpleNamespace(
                success=False, message="Git command timed out", error=None, output=""
            )
        force = bool(kwargs.get("force", False))
        self.force_calls.append(force)
        self.started.set()
        if self.gate is not None:
            await asyncio.wait_for(self.gate.wait(), timeout=5)
        if not force and self.porcelain().strip():
            return SimpleNamespace(
                success=False,
                message="contains modified or untracked files, use --force to delete it",
                error="worktree_dirty",
                output="",
            )
        if not self.lie_about_removal:
            _remove_tree(Path(path))
        return SimpleNamespace(success=True, message="Deleted", error=None, output="")


def _remove_tree(path: Path) -> None:
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_dir() and not child.is_symlink():
            child.rmdir()
        else:
            child.unlink()
    path.rmdir()


def _worktree(path: Path) -> Worktree:
    return Worktree(
        id=WORKTREE_ID,
        project_id="project-1",
        branch_name=BRANCH,
        worktree_path=str(path),
        base_branch="main",
        status="active",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        task_id=WORKTREE_ID,
        agent_session_id=None,
        merged_at=None,
    )


def _tree(tmp_path: Path) -> Path:
    tree = tmp_path / "wt-25840"
    tree.mkdir()
    (tree / "README.md").write_text("tracked\n")
    return tree


async def _delete(
    surface: DeletionSurface,
    storage: _Storage,
    git: _Git | None,
    *,
    boundary: DestructiveBoundary | None = None,
) -> Any:
    return await delete_worktree_transaction(
        boundary or DestructiveBoundary(),
        request=WorktreeDeletionRequest(WORKTREE_ID, surface),
        worktree_storage=cast(LocalWorktreeManager, storage),
        resolve_git_manager=lambda _worktree: cast(WorktreeGitManager | None, git),
        task_manager=cast(LocalTaskManager, SimpleNamespace(artifacts=_Artifacts())),
    )


@pytest.mark.asyncio
async def test_second_deletion_is_refused_while_one_is_in_flight(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    gate = asyncio.Event()
    git = _Git(tree, gate=gate)
    storage = _Storage(_worktree(tree))

    first = asyncio.create_task(_delete(DeletionSurface.MCP, storage, git))
    await asyncio.wait_for(git.started.wait(), timeout=5)
    status_calls_before = git.status_calls

    second = await _delete(DeletionSurface.TEARDOWN, storage, git)

    assert second.success is False
    assert second.error_code == "deletion_in_progress"
    assert second.abandoned is True
    assert git.delete_calls == 1
    assert git.status_calls == status_calls_before
    assert tree.is_dir()
    assert WORKTREE_ID in storage.rows

    gate.set()
    done = await asyncio.wait_for(first, timeout=5)
    assert done.success is True
    assert git.force_calls == [False]
    assert not tree.exists()
    assert WORKTREE_ID not in storage.rows


@pytest.mark.asyncio
async def test_refused_non_force_delete_leaves_tree_branch_and_row_unchanged(
    tmp_path: Path,
) -> None:
    tree = _tree(tmp_path)
    (tree / "README.md").write_text("edited, uncommitted\n")
    git = _Git(tree, tracked_lines=(" M README.md",))
    storage = _Storage(_worktree(tree))

    result = await _delete(DeletionSurface.MCP, storage, git)

    assert result.success is False
    assert result.uncommitted_changes is True
    assert git.delete_calls == 0
    assert (tree / "README.md").read_text() == "edited, uncommitted\n"
    assert WORKTREE_ID in storage.rows


@pytest.mark.asyncio
async def test_reported_removal_is_verified_against_the_filesystem(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    git = _Git(tree, lie_about_removal=True)
    storage = _Storage(_worktree(tree))

    result = await _delete(DeletionSurface.MCP, storage, git)

    assert result.success is False
    assert result.error_code == "worktree_removal_unverified"
    assert tree.is_dir()
    assert WORKTREE_ID in storage.rows


@pytest.mark.asyncio
async def test_record_delete_failure_after_git_removal_is_structured(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    git = _Git(tree)
    storage = _Storage(
        _worktree(tree),
        delete_error=RuntimeError("canceling statement due to lock timeout"),
    )

    result = await _delete(DeletionSurface.TEARDOWN, storage, git)

    assert result.success is False
    assert result.git_deleted is True
    assert result.error_code == "worktree_record_delete_failed"
    assert result.abandoned is True
    assert not tree.exists()
    assert WORKTREE_ID in storage.rows


@pytest.mark.asyncio
async def test_teardown_clears_generated_untracked_files_and_never_forces(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    (tree / ".mcp.json").write_text("{}\n")
    git = _Git(tree)
    storage = _Storage(_worktree(tree))

    result = await _delete(DeletionSurface.TEARDOWN, storage, git)

    assert result.success is True
    assert git.force_calls == [False]
    assert not tree.exists()
    assert WORKTREE_ID not in storage.rows


@pytest.mark.asyncio
async def test_teardown_refuses_tracked_change_before_any_removal(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    (tree / "README.md").write_text("edited, uncommitted\n")
    git = _Git(tree, tracked_lines=(" M README.md",))
    storage = _Storage(_worktree(tree))

    result = await _delete(DeletionSurface.TEARDOWN, storage, git)

    assert result.success is False
    assert result.uncommitted_changes is True
    assert git.delete_calls == 0
    assert (tree / "README.md").read_text() == "edited, uncommitted\n"
    assert WORKTREE_ID in storage.rows


@pytest.mark.asyncio
async def test_teardown_refuses_tracked_change_under_a_generated_path(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    (tree / ".mcp.json").write_text('{"edited": true}\n')
    git = _Git(tree, tracked_lines=(" M .mcp.json",))
    storage = _Storage(_worktree(tree))

    result = await _delete(DeletionSurface.TEARDOWN, storage, git)

    assert result.success is False
    assert result.uncommitted_changes is True
    assert git.delete_calls == 0
    assert (tree / ".mcp.json").read_text() == '{"edited": true}\n'
    assert WORKTREE_ID in storage.rows


@pytest.mark.asyncio
async def test_http_delete_refuses_tracked_change_without_forcing(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    (tree / "README.md").write_text("edited, uncommitted\n")
    git = _Git(tree, tracked_lines=(" M README.md",))
    storage = _Storage(_worktree(tree))

    result = await _delete(DeletionSurface.HTTP, storage, git)

    assert result.success is False
    assert result.uncommitted_changes is True
    assert result.git_deleted is False
    assert git.force_calls == []
    assert git.delete_calls == 0
    assert (tree / "README.md").read_text() == "edited, uncommitted\n"
    assert WORKTREE_ID in storage.rows


@pytest.mark.asyncio
async def test_http_refusal_text_does_not_advertise_force(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    (tree / "README.md").write_text("edited, uncommitted\n")
    git = _Git(tree, tracked_lines=(" M README.md",))
    storage = _Storage(_worktree(tree))

    result = await _delete(DeletionSurface.HTTP, storage, git)

    assert result.uncommitted_changes is True
    assert result.error is not None
    assert "uncommitted changes" in result.error
    # HTTP callers cannot pass force, so the refusal must not point them at it.
    assert "force" not in result.error.lower()


@pytest.mark.asyncio
async def test_missing_tree_without_git_manager_refuses_and_keeps_stale_row(
    tmp_path: Path,
) -> None:
    storage = _Storage(_worktree(tmp_path / "wt-25840-gone"))

    result = await _delete(DeletionSurface.MCP, storage, None)

    assert result.success is False
    assert result.error_code == "worktree_git_unavailable"
    assert result.git_deleted is False
    assert WORKTREE_ID in storage.rows


@pytest.mark.asyncio
async def test_unreaped_git_child_holds_the_claim_until_it_exits(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    settled: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    git = _Git(tree, unreaped=settled)
    storage = _Storage(_worktree(tree))

    refused = await _delete(DeletionSurface.MCP, storage, git)

    assert refused.success is False
    assert refused.error_code == "worktree_in_use"
    assert refused.git_deleted is False
    assert tree.is_dir()
    assert WORKTREE_ID in storage.rows

    held = await _delete(DeletionSurface.TEARDOWN, storage, git)
    assert held.error_code == "deletion_in_progress"
    assert git.delete_calls == 1

    settled.set_result(None)
    retried: Any = None
    for _ in range(100):
        retried = await _delete(DeletionSurface.MCP, storage, git)
        if retried.error_code != "deletion_in_progress":
            break
        await asyncio.sleep(0)

    assert retried.success is True
    assert not tree.exists()
    assert WORKTREE_ID not in storage.rows


@pytest.mark.unit
async def test_remove_stall_reports_its_command_and_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `git worktree remove` that outlives its budget is named by its argv and pid.

    The refusal keeps the tree, so a stall that ends a test run is attributable from the
    refusal text alone rather than from a thread dump.
    """
    tree = tmp_path / "tree"
    tree.mkdir()
    argv = ("git", "worktree", "remove", str(tree))
    stalled = daemon_git.GitTimeout(
        "timeout", argv, 240.0, stderr="Git timed out: phase=running pid=4242 elapsed_seconds=240"
    )
    monkeypatch.setattr(daemon_git.daemon_git, "run", AsyncMock(return_value=stalled))

    result = await delete_worktree(GitRunner(str(tmp_path)), tree)

    assert result.success is False
    assert result.error is not None
    assert "worktree remove" in result.error
    assert "pid=4242" in result.error
    assert tree.is_dir()

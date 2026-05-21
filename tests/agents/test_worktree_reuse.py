"""Tests for reused agent worktree synchronization."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gobby.agents.worktree_reuse import sync_reused_worktree_to_base

pytestmark = pytest.mark.unit


class FakeGitManager:
    def __init__(self, responses: list[subprocess.CompletedProcess[str]]) -> None:
        self.responses = responses
        self.calls: list[tuple[list[str], str | Path | None, int]] = []

    def run_git_command(
        self,
        args: list[str],
        *,
        cwd: str | Path | None = None,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, cwd, timeout))
        return self.responses.pop(0)


def _result(
    args: list[str],
    returncode: int,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=args, returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.mark.asyncio
async def test_sync_reused_worktree_rebases_when_base_is_not_ancestor(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    git = FakeGitManager(
        [
            _result(["git", "rev-parse"], 0, stdout="base-sha\n"),
            _result(["git", "status"], 0),
            _result(["git", "merge-base"], 1),
            _result(["git", "rebase"], 0),
        ]
    )

    result = await sync_reused_worktree_to_base(
        git_manager=git,
        worktree_path=str(worktree),
        base_branch="0.4.7",
    )

    assert result.status == "rebased"
    assert result.base_ref == "0.4.7"
    assert git.calls[3][0] == ["rebase", "0.4.7"]
    assert git.calls[3][1] == worktree
    assert git.calls[3][2] == 120


@pytest.mark.asyncio
async def test_sync_reused_worktree_rejects_dirty_worktree(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    git = FakeGitManager(
        [
            _result(["git", "rev-parse"], 0, stdout="base-sha\n"),
            _result(["git", "status"], 0, stdout=" M src/app.py\n"),
        ]
    )

    with pytest.raises(RuntimeError, match="uncommitted changes"):
        await sync_reused_worktree_to_base(
            git_manager=git,
            worktree_path=str(worktree),
            base_branch="0.4.7",
        )

    assert len(git.calls) == 2


@pytest.mark.asyncio
async def test_sync_reused_worktree_aborts_failed_rebase(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    git = FakeGitManager(
        [
            _result(["git", "rev-parse"], 0, stdout="base-sha\n"),
            _result(["git", "status"], 0),
            _result(["git", "merge-base"], 1),
            _result(["git", "rebase"], 1, stderr="CONFLICT\n"),
            _result(["git", "rebase", "--abort"], 0),
        ]
    )

    with pytest.raises(RuntimeError, match="rebase aborted"):
        await sync_reused_worktree_to_base(
            git_manager=git,
            worktree_path=str(worktree),
            base_branch="0.4.7",
        )

    assert git.calls[-1][0] == ["rebase", "--abort"]

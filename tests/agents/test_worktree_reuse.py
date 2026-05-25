"""Tests for reused agent worktree synchronization."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gobby.agents.worktree_reuse import (
    ReusedWorktreeRebaseConflict,
    sync_reused_worktree_to_base,
)
from gobby.worktrees.git import WorktreeGitManager


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


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.asyncio
@pytest.mark.unit
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
@pytest.mark.unit
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
@pytest.mark.integration
async def test_sync_reused_worktree_allows_generated_isolation_metadata(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    worktree = tmp_path / "worktree"
    parent.mkdir()
    (parent / ".gobby").mkdir()
    (parent / ".gobby" / "project.json").write_text(
        '{"id":"proj-1","name":"parent"}\n',
        encoding="utf-8",
    )
    (parent / "app.py").write_text("print('base')\n", encoding="utf-8")
    _git(parent, "init", "-b", "main")
    _git(parent, "config", "user.email", "test@example.com")
    _git(parent, "config", "user.name", "Test User")
    _git(parent, "add", ".gobby/project.json", "app.py")
    _git(parent, "commit", "-m", "initial")
    base_sha = _git(parent, "rev-parse", "main").stdout.strip()
    _git(parent, "worktree", "add", "-b", "reuse", str(worktree), "main")

    (worktree / ".gobby" / "project.json").write_text(
        (
            '{"id":"proj-1","name":"parent",'
            f'"parent_project_path":"{parent.resolve()}",'
            '"parent_project_id":"proj-1"}\n'
        ),
        encoding="utf-8",
    )
    (worktree / ".mcp.json").write_text('{"mcpServers":{}}\n', encoding="utf-8")

    result = await sync_reused_worktree_to_base(
        git_manager=WorktreeGitManager(parent),
        worktree_path=str(worktree),
        base_branch="main",
    )

    assert result.base_commit_sha == base_sha
    assert _git(worktree, "status", "--porcelain").stdout == ""
    exclude_path = (
        worktree / _git(worktree, "rev-parse", "--git-path", "info/exclude").stdout.strip()
    )
    assert ".mcp.json" in exclude_path.read_text(encoding="utf-8")
    ls_files = _git(worktree, "ls-files", "-v", ".gobby/project.json").stdout
    assert ls_files.startswith("S ")
    assert "parent_project_path" in (worktree / ".gobby" / "project.json").read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sync_reused_worktree_rejects_non_generated_project_metadata(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    worktree = tmp_path / "worktree"
    parent.mkdir()
    (parent / ".gobby").mkdir()
    (parent / ".gobby" / "project.json").write_text(
        '{"id":"proj-1","name":"parent"}\n',
        encoding="utf-8",
    )
    _git(parent, "init", "-b", "main")
    _git(parent, "config", "user.email", "test@example.com")
    _git(parent, "config", "user.name", "Test User")
    _git(parent, "add", ".gobby/project.json")
    _git(parent, "commit", "-m", "initial")
    _git(parent, "worktree", "add", "-b", "reuse", str(worktree), "main")
    (worktree / ".gobby" / "project.json").write_text(
        '{"id":"proj-1","name":"edited"}\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="uncommitted changes"):
        await sync_reused_worktree_to_base(
            git_manager=WorktreeGitManager(parent),
            worktree_path=str(worktree),
            base_branch="main",
        )


@pytest.mark.asyncio
@pytest.mark.unit
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

    with pytest.raises(ReusedWorktreeRebaseConflict, match="rebase aborted"):
        await sync_reused_worktree_to_base(
            git_manager=git,
            worktree_path=str(worktree),
            base_branch="0.4.7",
        )

    assert git.calls[-1][0] == ["rebase", "--abort"]

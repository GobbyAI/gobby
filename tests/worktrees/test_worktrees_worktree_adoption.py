from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.worktrees.git._models import WorktreeInfo
from gobby.worktrees.git.manager import WorktreeGitManager


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("root\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


@pytest.mark.parametrize("detached", [False, True])
def test_inspect_linked_worktree_returns_real_branch_or_none(
    git_repo: Path,
    tmp_path: Path,
    detached: bool,
) -> None:
    linked = tmp_path / ("detached" if detached else "branch")
    args = ["worktree", "add"]
    if detached:
        args.extend(["--detach", str(linked), "HEAD"])
    else:
        args.extend(["-b", "feature/adopt", str(linked)])
    _git(git_repo, *args)

    info = WorktreeGitManager(git_repo).inspect_worktree(linked)

    assert info.path == str(linked.resolve())
    assert info.branch is None if detached else info.branch == "feature/adopt"
    assert info.is_detached is detached
    assert info.commit


def test_inspect_linked_worktree_rejects_primary_and_unlinked_paths(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    manager = WorktreeGitManager(git_repo)
    unlinked = tmp_path / "unlinked"
    unlinked.mkdir()

    with pytest.raises(ValueError, match="Primary checkout"):
        manager.inspect_worktree(git_repo)
    with pytest.raises(ValueError, match="not a linked worktree"):
        manager.inspect_worktree(unlinked)


@pytest.mark.parametrize(
    ("field", "message"),
    [("is_bare", "Bare worktree"), ("prunable", "Prunable worktree")],
)
def test_inspect_linked_worktree_rejects_unadoptable_metadata(
    tmp_path: Path,
    field: str,
    message: str,
) -> None:
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    primary.mkdir()
    linked.mkdir()
    manager = WorktreeGitManager(primary)
    primary_info = WorktreeInfo(path=str(primary), branch="main", commit="abc")
    linked_info = WorktreeInfo(
        path=str(linked),
        branch="feature/adopt",
        commit="def",
        is_bare=field == "is_bare",
        prunable=field == "prunable",
    )

    with patch(
        "gobby.worktrees.git._lifecycle.list_worktrees",
        return_value=[primary_info, linked_info],
    ):
        with pytest.raises(ValueError, match=message):
            manager.inspect_worktree(linked)

"""Worktree forks preserve the selected commit, independently of remote state."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.worktrees.creation import WorktreeCreationResult, create_worktree
from gobby.worktrees.git import WorktreeGitManager


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Fork Test")
    git(root, "commit", "--allow-empty", "-m", "initial")
    return root


async def fork(repo: Path, path: Path, base: str | None = None) -> WorktreeCreationResult:
    storage = MagicMock()
    storage.get_by_branch.return_value = None
    storage.create.side_effect = lambda **values: SimpleNamespace(id="fork", **values)
    return await create_worktree(
        git_manager=WorktreeGitManager(repo),
        worktree_storage=storage,
        project_id="project",
        branch_name=path.name,
        worktree_path=str(path),
        base_branch=base,
        sidecar_writer=AsyncMock(),
        event_emitter=MagicMock(return_value={}),
    )


async def test_default_uses_caller_head_without_fetch(repo: Path, tmp_path: Path) -> None:
    git(repo, "switch", "-c", "caller")
    git(repo, "commit", "--allow-empty", "-m", "unpushed caller commit")
    selected = git(repo, "rev-parse", "HEAD")
    # No remote exists: default creation must still succeed from local HEAD.
    child = tmp_path / "default-fork"
    result = await fork(repo, child)
    assert result.success, result.error
    assert git(child, "rev-parse", "HEAD") == selected
    assert result.worktree is not None
    assert result.worktree.base_branch == "caller"


@pytest.mark.parametrize(
    "base", ["main", "refs/heads/main", "origin/main", "refs/remotes/origin/main"]
)
async def test_explicit_refs_select_commit(repo: Path, tmp_path: Path, base: str) -> None:
    git(repo, "tag", "origin/main")
    git(repo, "tag", "main")
    git(repo, "commit", "--allow-empty", "-m", "branch advances past tags")
    selected = git(repo, "rev-parse", "refs/heads/main")
    git(repo, "remote", "add", "origin", str(repo))
    git(repo, "switch", "-c", "caller")
    git(repo, "commit", "--allow-empty", "-m", "caller advances")
    child = tmp_path / "explicit-fork"
    result = await fork(repo, child, base)
    assert result.success, result.error
    assert git(child, "rev-parse", "HEAD") == selected
    assert result.worktree is not None
    assert result.worktree.base_branch == "main"


async def test_existing_branch_mismatch_is_rejected(repo: Path, tmp_path: Path) -> None:
    git(repo, "branch", "existing")
    git(repo, "commit", "--allow-empty", "-m", "selected advances")
    selected = git(repo, "rev-parse", "HEAD")
    child = tmp_path / "existing-fork"
    result = await WorktreeGitManager(repo).create_worktree(
        child, "existing", base_branch="main", base_commit_sha=selected, create_branch=False
    )
    assert not result.success
    assert not child.exists()
    assert result.error == "existing_branch_commit_mismatch"


async def test_detached_default_has_no_side_effects(repo: Path, tmp_path: Path) -> None:
    git(repo, "switch", "--detach", "HEAD")
    child = tmp_path / "missing" / "detached-fork"
    result = await fork(repo, child)
    assert not result.success
    assert result.error_code == "detached_head_requires_base"
    assert not child.parent.exists()

"""Worktree forks preserve the selected commit, independently of remote state."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.worktrees.creation import WorktreeCreationResult, create_worktree
from gobby.worktrees.git import GitOperationResult, WorktreeGitManager


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
    with patch.object(
        WorktreeGitManager,
        "run_git_command",
        autospec=True,
        side_effect=WorktreeGitManager.run_git_command,
    ) as run_git:
        result = await fork(repo, child, base)
    fetches = [call.args[1] for call in run_git.call_args_list if call.args[1][0] == "fetch"]
    assert fetches == (
        [["fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"]]
        if base.startswith(("origin/", "refs/remotes/origin/"))
        else []
    )
    assert result.success, result.error
    assert git(child, "rev-parse", "HEAD") == selected
    assert result.worktree is not None
    assert result.worktree.base_branch == "main"


@pytest.mark.parametrize("phase", ["branch", "check-ref-format", "fetch"])
async def test_base_selection_timeout_returns_structured_error(
    repo: Path, tmp_path: Path, phase: str
) -> None:
    manager = WorktreeGitManager(repo)
    child = tmp_path / "timeout-fork"
    storage = MagicMock()
    command = ["git", phase]
    failure = subprocess.TimeoutExpired(command, 5)
    outcomes = (
        [subprocess.CompletedProcess(["git", "check-ref-format"], 0, "", ""), failure]
        if phase == "fetch"
        else [failure]
    )
    base = None if phase == "branch" else "origin/main"
    with patch.object(manager, "_run_git", side_effect=outcomes):
        result = await create_worktree(
            git_manager=manager,
            worktree_storage=storage,
            project_id="project",
            branch_name=child.name,
            worktree_path=str(child),
            base_branch=base,
        )
    assert not result.success
    assert result.error_code == "base_selection_timeout"
    assert result.error is not None and "timed out" in result.error.lower()
    assert result.worktree is None
    assert not child.exists()
    assert git(repo, "branch", "--list", child.name) == ""
    storage.get_by_branch.assert_not_called()
    storage.create.assert_not_called()


async def test_existing_branch_mismatch_is_rejected(repo: Path, tmp_path: Path) -> None:
    git(repo, "branch", "existing")
    git(repo, "commit", "--allow-empty", "-m", "selected advances")
    selected = git(repo, "rev-parse", "HEAD")
    child = tmp_path / "existing-fork"
    result = await WorktreeGitManager(repo).create_worktree(
        child, "existing", base_branch="main", base_commit_sha=selected, create_branch=True
    )
    assert not result.success
    assert not child.exists()
    assert result.error == "existing_branch_commit_mismatch"


async def test_existing_branch_checkout_preserves_tip_and_merge_base(
    repo: Path, tmp_path: Path
) -> None:
    git(repo, "checkout", "-b", "existing")
    git(repo, "commit", "--allow-empty", "-m", "existing work")
    existing_tip = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    base_tip = git(repo, "rev-parse", "HEAD")
    child = tmp_path / "existing-checkout"
    storage = MagicMock()
    storage.get_by_branch.return_value = None
    storage.create.side_effect = lambda **values: SimpleNamespace(id="existing", **values)
    result = await create_worktree(
        git_manager=WorktreeGitManager(repo),
        worktree_storage=storage,
        project_id="project",
        branch_name="existing",
        base_branch="main",
        worktree_path=str(child),
        create_branch=False,
        sidecar_writer=AsyncMock(),
        event_emitter=MagicMock(return_value={}),
    )
    assert result.success, result.error
    assert result.worktree is not None and result.worktree.base_branch == "main"
    assert git(child, "rev-parse", "HEAD") == existing_tip
    assert git(repo, "rev-parse", "main") == base_tip


async def test_existing_checkout_refuses_branch_movement_after_selection(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(repo, "branch", "existing")
    selected = git(repo, "rev-parse", "existing")
    child = tmp_path / "raced-existing"
    manager = WorktreeGitManager(repo)
    original_create = manager.create_worktree

    async def move_branch_then_create(
        *,
        worktree_path: str | Path,
        branch_name: str,
        base_branch: str,
        base_commit_sha: str,
        create_branch: bool = True,
    ) -> GitOperationResult:
        git(repo, "checkout", "existing")
        git(repo, "commit", "--allow-empty", "-m", "concurrent task work")
        git(repo, "checkout", "main")
        return await original_create(
            worktree_path, branch_name, base_branch, base_commit_sha, create_branch
        )

    monkeypatch.setattr(manager, "create_worktree", move_branch_then_create)
    storage = MagicMock()
    storage.get_by_branch.return_value = None
    result = await create_worktree(
        git_manager=manager,
        worktree_storage=storage,
        project_id="project",
        branch_name="existing",
        base_branch="main",
        worktree_path=str(child),
        create_branch=False,
    )
    assert not result.success and result.error == "existing_branch_commit_mismatch"
    assert git(repo, "rev-parse", "existing") != selected
    assert not child.exists()
    storage.create.assert_not_called()


@pytest.mark.parametrize("base", ["existing", "refs/heads/existing"])
async def test_self_base_is_rejected_before_creation(repo: Path, tmp_path: Path, base: str) -> None:
    git(repo, "branch", "existing")
    child = tmp_path / "self-base"
    storage = MagicMock()
    result = await create_worktree(
        git_manager=WorktreeGitManager(repo),
        worktree_storage=storage,
        project_id="project",
        branch_name="existing",
        base_branch=base,
        worktree_path=str(child),
        create_branch=False,
    )
    assert not result.success
    assert result.error_code == "merge_target_is_source_branch"
    assert not child.exists()
    storage.get_by_branch.assert_not_called()
    storage.create.assert_not_called()


async def test_detached_default_has_no_side_effects(repo: Path, tmp_path: Path) -> None:
    git(repo, "switch", "--detach", "HEAD")
    child = tmp_path / "missing" / "detached-fork"
    result = await fork(repo, child)
    assert not result.success
    assert result.error_code == "detached_head_requires_base"
    assert not child.parent.exists()

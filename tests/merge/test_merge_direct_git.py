from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "update-ref", "refs/remotes/origin/main", "main")


def _create_feature_worktree(repo: Path, tmp_path: Path, branch: str) -> tuple[Path, str]:
    worktree_path = tmp_path / "feature-worktree"
    _git(repo, "worktree", "add", "-b", branch, str(worktree_path), "main")
    (worktree_path / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(worktree_path, "add", "feature.txt")
    _git(worktree_path, "commit", "-m", "feature")
    return worktree_path, _git(worktree_path, "rev-parse", "HEAD")


def _create_registry(temp_db, repo: Path, worktree_path: Path, branch: str):
    from gobby.mcp_proxy.tools.merge import create_merge_registry
    from gobby.storage.merge_resolutions import MergeResolutionManager
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.worktrees import LocalWorktreeManager
    from gobby.worktrees.git import WorktreeGitManager
    from gobby.worktrees.merge import MergeResolver

    project = LocalProjectManager(temp_db).create("merge-direct", repo_path=str(repo))
    worktree_manager = LocalWorktreeManager(temp_db)
    worktree = worktree_manager.create(
        project_id=project.id,
        branch_name=branch,
        worktree_path=str(worktree_path),
        base_branch="main",
    )
    merge_storage = MergeResolutionManager(temp_db)
    registry = create_merge_registry(
        merge_storage=merge_storage,
        merge_resolver=MergeResolver(),
        git_manager=WorktreeGitManager(repo),
        worktree_manager=worktree_manager,
    )
    return registry, merge_storage, worktree


@pytest.mark.asyncio
async def test_merge_apply_fast_forwards_reused_clean_resolution(temp_db, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    branch = "feature/direct-ff"
    _init_repo(repo)
    worktree_path, feature_sha = _create_feature_worktree(repo, tmp_path, branch)
    registry, _merge_storage, worktree = _create_registry(temp_db, repo, worktree_path, branch)

    first = await registry.call(
        "merge_start",
        {"worktree_id": worktree.id, "source_branch": branch, "target_branch": "main"},
    )
    assert first["success"] is True

    reused = await registry.call(
        "merge_start",
        {"worktree_id": worktree.id, "source_branch": branch, "target_branch": "main"},
    )
    assert reused["success"] is True
    assert reused["reused_resolution"] is True

    result = await registry.call("merge_apply", {"resolution_id": reused["resolution_id"]})

    assert result["success"] is True
    assert result["direct_merge"] is True
    assert result["merge_strategy"] == "ff-only"
    assert result["merge_sha"] == feature_sha
    assert result["commit_sha"] == feature_sha
    assert _git(repo, "rev-parse", "main") == feature_sha
    assert _git(repo, "rev-list", "--count", "main") == "2"


@pytest.mark.asyncio
async def test_no_ff_strategy_bypasses_reuse_and_creates_merge_commit(
    temp_db,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    branch = "feature/direct-no-ff"
    _init_repo(repo)
    worktree_path, feature_sha = _create_feature_worktree(repo, tmp_path, branch)
    registry, merge_storage, worktree = _create_registry(temp_db, repo, worktree_path, branch)

    first = await registry.call(
        "merge_start",
        {"worktree_id": worktree.id, "source_branch": branch, "target_branch": "main"},
    )
    assert first["success"] is True

    no_ff = await registry.call(
        "merge_start",
        {
            "worktree_id": worktree.id,
            "source_branch": branch,
            "target_branch": "main",
            "strategy": "no-ff",
        },
    )
    assert no_ff["success"] is True
    assert "reused_resolution" not in no_ff
    assert no_ff["tier"] == "git_no_ff"
    assert merge_storage.get_resolution(no_ff["resolution_id"]).tier_used == "git_no_ff"

    result = await registry.call("merge_apply", {"resolution_id": no_ff["resolution_id"]})

    assert result["success"] is True
    assert result["direct_merge"] is True
    assert result["merge_strategy"] == "no-ff"
    assert result["merge_sha"] == _git(repo, "rev-parse", "main")
    assert result["merge_sha"] != feature_sha
    assert _git(repo, "rev-parse", "main^2") == feature_sha
    assert _git(repo, "rev-list", "--count", "main") == "3"

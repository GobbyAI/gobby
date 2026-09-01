"""Real-repository coverage for merge_worktree dirty-target landing."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry
from gobby.worktrees.git import WorktreeGitManager
from tests.mcp_proxy.tools.test_merge_landscape import _commit_file, _init_git_repo

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


def _repo_with_feature(tmp_path: Path) -> tuple[Path, Path, WorktreeGitManager, MagicMock]:
    repo = tmp_path / "repo"
    source_path = tmp_path / "feature-worktree"
    repo.mkdir()
    _init_git_repo(repo)
    _commit_file(repo, "base.txt", "base\n")
    (repo / ".gobby").mkdir()
    _commit_file(repo, ".gobby/project.json", "{}\n")
    _git(repo, "branch", "-M", "main")
    _git(repo, "worktree", "add", "-b", "feature/path", str(source_path), "main")
    _commit_file(source_path, "feature.txt", "feature\n")

    git_manager = WorktreeGitManager(repo)
    worktree = MagicMock(
        worktree_path=str(source_path),
        branch_name="feature/path",
        base_branch="main",
        status="active",
    )
    ctx = MagicMock(
        git_manager=git_manager,
        project_id="test-project",
    )
    ctx.resolve_worktree_id.side_effect = lambda ref: ref
    ctx.worktree_storage.get.return_value = worktree
    return repo, source_path, git_manager, ctx


async def _merge(ctx: MagicMock, git_manager: WorktreeGitManager) -> dict[str, object]:
    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")
    assert merge_tool is not None
    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(git_manager, "test-project", None),
    ):
        return cast(dict[str, object], await merge_tool("wt-real"))


@pytest.mark.asyncio
async def test_unrelated_staged_target_file_lands_by_fast_forward(tmp_path: Path) -> None:
    repo, _, git_manager, ctx = _repo_with_feature(tmp_path)
    (repo / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")

    result = await _merge(ctx, git_manager)

    assert result["success"] is True
    assert result["landing"] == "fast-forward"
    assert result["merge_sha"] == _git(repo, "rev-parse", "refs/heads/main")
    assert _git(repo, "diff", "--cached", "--name-only") == "staged.txt"
    assert (repo / "staged.txt").read_text(encoding="utf-8") == "staged\n"
    ctx.worktree_storage.mark_merged.assert_called_once_with("wt-real")


@pytest.mark.asyncio
async def test_overlapping_staged_target_file_is_rejected(tmp_path: Path) -> None:
    repo, _, git_manager, ctx = _repo_with_feature(tmp_path)
    (repo / "feature.txt").write_text("target staged value\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")

    result = await _merge(ctx, git_manager)

    assert result["success"] is False
    assert result["overlapping_dirty_paths"] == ["feature.txt"]
    assert _git(repo, "diff", "--cached", "--name-only") == "feature.txt"


@pytest.mark.asyncio
async def test_clean_target_keeps_no_ff_merge_landing(tmp_path: Path) -> None:
    repo, _, git_manager, ctx = _repo_with_feature(tmp_path)

    result = await _merge(ctx, git_manager)

    assert result["success"] is True
    assert result["landing"] == "merge"
    assert len(_git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3


@pytest.mark.asyncio
async def test_sync_into_branch_conflict_is_reported_and_aborted(tmp_path: Path) -> None:
    repo, source_path, git_manager, ctx = _repo_with_feature(tmp_path)
    _commit_file(source_path, "conflict.txt", "feature value\n")
    (repo / "conflict.txt").write_text("target value\n", encoding="utf-8")
    _git(repo, "add", "conflict.txt")
    _git(repo, "commit", "-m", "target conflict")
    (repo / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")

    result = await _merge(ctx, git_manager)

    assert result["success"] is False
    assert result["has_conflicts"] is True
    assert result["conflicted_files"] == ["conflict.txt"]
    assert result["step"] == "sync-into-branch"
    assert _git(source_path, "status", "--porcelain") == ""
    assert _git(repo, "diff", "--cached", "--name-only") == "staged.txt"

"""Real-repository coverage for merge_worktree dirty-target landing."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

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


def _index_snapshot(repo: Path, path: str) -> tuple[str, str, bytes]:
    """Capture staged entry metadata, staged bytes, and worktree bytes for one path."""
    return (
        _git(repo, "ls-files", "--stage", "--", path),
        _git(repo, "diff", "--cached", "--binary", "--", path),
        (repo / path).read_bytes(),
    )


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
async def test_unrelated_staged_gobby_file_lands_with_index_fidelity(tmp_path: Path) -> None:
    repo, _, git_manager, ctx = _repo_with_feature(tmp_path)
    gobby_path = ".gobby/project.json"
    (repo / gobby_path).write_bytes(b'{"local":"staged"}\n')
    _git(repo, "add", gobby_path)
    before = _index_snapshot(repo, gobby_path)

    result = await _merge(ctx, git_manager)

    assert result["success"] is True
    assert result["landing"] == "fast-forward"
    assert _index_snapshot(repo, gobby_path) == before


async def test_target_only_history_does_not_create_false_dirty_overlap(tmp_path: Path) -> None:
    repo, _, git_manager, ctx = _repo_with_feature(tmp_path)
    _commit_file(repo, "target-only.txt", "committed on target\n")
    (repo / "target-only.txt").write_text("dirty target state\n", encoding="utf-8")

    result = await _merge(ctx, git_manager)

    assert result["success"] is True
    assert (repo / "target-only.txt").read_text(encoding="utf-8") == "dirty target state\n"


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
    gobby_path = ".gobby/project.json"
    (repo / gobby_path).write_bytes(b'{"local":"staged"}\n')
    _git(repo, "add", gobby_path)
    before = _index_snapshot(repo, gobby_path)

    result = await _merge(ctx, git_manager)

    assert result["success"] is False
    assert result["has_conflicts"] is True
    assert result["conflicted_files"] == ["conflict.txt"]
    assert result["step"] == "sync-into-branch"
    assert _git(source_path, "status", "--porcelain") == ""
    assert _git(repo, "diff", "--cached", "--name-only") == ".gobby/project.json\nstaged.txt"
    assert _index_snapshot(repo, gobby_path) == before


@pytest.mark.parametrize("failure_step", ["merge", "stash-list", "stash-pop", "restore-branch"])
async def test_landed_merge_survives_git_timeouts(tmp_path: Path, failure_step: str) -> None:
    repo, source_path, git_manager, ctx = _repo_with_feature(tmp_path)
    source_sha = _git(source_path, "rev-parse", "HEAD")
    # A foreign stash must survive even when our own restore is ambiguous.
    (repo / "base.txt").write_text("foreign dirty state\n", encoding="utf-8")
    _git(repo, "stash", "push", "-m", "foreign-stash")
    foreign_oid = _git(repo, "rev-parse", "refs/stash")
    (repo / ".gobby/project.json").write_text('{"dirty": true}\n', encoding="utf-8")
    if failure_step == "restore-branch":
        _git(repo, "checkout", "-b", "develop")
    run_git = git_manager.run_git_command
    attempted: list[list[str]] = []

    async def fail_git(args: list[str], **kwargs: Any) -> Any:
        attempted.append(args)
        fails = (
            (failure_step == "merge" and args[:2] == ["merge", "refs/heads/feature/path"])
            or (failure_step == "stash-list" and args == ["stash", "list", "--format=%gd%x00%H"])
            or (failure_step == "stash-pop" and args[:2] == ["stash", "pop"])
            or (failure_step == "restore-branch" and args == ["checkout", "develop"])
        )
        if fails:
            if failure_step == "merge":
                await run_git(args, **kwargs)
            elif failure_step == "stash-pop":
                # Git applied the stash before timing out; retaining it is intentional.
                await run_git(["stash", "apply", args[2]], **kwargs)
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 10))
        return await run_git(args, **kwargs)

    with patch.object(git_manager, "run_git_command", AsyncMock(side_effect=fail_git)):
        result = await _merge(ctx, git_manager)

    assert result["success"] is True
    assert result["landing_state"] == "landed"
    assert result["source_sha"] == source_sha
    assert result["merge_sha"] == _git(repo, "rev-parse", "main")
    assert _git(repo, "merge-base", "--is-ancestor", source_sha, str(result["merge_sha"])) == ""
    warnings = cast(list[dict[str, object]], result["cleanup_warnings"])
    expected_step = "merge-operation" if failure_step == "merge" else failure_step
    assert warnings[0]["step"] == expected_step
    remaining = _git(repo, "stash", "list", "--format=%H").splitlines()
    assert foreign_oid in remaining
    if failure_step in {"stash-list", "stash-pop"}:
        assert result["retained_stash_oid"] in remaining
        assert warnings[0]["retained_stash_oid"] == result["retained_stash_oid"]
        assert len(remaining) == 2
    else:
        assert result["retained_stash_oid"] is None
        assert remaining == [foreign_oid]
    if failure_step != "stash-list":
        assert (repo / ".gobby/project.json").read_text(encoding="utf-8") == '{"dirty": true}\n'
    assert not any(args[:2] == ["stash", "drop"] for args in attempted)


@pytest.mark.parametrize("unknown", [False, True])
async def test_prelanding_timeout_reports_verified_failure_or_unknown(
    tmp_path: Path, unknown: bool
) -> None:
    repo, _, git_manager, ctx = _repo_with_feature(tmp_path)
    before_sha = _git(repo, "rev-parse", "main")
    run_git = git_manager.run_git_command
    status_failed = False

    async def fail_git(args: list[str], **kwargs: Any) -> Any:
        nonlocal status_failed
        if args == ["status", "--porcelain"]:
            status_failed = True
            raise subprocess.TimeoutExpired(args, 10)
        if unknown and status_failed and args == ["rev-parse", "refs/heads/main"]:
            raise subprocess.TimeoutExpired(args, 10)
        return await run_git(args, **kwargs)

    with patch.object(git_manager, "run_git_command", AsyncMock(side_effect=fail_git)):
        result = await _merge(ctx, git_manager)

    assert result["success"] is False
    assert result["landing_state"] == ("unknown" if unknown else "not-landed")
    assert result["merged"] is (None if unknown else False)
    assert "merge_sha" not in result
    assert _git(repo, "rev-parse", "main") == before_sha
    ctx.worktree_storage.mark_merged.assert_not_called()


async def test_reconciliation_uses_captured_source_when_branch_advances(tmp_path: Path) -> None:
    repo, source_path, git_manager, ctx = _repo_with_feature(tmp_path)
    source_sha = _git(source_path, "rev-parse", "HEAD")
    run_git = git_manager.run_git_command

    async def advance_after_merge(args: list[str], **kwargs: Any) -> Any:
        result = await run_git(args, **kwargs)
        if args[:2] == ["merge", "refs/heads/feature/path"]:
            _commit_file(source_path, "later.txt", "later source work\n")
            raise subprocess.TimeoutExpired(args, 240)
        return result

    with patch.object(git_manager, "run_git_command", AsyncMock(side_effect=advance_after_merge)):
        result = await _merge(ctx, git_manager)

    assert result["success"] is True
    assert result["source_sha"] == source_sha
    assert result["merge_sha"] == _git(repo, "rev-parse", "main")
    assert _git(source_path, "rev-parse", "HEAD") != source_sha
    assert not (repo / "later.txt").exists()
    ctx.worktree_storage.mark_merged.assert_not_called()

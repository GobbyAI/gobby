"""Tests for the merge-landscape analytics tools.

Covers each of the six tools registered by `register_merge_landscape_tools`:
analyze_merge_landscape, predict_conflicts, cherry_pick_into_worktree,
merge_subset, verify_in_worktree, inspect_merge_state.

Each tool gets a happy-path test plus at least one failure mode.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.merge_landscape import register_merge_landscape_tools
from gobby.storage.worktrees import Worktree

pytestmark = pytest.mark.unit


# --- helpers ---


def _make_worktree(
    *,
    id: str = "wt-1",
    branch: str = "feat/x",
    path: str = "/tmp/wt-1",
    base: str = "main",
    project_id: str = "proj-1",
    task_id: str | None = None,
) -> Worktree:
    return Worktree(
        id=id,
        project_id=project_id,
        task_id=task_id,
        branch_name=branch,
        worktree_path=path,
        base_branch=base,
        agent_session_id=None,
        status="active",
        created_at="2026-04-28T00:00:00Z",
        updated_at="2026-04-28T00:00:00Z",
        merged_at=None,
    )


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _make_registry(
    *,
    worktree_manager: MagicMock | None,
    git_manager: MagicMock | None,
) -> InternalToolRegistry:
    registry = InternalToolRegistry(name="gobby-merge", description="test")
    if git_manager is not None:
        git_manager.run_git_command.side_effect = lambda args, cwd=None, timeout=30, check=False: (
            git_manager._run_git(args, cwd=cwd, timeout=timeout, check=check)
        )
    register_merge_landscape_tools(
        registry,
        worktree_manager=worktree_manager,
        git_manager=git_manager,
    )
    return registry


# --- analyze_merge_landscape ---


@pytest.mark.asyncio
async def test_analyze_merge_landscape_happy_path(tmp_path) -> None:
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.list_worktrees.return_value = [wt]

    git_manager = MagicMock()
    git_manager._run_git.side_effect = [
        _completed(stdout="3\n"),  # rev-list --count main..HEAD
        _completed(stdout="1\n"),  # rev-list --count HEAD..main
        _completed(stdout="src/a.py\nsrc/b.py\n"),  # diff --name-only
        _completed(stdout="2026-04-28T12:34:56+00:00\n"),  # log -1 %cI
    ]

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=git_manager)
    result = await registry.call("analyze_merge_landscape", {})

    assert result["success"] is True
    assert len(result["worktrees"]) == 1
    entry = result["worktrees"][0]
    assert entry["worktree_id"] == "wt-1"
    assert entry["branch"] == "feat/x"
    assert entry["commits_ahead"] == 3
    assert entry["commits_behind"] == 1
    assert entry["divergence_commits"] == 3
    assert entry["files_touched"] == ["src/a.py", "src/b.py"]
    assert entry["last_commit_at"] == "2026-04-28T12:34:56+00:00"


@pytest.mark.asyncio
async def test_analyze_merge_landscape_behind_only_keeps_divergence_zero(tmp_path) -> None:
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.list_worktrees.return_value = [wt]

    git_manager = MagicMock()
    git_manager._run_git.side_effect = [
        _completed(stdout="0\n"),  # rev-list --count main..HEAD
        _completed(stdout="2\n"),  # rev-list --count HEAD..main
        _completed(stdout=""),  # diff --name-only
        _completed(stdout="2026-04-28T12:34:56+00:00\n"),  # log -1 %cI
    ]

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=git_manager)
    result = await registry.call("analyze_merge_landscape", {})

    assert result["success"] is True
    entry = result["worktrees"][0]
    assert entry["commits_ahead"] == 0
    assert entry["commits_behind"] == 2
    assert entry["divergence_commits"] == 0


@pytest.mark.asyncio
async def test_analyze_merge_landscape_missing_worktree_dir(tmp_path) -> None:
    wt = _make_worktree(path=str(tmp_path / "nonexistent"))
    worktree_manager = MagicMock()
    worktree_manager.list_worktrees.return_value = [wt]
    git_manager = MagicMock()

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=git_manager)
    result = await registry.call("analyze_merge_landscape", {})

    assert result["success"] is True
    assert result["worktrees"][0]["error"] == "worktree_path missing on disk"
    git_manager._run_git.assert_not_called()


@pytest.mark.asyncio
async def test_analyze_merge_landscape_missing_dependencies() -> None:
    registry = _make_registry(worktree_manager=None, git_manager=MagicMock())
    result = await registry.call("analyze_merge_landscape", {})
    assert result["success"] is False
    assert "worktree_manager" in result["error"]


# --- predict_conflicts ---


@pytest.mark.asyncio
async def test_predict_conflicts_clean_pair(tmp_path) -> None:
    wt_a = _make_worktree(id="wt-a", branch="feat/a", path=str(tmp_path / "a"))
    wt_b = _make_worktree(id="wt-b", branch="feat/b", path=str(tmp_path / "b"))
    worktree_manager = MagicMock()
    worktree_manager.get.side_effect = lambda wid: {"wt-a": wt_a, "wt-b": wt_b}.get(wid)

    git_manager = MagicMock()
    git_manager.repo_path = str(tmp_path)
    # 1 pair (a vs b) + 2 target predictions (a vs main, b vs main) = 3 calls.
    git_manager._run_git.side_effect = [
        _completed(returncode=0),  # pair: clean
        _completed(returncode=0),  # target: a clean
        _completed(returncode=0),  # target: b clean
    ]

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=git_manager)
    result = await registry.call("predict_conflicts", {"worktree_ids": ["wt-a", "wt-b"]})

    assert result["success"] is True
    assert len(result["pairs"]) == 1
    assert result["pairs"][0]["clean"] is True
    assert result["pairs"][0]["conflict_files"] == []
    assert len(result["target_predictions"]) == 2
    assert all(p["clean"] for p in result["target_predictions"])


@pytest.mark.asyncio
async def test_predict_conflicts_pair_conflicts(tmp_path) -> None:
    wt_a = _make_worktree(id="wt-a", branch="feat/a", path=str(tmp_path / "a"))
    wt_b = _make_worktree(id="wt-b", branch="feat/b", path=str(tmp_path / "b"))
    worktree_manager = MagicMock()
    worktree_manager.get.side_effect = lambda wid: {"wt-a": wt_a, "wt-b": wt_b}.get(wid)

    git_manager = MagicMock()
    git_manager.repo_path = str(tmp_path)
    conflict_output = "abcdef0123\nsrc/conflict_a.py\nsrc/conflict_b.py\n\nrest of info\n"
    git_manager._run_git.side_effect = [
        _completed(returncode=1, stdout=conflict_output),  # pair conflicts
        _completed(returncode=0),  # target a clean
        _completed(returncode=0),  # target b clean
    ]

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=git_manager)
    result = await registry.call("predict_conflicts", {"worktree_ids": ["wt-a", "wt-b"]})

    assert result["success"] is True
    pair = result["pairs"][0]
    assert pair["clean"] is False
    assert pair["conflict_files"] == ["src/conflict_a.py", "src/conflict_b.py"]
    assert pair["conflict_files_count"] == 2


@pytest.mark.asyncio
async def test_predict_conflicts_empty_input() -> None:
    registry = _make_registry(worktree_manager=MagicMock(), git_manager=MagicMock())
    result = await registry.call("predict_conflicts", {"worktree_ids": []})
    assert result["success"] is False
    assert "non-empty" in result["error"]


# --- cherry_pick_into_worktree ---


@pytest.mark.asyncio
async def test_cherry_pick_success(tmp_path) -> None:
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = wt

    git_manager = MagicMock()
    git_manager._run_git.return_value = _completed(stdout="picked")

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=git_manager)
    result = await registry.call(
        "cherry_pick_into_worktree",
        {"worktree_id": "wt-1", "commits": ["abc123"]},
    )

    assert result["success"] is True
    assert result["applied"] == ["abc123"]


@pytest.mark.asyncio
async def test_cherry_pick_conflict_returns_files(tmp_path) -> None:
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = wt

    git_manager = MagicMock()
    git_manager._run_git.side_effect = [
        _completed(returncode=1, stderr="CONFLICT (content)"),
        _completed(stdout="src/a.py\nsrc/b.py\n"),
    ]

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=git_manager)
    result = await registry.call(
        "cherry_pick_into_worktree",
        {"worktree_id": "wt-1", "commits": ["abc123"]},
    )

    assert result["success"] is False
    assert result["conflicts"] == ["src/a.py", "src/b.py"]


@pytest.mark.asyncio
async def test_cherry_pick_empty_commits() -> None:
    registry = _make_registry(worktree_manager=MagicMock(), git_manager=MagicMock())
    result = await registry.call(
        "cherry_pick_into_worktree", {"worktree_id": "wt-1", "commits": []}
    )
    assert result["success"] is False
    assert "non-empty" in result["error"]


# --- merge_subset ---


@pytest.mark.asyncio
async def test_merge_subset_success(tmp_path) -> None:
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = wt

    git_manager = MagicMock()
    git_manager._run_git.side_effect = [
        _completed(),  # checkout
        _completed(),  # add
        _completed(),  # commit
        _completed(stdout="deadbeef\n"),  # rev-parse HEAD
    ]

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=git_manager)
    result = await registry.call(
        "merge_subset",
        {
            "worktree_id": "wt-1",
            "source_branch": "feat/donor",
            "paths": ["src/a.py", "src/b.py"],
        },
    )

    assert result["success"] is True
    assert result["commit_sha"] == "deadbeef"
    assert result["paths"] == ["src/a.py", "src/b.py"]


@pytest.mark.asyncio
async def test_merge_subset_checkout_failure(tmp_path) -> None:
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = wt

    git_manager = MagicMock()
    git_manager._run_git.side_effect = [
        _completed(returncode=1, stderr="pathspec did not match"),
    ]

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=git_manager)
    result = await registry.call(
        "merge_subset",
        {
            "worktree_id": "wt-1",
            "source_branch": "feat/donor",
            "paths": ["missing.py"],
        },
    )

    assert result["success"] is False
    assert "git checkout failed" in result["error"]


# --- verify_in_worktree ---


@pytest.mark.asyncio
async def test_verify_in_worktree_success(tmp_path) -> None:
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = wt

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=MagicMock())
    result = await registry.call(
        "verify_in_worktree",
        {"worktree_id": "wt-1", "command": "echo hello"},
    )

    assert result["success"] is True
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]


@pytest.mark.asyncio
async def test_verify_in_worktree_command_failure(tmp_path) -> None:
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = wt

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=MagicMock())
    result = await registry.call(
        "verify_in_worktree",
        {"worktree_id": "wt-1", "command": "exit 7"},
    )

    assert result["success"] is False
    assert result["exit_code"] == 7


@pytest.mark.asyncio
async def test_verify_in_worktree_timeout(tmp_path) -> None:
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = wt

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=MagicMock())
    result = await registry.call(
        "verify_in_worktree",
        {"worktree_id": "wt-1", "command": "sleep 5", "timeout": 1},
    )

    assert result["success"] is False
    assert result.get("timed_out") is True


# --- inspect_merge_state ---


@pytest.mark.asyncio
async def test_inspect_merge_state_clean(tmp_path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = wt

    git_manager = MagicMock()
    git_manager._run_git.side_effect = [
        _completed(stdout=".git\n"),  # rev-parse --git-dir
        _completed(stdout=""),  # diff --diff-filter=U
    ]

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=git_manager)
    result = await registry.call("inspect_merge_state", {"worktree_id": "wt-1"})

    assert result["success"] is True
    assert result["state"] == "clean"
    assert result["has_merge_head"] is False
    assert result["conflicted_files"] == []
    assert result["can_resume"] is False


@pytest.mark.asyncio
async def test_inspect_merge_state_orphaned_merge(tmp_path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "MERGE_HEAD").write_text("abcdef0123\n")
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = wt

    git_manager = MagicMock()
    git_manager._run_git.side_effect = [
        _completed(stdout=".git\n"),
        _completed(stdout="src/conflicted.py\n"),
    ]

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=git_manager)
    result = await registry.call("inspect_merge_state", {"worktree_id": "wt-1"})

    assert result["success"] is True
    assert result["state"] == "merging"
    assert result["has_merge_head"] is True
    assert result["conflicted_files"] == ["src/conflicted.py"]
    assert result["can_resume"] is True


@pytest.mark.asyncio
async def test_inspect_merge_state_worktree_missing() -> None:
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = None

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=MagicMock())
    result = await registry.call("inspect_merge_state", {"worktree_id": "missing"})
    assert result["success"] is False
    assert "not found" in result["error"]

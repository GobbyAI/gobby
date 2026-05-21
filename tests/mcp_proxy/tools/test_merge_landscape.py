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
from tests._timing import wait_forever

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
    status: str = "active",
) -> Worktree:
    return Worktree(
        id=id,
        project_id=project_id,
        task_id=task_id,
        branch_name=branch,
        worktree_path=path,
        base_branch=base,
        agent_session_id=None,
        status=status,
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
    merge_storage: MagicMock | None = None,
) -> InternalToolRegistry:
    registry = InternalToolRegistry(name="gobby-merge", description="test")
    register_merge_landscape_tools(
        registry,
        worktree_manager=worktree_manager,
        git_manager=git_manager,
        merge_storage=merge_storage,
    )
    return registry


def _init_git_repo(path) -> None:
    subprocess.run(
        ["git", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )


class _SubprocessGitManager:
    def run_git_command(self, args, cwd=None, timeout=30, check=False):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            timeout=timeout,
            check=check,
            capture_output=True,
            text=True,
        )


def _commit_file(path, name: str, content: str) -> None:
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", f"add {name}"], cwd=path, check=True)


# --- analyze_merge_landscape ---


@pytest.mark.asyncio
async def test_analyze_merge_landscape_happy_path(tmp_path) -> None:
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.list_worktrees.return_value = [wt]

    git_manager = MagicMock()
    git_manager.run_git_command.side_effect = [
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
    assert entry["status"] == "active"
    assert entry["commits_ahead"] == 3
    assert entry["commits_behind"] == 1
    assert entry["divergence_commits"] == 4
    assert entry["files_touched"] == ["src/a.py", "src/b.py"]
    assert entry["last_commit_at"] == "2026-04-28T12:34:56+00:00"


@pytest.mark.asyncio
async def test_analyze_merge_landscape_behind_only_keeps_divergence_zero(tmp_path) -> None:
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.list_worktrees.return_value = [wt]

    git_manager = MagicMock()
    git_manager.run_git_command.side_effect = [
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
    assert entry["divergence_commits"] == 2


@pytest.mark.asyncio
async def test_analyze_merge_landscape_keeps_merged_worktree_with_branch_only_commits(
    tmp_path,
) -> None:
    active = _make_worktree(id="wt-active", path=str(tmp_path / "active"))
    merged = _make_worktree(
        id="wt-merged",
        branch="task/reopened",
        path=str(tmp_path / "merged"),
        task_id="task-reopened",
        status="merged",
    )
    (tmp_path / "active").mkdir()
    (tmp_path / "merged").mkdir()
    worktree_manager = MagicMock()
    worktree_manager.list_worktrees.return_value = [active, merged]

    git_manager = MagicMock()
    git_manager.run_git_command.side_effect = [
        _completed(stdout="0\n"),  # active: rev-list --count main..HEAD
        _completed(stdout="2\n"),  # active: rev-list --count HEAD..main
        _completed(stdout=""),  # active: diff --name-only
        _completed(stdout="2026-04-28T12:34:56+00:00\n"),  # active: log -1 %cI
        _completed(stdout="1\n"),  # merged: branch-only commit after reopen
        _completed(stdout="0\n"),  # merged: rev-list --count HEAD..main
        _completed(stdout="tests/reopened.py\n"),  # merged: diff --name-only
        _completed(stdout="2026-04-29T12:34:56+00:00\n"),  # merged: log -1 %cI
    ]

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=git_manager)
    result = await registry.call("analyze_merge_landscape", {})

    assert result["success"] is True
    assert [entry["worktree_id"] for entry in result["worktrees"]] == [
        "wt-active",
        "wt-merged",
    ]
    reopened = result["worktrees"][1]
    assert reopened["status"] == "merged"
    assert reopened["commits_ahead"] == 1
    assert reopened["reactivation_required"] is True
    assert reopened["files_touched"] == ["tests/reopened.py"]


@pytest.mark.asyncio
async def test_analyze_merge_landscape_skips_merged_worktree_without_ahead_commits(
    tmp_path,
) -> None:
    merged = _make_worktree(
        id="wt-merged",
        branch="task/already-landed",
        path=str(tmp_path),
        status="merged",
    )
    worktree_manager = MagicMock()
    worktree_manager.list_worktrees.return_value = [merged]

    git_manager = MagicMock()
    git_manager.run_git_command.side_effect = [
        _completed(stdout="0\n"),  # rev-list --count main..HEAD
        _completed(stdout="0\n"),  # rev-list --count HEAD..main
    ]

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=git_manager)
    result = await registry.call("analyze_merge_landscape", {})

    assert result["success"] is True
    assert result["worktrees"] == []


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
    git_manager.run_git_command.assert_not_called()


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
    git_manager.run_git_command.side_effect = [
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
async def test_predict_conflicts_defaults_to_worktree_base_branch(tmp_path) -> None:
    wt = _make_worktree(id="wt-a", branch="feat/a", path=str(tmp_path / "a"), base="0.4.7")
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = wt

    git_manager = MagicMock()
    git_manager.repo_path = str(tmp_path)
    git_manager.run_git_command.return_value = _completed(returncode=0)

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=git_manager)
    result = await registry.call("predict_conflicts", {"worktree_ids": ["wt-a"]})

    assert result["success"] is True
    assert result["target_predictions"][0]["target_branch"] == "0.4.7"
    assert git_manager.run_git_command.call_args.args[0] == [
        "merge-tree",
        "--write-tree",
        "--name-only",
        "--no-messages",
        "0.4.7",
        "feat/a",
    ]


@pytest.mark.asyncio
async def test_predict_conflicts_pair_conflicts(tmp_path) -> None:
    wt_a = _make_worktree(id="wt-a", branch="feat/a", path=str(tmp_path / "a"))
    wt_b = _make_worktree(id="wt-b", branch="feat/b", path=str(tmp_path / "b"))
    worktree_manager = MagicMock()
    worktree_manager.get.side_effect = lambda wid: {"wt-a": wt_a, "wt-b": wt_b}.get(wid)

    git_manager = MagicMock()
    git_manager.repo_path = str(tmp_path)
    conflict_output = "abcdef0123\nsrc/conflict_a.py\nsrc/conflict_b.py\n\nrest of info\n"
    git_manager.run_git_command.side_effect = [
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
async def test_predict_conflicts_distinguishes_command_failure(tmp_path) -> None:
    wt_a = _make_worktree(id="wt-a", branch="missing/a", path=str(tmp_path / "a"))
    wt_b = _make_worktree(id="wt-b", branch="feat/b", path=str(tmp_path / "b"))
    worktree_manager = MagicMock()
    worktree_manager.get.side_effect = lambda wid: {"wt-a": wt_a, "wt-b": wt_b}.get(wid)

    git_manager = MagicMock()
    git_manager.repo_path = str(tmp_path)
    git_manager.run_git_command.return_value = _completed(
        returncode=128,
        stderr="fatal: bad revision",
    )

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=git_manager)
    result = await registry.call("predict_conflicts", {"worktree_ids": ["wt-a", "wt-b"]})

    assert result["success"] is False
    assert result["error"] == "merge_tree_failed"
    assert "rc=128" in result["message"]
    assert "fatal: bad revision" in result["message"]


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
    git_manager.run_git_command.return_value = _completed(stdout="picked")

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
    git_manager.run_git_command.side_effect = [
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
    git_manager.run_git_command.side_effect = [
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
    git_manager.run_git_command.side_effect = [
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
    _init_git_repo(tmp_path)
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = wt

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=MagicMock())
    result = await registry.call(
        "verify_in_worktree",
        {"worktree_id": "wt-1", "command": "git status --short"},
    )

    assert result["success"] is True
    assert result["exit_code"] == 0
    assert result["stdout"] == ""


@pytest.mark.asyncio
async def test_verify_in_worktree_final_rejects_dirty_tree(tmp_path) -> None:
    _init_git_repo(tmp_path)
    _commit_file(tmp_path, "tracked.txt", "clean\n")
    (tmp_path / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = wt

    registry = _make_registry(
        worktree_manager=worktree_manager,
        git_manager=_SubprocessGitManager(),
    )
    result = await registry.call(
        "verify_in_worktree",
        {"worktree_id": "wt-1", "command": "git status --short", "final": True},
    )

    assert result["success"] is False
    assert result["exit_code"] == 0
    assert result["error"] == "final verification failed: worktree is dirty"
    assert result["dirty_files"] == [" M tracked.txt"]


@pytest.mark.asyncio
async def test_verify_in_worktree_non_final_allows_dirty_tree(tmp_path) -> None:
    _init_git_repo(tmp_path)
    _commit_file(tmp_path, "tracked.txt", "clean\n")
    (tmp_path / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = wt

    registry = _make_registry(
        worktree_manager=worktree_manager,
        git_manager=_SubprocessGitManager(),
    )
    result = await registry.call(
        "verify_in_worktree",
        {"worktree_id": "wt-1", "command": "git status --short"},
    )

    assert result["success"] is True
    assert result["stdout"] == " M tracked.txt\n"


@pytest.mark.asyncio
async def test_verify_in_worktree_command_failure(tmp_path) -> None:
    _init_git_repo(tmp_path)
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = wt

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=MagicMock())
    result = await registry.call(
        "verify_in_worktree",
        {"worktree_id": "wt-1", "command": "git rev-parse --verify missing-ref"},
    )

    assert result["success"] is False
    assert result["exit_code"] != 0


@pytest.mark.asyncio
async def test_verify_in_worktree_rejects_unapproved_command(tmp_path) -> None:
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = wt

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=MagicMock())
    result = await registry.call(
        "verify_in_worktree",
        {"worktree_id": "wt-1", "command": "python -c 'print(1)'"},
    )

    assert result["success"] is False
    assert "not permitted" in result["error"]


@pytest.mark.asyncio
async def test_verify_in_worktree_parse_error(tmp_path) -> None:
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = wt

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=MagicMock())
    result = await registry.call(
        "verify_in_worktree",
        {"worktree_id": "wt-1", "command": "python -c 'unterminated"},
    )

    assert result["success"] is False
    assert "failed to parse command" in result["error"]


@pytest.mark.asyncio
async def test_verify_in_worktree_empty_command(tmp_path) -> None:
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = wt

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=MagicMock())
    result = await registry.call(
        "verify_in_worktree",
        {"worktree_id": "wt-1", "command": "   "},
    )

    assert result["success"] is False
    assert result["error"] == "command is required"


@pytest.mark.asyncio
async def test_verify_in_worktree_timeout(tmp_path, monkeypatch) -> None:
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = wt

    class SlowProcess:
        returncode: int | None = None

        async def communicate(self):
            await wait_forever()
            return b"", b""

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode or -9

    async def slow_subprocess(*_args, **_kwargs):
        return SlowProcess()

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.merge_landscape.asyncio.create_subprocess_exec",
        slow_subprocess,
    )

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=MagicMock())
    result = await registry.call(
        "verify_in_worktree",
        {"worktree_id": "wt-1", "command": "git status", "timeout": 1},
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
    git_manager.run_git_command.side_effect = [
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
    assert result["active_resolution_id"] is None
    assert result["conflicts"] == []


@pytest.mark.asyncio
async def test_inspect_merge_state_orphaned_merge(tmp_path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "MERGE_HEAD").write_text("abcdef0123\n")
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = wt

    git_manager = MagicMock()
    git_manager.run_git_command.side_effect = [
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
async def test_inspect_merge_state_includes_active_resolution_conflicts(tmp_path) -> None:
    from gobby.storage.merge_resolutions import MergeConflict, MergeResolution

    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "MERGE_HEAD").write_text("abcdef0123\n")
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = wt

    git_manager = MagicMock()
    git_manager.run_git_command.side_effect = [
        _completed(stdout=".git\n"),
        _completed(stdout="src/conflicted.py\n"),
    ]

    merge_storage = MagicMock()
    merge_storage.get_active_resolution.return_value = MergeResolution(
        id="mr-test123",
        worktree_id="wt-1",
        source_branch="feature/test",
        target_branch="0.4.7",
        status="pending",
        tier_used=None,
        created_at="2026-05-20T00:00:00+00:00",
        updated_at="2026-05-20T00:00:00+00:00",
    )
    merge_storage.list_conflicts.return_value = [
        MergeConflict(
            id="mc-conflict1",
            resolution_id="mr-test123",
            file_path="src/conflicted.py",
            status="pending",
            ours_content=None,
            theirs_content=None,
            resolved_content=None,
            created_at="2026-05-20T00:00:00+00:00",
            updated_at="2026-05-20T00:00:00+00:00",
        )
    ]

    registry = _make_registry(
        worktree_manager=worktree_manager,
        git_manager=git_manager,
        merge_storage=merge_storage,
    )
    result = await registry.call("inspect_merge_state", {"worktree_id": "wt-1"})

    assert result["success"] is True
    assert result["state"] == "merging"
    assert result["active_resolution_id"] == "mr-test123"
    assert result["source_branch"] == "feature/test"
    assert result["target_branch"] == "0.4.7"
    assert result["conflicts"] == [
        {
            "conflict_id": "mc-conflict1",
            "file_path": "src/conflicted.py",
            "status": "pending",
            "has_resolved_content": False,
        }
    ]
    merge_storage.get_active_resolution.assert_called_once_with("wt-1")
    merge_storage.list_conflicts.assert_called_once_with(resolution_id="mr-test123")


@pytest.mark.asyncio
async def test_inspect_merge_state_recovers_latest_resolution_for_orphaned_git_merge(
    tmp_path,
) -> None:
    from gobby.storage.merge_resolutions import MergeConflict, MergeResolution

    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "MERGE_HEAD").write_text("abcdef0123\n")
    wt = _make_worktree(path=str(tmp_path))
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = wt

    git_manager = MagicMock()
    git_manager.run_git_command.side_effect = [
        _completed(stdout=".git\n"),
        _completed(stdout="src/conflicted.py\n"),
    ]

    merge_storage = MagicMock()
    merge_storage.get_active_resolution.return_value = None
    merge_storage.get_latest_resolution.return_value = MergeResolution(
        id="mr-stale123",
        worktree_id="wt-1",
        source_branch="feature/test",
        target_branch="0.4.7",
        status="resolved",
        tier_used="conflict_only_ai",
        created_at="2026-05-20T00:00:00+00:00",
        updated_at="2026-05-20T00:01:00+00:00",
    )
    merge_storage.list_conflicts.return_value = [
        MergeConflict(
            id="mc-stale1",
            resolution_id="mr-stale123",
            file_path="src/conflicted.py",
            status="resolved",
            ours_content=None,
            theirs_content=None,
            resolved_content=None,
            created_at="2026-05-20T00:00:00+00:00",
            updated_at="2026-05-20T00:01:00+00:00",
        )
    ]

    registry = _make_registry(
        worktree_manager=worktree_manager,
        git_manager=git_manager,
        merge_storage=merge_storage,
    )
    result = await registry.call("inspect_merge_state", {"worktree_id": "wt-1"})

    assert result["success"] is True
    assert result["active_resolution_id"] == "mr-stale123"
    assert result["conflicts"] == [
        {
            "conflict_id": "mc-stale1",
            "file_path": "src/conflicted.py",
            "status": "pending",
            "has_resolved_content": False,
        }
    ]
    merge_storage.get_latest_resolution.assert_called_once_with("wt-1")


@pytest.mark.asyncio
async def test_inspect_merge_state_worktree_missing() -> None:
    worktree_manager = MagicMock()
    worktree_manager.get.return_value = None

    registry = _make_registry(worktree_manager=worktree_manager, git_manager=MagicMock())
    result = await registry.call("inspect_merge_state", {"worktree_id": "missing"})
    assert result["success"] is False
    assert "not found" in result["error"]

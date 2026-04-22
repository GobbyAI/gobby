from unittest.mock import MagicMock

import pytest

from gobby.storage.worktrees import Worktree

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_merge_worktree_success(registry, mock_worktree_storage, mock_git_manager) -> None:
    """Merge worktree successfully (fully isolated in worktree)."""
    wt = Worktree(
        id="wt-1",
        project_id="proj-1",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager._run_git.return_value = MagicMock(returncode=0, stdout="main", stderr="")
    mock_worktree_storage.mark_merged.return_value = True

    result = await registry.call("merge_worktree", {"worktree_id": "wt-1", "target_branch": "main"})

    assert result["success"] is True
    assert result["source_branch"] == "feature/test"
    assert result["target_branch"] == "main"
    mock_worktree_storage.mark_merged.assert_called_once_with("wt-1")
    calls = mock_git_manager._run_git.call_args_list
    merge_call = [c for c in calls if c[0][0][:1] == ["merge"] and "--no-edit" in c[0][0]]
    assert len(merge_call) == 1
    assert (
        merge_call[0].kwargs.get("cwd") == "/tmp/wt1" or merge_call[0][1].get("cwd") == "/tmp/wt1"
    )
    assert result["push_command"] == "git push --no-verify origin feature/test:main"
    assert result["pushed"] is False
    push_calls = [c for c in calls if c[0][0][:1] == ["push"]]
    assert len(push_calls) == 0


@pytest.mark.asyncio
async def test_merge_worktree_push_success(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """Merge worktree with push=True executes git push after merge."""
    wt = Worktree(
        id="wt-1",
        project_id="proj-1",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager._run_git.return_value = MagicMock(returncode=0, stdout="", stderr="")
    mock_worktree_storage.mark_merged.return_value = True

    result = await registry.call(
        "merge_worktree", {"worktree_id": "wt-1", "target_branch": "main", "push": True}
    )

    assert result["success"] is True
    assert result["pushed"] is True
    assert "push_command" not in result
    calls = mock_git_manager._run_git.call_args_list
    push_calls = [c for c in calls if c[0][0][:1] == ["push"]]
    assert len(push_calls) == 1
    push_args = push_calls[0][0][0]
    assert push_args == ["push", "--no-verify", "origin", "feature/test:main"]
    assert (
        push_calls[0].kwargs.get("cwd") == "/tmp/wt1" or push_calls[0][1].get("cwd") == "/tmp/wt1"
    )


@pytest.mark.asyncio
async def test_merge_worktree_push_failure(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """Merge worktree with push=True returns merge_succeeded when push fails."""
    wt = Worktree(
        id="wt-1",
        project_id="proj-1",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt

    ok = MagicMock(returncode=0, stdout="", stderr="")
    mock_git_manager._run_git.side_effect = [
        ok,
        MagicMock(returncode=0, stdout="stash@{0}", stderr=""),
        ok,
        MagicMock(returncode=0, stdout="stash@{0}\nstash@{1}", stderr=""),
        ok,
        MagicMock(returncode=1, stdout="", stderr="rejected: non-fast-forward"),
        ok,
    ]
    mock_worktree_storage.mark_merged.return_value = True

    result = await registry.call(
        "merge_worktree", {"worktree_id": "wt-1", "target_branch": "main", "push": True}
    )

    assert result["success"] is False
    assert result["merge_succeeded"] is True
    assert "Push failed" in result["error"]
    assert result["source_branch"] == "feature/test"
    assert result["target_branch"] == "main"


@pytest.mark.asyncio
async def test_merge_worktree_not_found(registry, mock_worktree_storage) -> None:
    """Merge fails when worktree not found."""
    mock_worktree_storage.get.return_value = None

    result = await registry.call(
        "merge_worktree", {"worktree_id": "missing", "target_branch": "main"}
    )

    assert result["success"] is False
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_merge_worktree_default_target_branch(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """Merge defaults target_branch to worktree's base_branch."""
    wt = Worktree(
        id="wt-1",
        project_id="proj-1",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="develop",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager._run_git.return_value = MagicMock(returncode=0, stdout="develop", stderr="")
    mock_worktree_storage.mark_merged.return_value = True

    result = await registry.call("merge_worktree", {"worktree_id": "wt-1"})

    assert result["success"] is True
    calls = mock_git_manager._run_git.call_args_list
    wt_merge = [c for c in calls if c[0][0][:1] == ["merge"] and "--no-edit" in c[0][0]]
    assert len(wt_merge) == 1
    assert "origin/develop" in wt_merge[0][0][0]


@pytest.mark.asyncio
async def test_merge_worktree_conflict(registry, mock_worktree_storage, mock_git_manager) -> None:
    """Merge detects conflicts in worktree and aborts cleanly."""
    wt = Worktree(
        id="wt-1",
        project_id="proj-1",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt

    def _run_git_side_effect(args, cwd=None, timeout=30, check=False):
        if args[0] == "fetch":
            return MagicMock(returncode=0, stdout="", stderr="")
        if args[0] == "merge" and "--no-edit" in args:
            return MagicMock(
                returncode=1,
                stdout="CONFLICT (content): Merge conflict in src/foo.py\n"
                "CONFLICT (content): Merge conflict in src/bar.py",
                stderr="Automatic merge failed",
            )
        if args[0] == "diff" and "--diff-filter=U" in args:
            return MagicMock(returncode=0, stdout="src/foo.py\nsrc/bar.py\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    mock_git_manager._run_git.side_effect = _run_git_side_effect

    result = await registry.call("merge_worktree", {"worktree_id": "wt-1", "target_branch": "main"})

    assert result["success"] is False
    assert result["has_conflicts"] is True
    assert len(result["conflicted_files"]) == 2
    mock_worktree_storage.mark_merged.assert_not_called()
    abort_calls = [
        c for c in mock_git_manager._run_git.call_args_list if c[0][0] == ["merge", "--abort"]
    ]
    assert len(abort_calls) == 1


@pytest.mark.asyncio
async def test_merge_worktree_non_conflict_failure(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """Merge fails with non-conflict error."""
    wt = Worktree(
        id="wt-1",
        project_id="proj-1",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt

    def _run_git_side_effect(args, cwd=None, timeout=30, check=False):
        if args[0] == "fetch":
            return MagicMock(returncode=0, stdout="", stderr="")
        if args[0] == "merge" and "--no-edit" in args:
            return MagicMock(
                returncode=128,
                stdout="",
                stderr="fatal: Not a valid object name 'main'",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    mock_git_manager._run_git.side_effect = _run_git_side_effect

    result = await registry.call("merge_worktree", {"worktree_id": "wt-1", "target_branch": "main"})

    assert result["success"] is False
    assert result["has_conflicts"] is False
    mock_worktree_storage.mark_merged.assert_not_called()


@pytest.mark.asyncio
async def test_merge_worktree_explicit_source_branch(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """Agent can specify source_branch explicitly."""
    wt = Worktree(
        id="wt-1",
        project_id="proj-1",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager._run_git.return_value = MagicMock(returncode=0, stdout="", stderr="")
    mock_worktree_storage.mark_merged.return_value = True

    result = await registry.call(
        "merge_worktree",
        {"worktree_id": "wt-1", "source_branch": "my-branch", "target_branch": "main"},
    )

    assert result["success"] is True
    assert result["source_branch"] == "my-branch"
    assert result["push_command"] == "git push --no-verify origin my-branch:main"
    push_calls = [c for c in mock_git_manager._run_git.call_args_list if c[0][0][:1] == ["push"]]
    assert len(push_calls) == 0


@pytest.mark.asyncio
async def test_merge_worktree_no_main_repo_operations(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """All git commands run in the worktree, never the main repo."""
    wt = Worktree(
        id="wt-1",
        project_id="proj-1",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager._run_git.return_value = MagicMock(returncode=0, stdout="", stderr="")
    mock_worktree_storage.mark_merged.return_value = True

    result = await registry.call("merge_worktree", {"worktree_id": "wt-1", "target_branch": "main"})

    assert result["success"] is True
    for call in mock_git_manager._run_git.call_args_list:
        cwd = call.kwargs.get("cwd") or (call[1].get("cwd") if len(call) > 1 else None)
        assert cwd == "/tmp/wt1", f"Git command ran without worktree cwd: {call}"

from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.worktrees import create_worktrees_registry
from gobby.storage.worktrees import Worktree, WorktreeStatus

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_worktree_storage():
    return MagicMock()


@pytest.fixture
def mock_git_manager():
    manager = MagicMock()
    manager.repo_path = "/tmp/repo"
    manager.run_git_command.side_effect = (
        lambda args, cwd=None, timeout=30, check=False: manager._run_git(
            args, cwd=cwd, timeout=timeout, check=check
        )
    )

    def get_unmerged_files(cwd=None):
        result = manager._run_git(["diff", "--name-only", "--diff-filter=U"], cwd=cwd, timeout=10)
        return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]

    manager.get_unmerged_files.side_effect = get_unmerged_files
    return manager


@pytest.fixture
def registry(mock_worktree_storage, mock_git_manager):
    return create_worktrees_registry(
        worktree_storage=mock_worktree_storage,
        git_manager=mock_git_manager,
        project_id="proj-1",
    )


@pytest.mark.asyncio
async def test_get_worktree_found(registry, mock_worktree_storage, mock_git_manager) -> None:
    wt = Worktree(
        id="wt-123",
        project_id="proj-1",
        branch_name="feat/1",
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
    mock_status = MagicMock()
    mock_status.has_uncommitted_changes = True
    mock_status.ahead = 1
    mock_status.behind = 2
    mock_status.branch = "feat/1"
    mock_git_manager.get_worktree_status.return_value = mock_status
    with patch("pathlib.Path.exists", return_value=True):
        result = await registry.call("get_worktree", {"worktree_id": "wt-123"})
        assert result["success"] is True
        assert result["worktree"]["id"] == "wt-123"
        assert result["git_status"]["has_uncommitted_changes"] is True


@pytest.mark.asyncio
async def test_get_worktree_not_found(registry, mock_worktree_storage) -> None:
    mock_worktree_storage.get.return_value = None
    result = await registry.call("get_worktree", {"worktree_id": "missing"})
    assert result["success"] is False
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_get_worktree_path_not_exists(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """Test get_worktree when path doesn't exist on disk."""
    wt = Worktree(
        id="wt-123",
        project_id="proj-1",
        branch_name="feat/1",
        worktree_path="/nonexistent/path",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    with patch("pathlib.Path.exists", return_value=False):
        result = await registry.call("get_worktree", {"worktree_id": "wt-123"})
        assert result["success"] is True
        assert result.get("git_status") is None or "git_status" not in result


@pytest.mark.asyncio
async def test_get_worktree_downgrades_stale_merged_status(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    wt = Worktree(
        id="wt-123",
        project_id="proj-1",
        branch_name="feature/not-merged",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status=WorktreeStatus.MERGED.value,
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at="2026-04-22T00:00:00+00:00",
        cleanup_after="2026-04-29T00:00:00+00:00",
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager._run_git.return_value = MagicMock(returncode=1, stdout="", stderr="")

    with patch("pathlib.Path.exists", return_value=True):
        result = await registry.call("get_worktree", {"worktree_id": "wt-123"})

    assert result["success"] is True
    assert result["worktree"]["status"] == WorktreeStatus.ACTIVE.value
    assert result["worktree"]["stored_status"] == WorktreeStatus.MERGED.value
    assert result["worktree"]["merged_at"] is None
    assert result["worktree"]["git_merge_state"]["git_merged"] is False


@pytest.mark.asyncio
async def test_list_worktrees(registry, mock_worktree_storage) -> None:
    wt1 = Worktree(
        id="1",
        project_id="p1",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.list_worktrees.return_value = [wt1]
    result = await registry.call("list_worktrees", {"status": "active"})
    assert result["success"] is True
    assert len(result["worktrees"]) == 1
    mock_worktree_storage.list_worktrees.assert_called_with(
        project_id="proj-1", status="active", agent_session_id=None, limit=50
    )


@pytest.mark.asyncio
async def test_claim_worktree_success(registry, mock_worktree_storage) -> None:
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        agent_session_id=None,
        task_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_worktree_storage.claim.return_value = True
    result = await registry.call("claim_worktree", {"worktree_id": "wt-1", "session_id": "sess-1"})
    assert result["success"] is True
    mock_worktree_storage.claim.assert_called_with("wt-1", "sess-1")


@pytest.mark.asyncio
async def test_claim_worktree_already_claimed(registry, mock_worktree_storage) -> None:
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        agent_session_id="other-session",
        task_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    result = await registry.call("claim_worktree", {"worktree_id": "wt-1", "session_id": "sess-1"})
    assert result["success"] is False
    assert "already claimed" in result["error"]


@pytest.mark.asyncio
async def test_claim_worktree_not_found(registry, mock_worktree_storage) -> None:
    """Test claim_worktree when worktree not found."""
    mock_worktree_storage.get.return_value = None
    result = await registry.call(
        "claim_worktree", {"worktree_id": "nonexistent", "session_id": "sess-1"}
    )
    assert result["success"] is False
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_release_worktree(registry, mock_worktree_storage) -> None:
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        agent_session_id="sess-1",
        task_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_worktree_storage.release.return_value = True
    result = await registry.call("release_worktree", {"worktree_id": "wt-1"})
    assert result["success"] is True
    mock_worktree_storage.release.assert_called_with("wt-1")


@pytest.mark.asyncio
async def test_release_worktree_not_found(registry, mock_worktree_storage) -> None:
    """Test release_worktree when worktree not found."""
    mock_worktree_storage.get.return_value = None
    result = await registry.call("release_worktree", {"worktree_id": "nonexistent"})
    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_abandon_worktree(registry, mock_worktree_storage) -> None:
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status=WorktreeStatus.ACTIVE.value,
        created_at="",
        updated_at="",
        agent_session_id=None,
        task_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_worktree_storage.mark_abandoned.return_value = wt

    result = await registry.call("abandon_worktree", {"worktree_id": "wt-1"})

    assert result["success"] is True
    mock_worktree_storage.mark_abandoned.assert_called_once_with("wt-1")


@pytest.mark.asyncio
async def test_abandon_worktree_not_found(registry, mock_worktree_storage) -> None:
    mock_worktree_storage.get.return_value = None
    result = await registry.call("abandon_worktree", {"worktree_id": "missing"})
    assert result["success"] is False
    assert "not found" in result["error"]
    mock_worktree_storage.mark_abandoned.assert_not_called()


@pytest.mark.asyncio
async def test_reactivate_worktree(registry, mock_worktree_storage) -> None:
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status=WorktreeStatus.MERGED.value,
        created_at="",
        updated_at="",
        agent_session_id=None,
        task_id=None,
        merged_at="2026-04-22T00:00:00+00:00",
        cleanup_after="2026-04-29T00:00:00+00:00",
    )
    mock_worktree_storage.get.return_value = wt
    mock_worktree_storage.update.return_value = wt

    result = await registry.call("reactivate_worktree", {"worktree_id": "wt-1"})

    assert result["success"] is True
    mock_worktree_storage.update.assert_called_once_with(
        "wt-1",
        status=WorktreeStatus.ACTIVE.value,
        merged_at=None,
        cleanup_after=None,
    )


@pytest.mark.asyncio
async def test_reactivate_worktree_not_found(registry, mock_worktree_storage) -> None:
    mock_worktree_storage.get.return_value = None
    result = await registry.call("reactivate_worktree", {"worktree_id": "missing"})
    assert result["success"] is False
    assert "not found" in result["error"]
    mock_worktree_storage.update.assert_not_called()


@pytest.mark.asyncio
async def test_mark_worktree_merged_requires_git_ancestry(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="feature/not-merged",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status=WorktreeStatus.ACTIVE.value,
        created_at="",
        updated_at="",
        agent_session_id=None,
        task_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager._run_git.return_value = MagicMock(returncode=1, stdout="", stderr="")

    result = await registry.call("mark_worktree_merged", {"worktree_id": "wt-1"})

    assert result["success"] is False
    assert "not merged" in result["error"]
    mock_worktree_storage.mark_merged.assert_not_called()


@pytest.mark.asyncio
async def test_mark_worktree_merged_success(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="feature/merged",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status=WorktreeStatus.ACTIVE.value,
        created_at="",
        updated_at="",
        agent_session_id=None,
        task_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager._run_git.return_value = MagicMock(returncode=0, stdout="", stderr="")
    mock_worktree_storage.mark_merged.return_value = wt

    result = await registry.call("mark_worktree_merged", {"worktree_id": "wt-1"})

    assert result["success"] is True
    mock_worktree_storage.mark_merged.assert_called_once_with("wt-1")


@pytest.mark.asyncio
async def test_delete_worktree_success(registry, mock_worktree_storage, mock_git_manager) -> None:
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager.get_worktree_status.return_value.has_uncommitted_changes = False
    mock_git_manager.delete_worktree.return_value.success = True
    mock_worktree_storage.delete.return_value = True
    with patch("pathlib.Path.exists", return_value=True):
        result = await registry.call("delete_worktree", {"worktree_id": "wt-1"})
        assert result["success"] is True
        mock_git_manager.delete_worktree.assert_called_with(
            "/tmp/p1", force=False, delete_branch=True, branch_name="b1"
        )
        mock_worktree_storage.delete.assert_called_with("wt-1")


@pytest.mark.asyncio
async def test_delete_worktree_uncommitted_changes(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager.get_worktree_status.return_value.has_uncommitted_changes = True
    with patch("pathlib.Path.exists", return_value=True):
        result = await registry.call("delete_worktree", {"worktree_id": "wt-1"})
        assert result["success"] is False
        assert "uncommitted changes" in result["error"]

        mock_git_manager.delete_worktree.return_value.success = True
        mock_worktree_storage.delete.return_value = True
        result_force = await registry.call(
            "delete_worktree", {"worktree_id": "wt-1", "force": True}
        )
        assert result_force["success"] is True
        mock_git_manager.delete_worktree.assert_called_with(
            "/tmp/p1", force=True, delete_branch=True, branch_name="b1"
        )


@pytest.mark.asyncio
async def test_delete_worktree_not_found(registry, mock_worktree_storage) -> None:
    """Test delete_worktree is idempotent when worktree not found."""
    mock_worktree_storage.get.return_value = None
    result = await registry.call("delete_worktree", {"worktree_id": "nonexistent"})
    assert result["success"] is True
    assert result["already_deleted"] is True


@pytest.mark.asyncio
async def test_delete_worktree_path_not_exists(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """Test delete_worktree when path doesn't exist (orphaned DB record)."""
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="/nonexistent",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_worktree_storage.delete.return_value = True
    with patch("pathlib.Path.exists", return_value=False):
        result = await registry.call("delete_worktree", {"worktree_id": "wt-1"})
        assert result["success"] is True
        mock_git_manager.delete_worktree.assert_not_called()
        mock_worktree_storage.delete.assert_called_once_with("wt-1")


@pytest.mark.asyncio
async def test_delete_worktree_git_failure(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """Test delete_worktree when git delete fails."""
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager.get_worktree_status.return_value.has_uncommitted_changes = False
    mock_git_manager.delete_worktree.return_value.success = False
    mock_git_manager.delete_worktree.return_value.error = "Git delete failed"
    with patch("pathlib.Path.exists", return_value=True):
        result = await registry.call("delete_worktree", {"worktree_id": "wt-1"})
        assert result["success"] is False
        assert "Git delete failed" in result["error"]


@pytest.mark.asyncio
async def test_sync_worktree(registry, mock_worktree_storage, mock_git_manager) -> None:
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager.sync_from_main.return_value.success = True
    mock_git_manager.sync_from_main.return_value.message = "Synced"
    result = await registry.call("sync_worktree", {"worktree_id": "wt-1", "strategy": "merge"})
    assert result["success"] is True
    mock_git_manager.sync_from_main.assert_called_with(
        "/tmp/p1", base_branch="main", strategy="merge"
    )


@pytest.mark.asyncio
async def test_sync_worktree_not_found(registry, mock_worktree_storage) -> None:
    """Test sync_worktree when worktree not found."""
    mock_worktree_storage.get.return_value = None
    result = await registry.call("sync_worktree", {"worktree_id": "nonexistent"})
    assert result["success"] is False
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_sync_worktree_failure(registry, mock_worktree_storage, mock_git_manager) -> None:
    """Test sync_worktree when sync fails."""
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager.sync_from_main.return_value.success = False
    mock_git_manager.sync_from_main.return_value.error = "Sync failed"
    result = await registry.call("sync_worktree", {"worktree_id": "wt-1"})
    assert result["success"] is False
    assert "Sync failed" in result["error"]


@pytest.mark.asyncio
async def test_detect_stale_worktrees(registry, mock_worktree_storage) -> None:
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status="active",
        created_at="old",
        updated_at="old",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.find_stale.return_value = [wt]
    result = await registry.call("detect_stale_worktrees", {"hours": 48})
    assert result["success"] is True
    assert result["count"] == 1
    mock_worktree_storage.find_stale.assert_called_with(project_id="proj-1", hours=48, limit=50)


@pytest.mark.asyncio
async def test_cleanup_stale_worktrees(registry, mock_worktree_storage, mock_git_manager) -> None:
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at="old",
        updated_at="old",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.cleanup_stale.return_value = [wt]
    mock_git_manager.delete_worktree.return_value.success = True

    result = await registry.call("cleanup_stale_worktrees", {"hours": 24, "dry_run": True})
    assert result["success"] is True
    assert result["count"] == 1
    assert result["cleaned"][0]["marked_abandoned"] is False
    mock_worktree_storage.cleanup_stale.assert_called_with(
        project_id="proj-1", hours=24, dry_run=True
    )

    result = await registry.call(
        "cleanup_stale_worktrees", {"hours": 24, "dry_run": False, "delete_git": True}
    )
    assert result["success"] is True
    assert result["cleaned"][0]["marked_abandoned"] is True
    assert result["cleaned"][0]["git_deleted"] is True
    mock_git_manager.delete_worktree.assert_called_with(
        "/tmp/p1", force=True, delete_branch=True, branch_name="b1"
    )


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
async def test_merge_worktree_does_not_mark_merged_when_target_lacks_source(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    wt = Worktree(
        id="wt-1",
        project_id="proj-1",
        branch_name="feature/not-merged",
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
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return MagicMock(returncode=1, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    mock_git_manager._run_git.side_effect = _run_git_side_effect

    result = await registry.call("merge_worktree", {"worktree_id": "wt-1", "target_branch": "main"})

    assert result["success"] is True
    assert result["merged"] is False
    mock_worktree_storage.mark_merged.assert_not_called()


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


def _merge_worktree_push_failure_git_sequence() -> list[MagicMock]:
    successful_git_call = MagicMock(returncode=0, stdout="", stderr="")
    local_target_exists = successful_git_call
    remote_target_missing = MagicMock(returncode=1, stdout="", stderr="")
    stash_before = MagicMock(returncode=0, stdout="stash@{0}", stderr="")
    stash_after = MagicMock(returncode=0, stdout="stash@{0}\nstash@{1}", stderr="")
    push_rejected = MagicMock(returncode=1, stdout="", stderr="rejected: non-fast-forward")
    return [
        successful_git_call,  # fetch
        local_target_exists,  # show-ref refs/heads/main
        remote_target_missing,  # show-ref refs/remotes/origin/main
        stash_before,  # stash list before
        successful_git_call,  # stash push
        stash_after,  # stash list after
        successful_git_call,  # merge
        push_rejected,  # push
        successful_git_call,  # stash pop
    ]


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

    mock_git_manager._run_git.side_effect = _merge_worktree_push_failure_git_sequence()
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
    assert wt_merge[0][0][0] == ["merge", "develop", "--no-edit"]


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


@pytest.mark.asyncio
async def test_get_worktree_by_task_downgrades_stale_merged_status(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    wt = Worktree(
        id="wt-123",
        project_id="proj-1",
        branch_name="feature/not-merged",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status=WorktreeStatus.MERGED.value,
        created_at="",
        updated_at="",
        task_id="task-1",
        agent_session_id=None,
        merged_at="2026-04-22T00:00:00+00:00",
        cleanup_after="2026-04-29T00:00:00+00:00",
    )
    mock_worktree_storage.get_by_task.return_value = wt
    mock_git_manager._run_git.return_value = MagicMock(returncode=1, stdout="", stderr="")

    result = await registry.call("get_worktree_by_task", {"task_id": "task-1"})

    assert result["success"] is True
    assert result["worktree"]["status"] == WorktreeStatus.ACTIVE.value
    assert result["worktree"]["stored_status"] == WorktreeStatus.MERGED.value
    assert result["worktree"]["git_merge_state"]["git_merged"] is False

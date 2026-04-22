from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.worktrees import Worktree

pytestmark = pytest.mark.unit


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

"""Tests for gobby.mcp_proxy.tools.clones module.

Tests for the gobby-clones MCP server tools:
- create_clone
- get_clone
- list_clones
- delete_clone
- sync_clone
- merge_clone
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.storage.clones import Clone, CloneStatus

pytestmark = pytest.mark.integration


@pytest.fixture
def mock_clone_storage() -> MagicMock:
    """Create mock clone storage."""
    return MagicMock()


@pytest.fixture
def mock_git_manager() -> MagicMock:
    """Create mock git manager."""
    manager = MagicMock()
    manager.repo_path = Path("/tmp/repo")
    return manager


@pytest.fixture
def registry(mock_clone_storage: Any, mock_git_manager: Any) -> Any:
    """Create registry with clone tools."""
    from gobby.mcp_proxy.tools.clones import create_clones_registry

    return create_clones_registry(
        clone_storage=mock_clone_storage,
        git_manager=mock_git_manager,
        project_id="11111111-1111-4111-8111-111111110001",
    )


class TestClonesRegistryCreation:
    """Tests for registry creation."""

    def test_creates_registry_with_expected_tools(self, registry: Any) -> None:
        """Registry has all expected tools."""
        tools = registry.list_tools()
        tool_names = [t["name"] for t in tools]

        assert "create_clone" in tool_names
        assert "get_clone" in tool_names
        assert "list_clones" in tool_names
        assert "delete_clone" in tool_names
        assert "sync_clone" in tool_names


class TestCreateClone:
    """Tests for create_clone tool."""

    @pytest.mark.asyncio
    async def test_create_clone_success(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Create clone successfully."""
        mock_git_manager.shallow_clone.return_value = MagicMock(success=True)
        mock_git_manager.get_remote_url.return_value = "https://github.com/user/repo.git"
        mock_clone_storage.create.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url="https://github.com/user/repo.git",
            last_sync_at=None,
            cleanup_after=None,
            created_at="now",
            updated_at="now",
        )

        result = await registry.call(
            "create_clone",
            {"branch_name": "main", "clone_path": "/tmp/clones/test"},
        )

        assert result["success"] is True
        assert result["clone"]["id"] == "clone-123"
        mock_git_manager.shallow_clone.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_clone_git_failure(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Create clone fails when git operation fails."""
        mock_git_manager.shallow_clone.return_value = MagicMock(success=False, error="Clone failed")
        mock_git_manager.get_remote_url.return_value = "https://github.com/user/repo.git"

        result = await registry.call(
            "create_clone",
            {"branch_name": "main", "clone_path": "/tmp/clones/test"},
        )

        assert result["success"] is False
        assert "failed" in result["error"].lower()
        mock_clone_storage.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_clone_with_task_id(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Create clone linked to a task."""
        mock_git_manager.shallow_clone.return_value = MagicMock(success=True)
        mock_git_manager.get_remote_url.return_value = "https://github.com/user/repo.git"
        mock_clone_storage.create.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id="task-456",
            agent_session_id=None,
            status="active",
            remote_url="https://github.com/user/repo.git",
            last_sync_at=None,
            cleanup_after=None,
            created_at="now",
            updated_at="now",
        )

        result = await registry.call(
            "create_clone",
            {"branch_name": "main", "clone_path": "/tmp/clones/test", "task_id": "task-456"},
        )

        assert result["success"] is True
        assert result["clone"]["task_id"] == "task-456"

    @pytest.mark.asyncio
    async def test_create_clone_use_local(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Create clone with use_local clones base_branch then creates new branch."""
        mock_git_manager.full_clone.return_value = MagicMock(success=True)
        mock_git_manager.run_git_command.return_value = MagicMock(returncode=0)
        mock_git_manager.get_remote_url.return_value = "https://github.com/user/repo.git"
        mock_clone_storage.create.return_value = Clone(
            id="clone-local",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="feature",
            clone_path="/tmp/clones/local",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url="https://github.com/user/repo.git",
            last_sync_at=None,
            cleanup_after=None,
            created_at="now",
            updated_at="now",
        )

        result = await registry.call(
            "create_clone",
            {
                "branch_name": "feature",
                "clone_path": "/tmp/clones/local",
                "use_local": True,
            },
        )

        assert result["success"] is True
        assert result["clone"]["id"] == "clone-local"
        # Should use full_clone with base_branch (not branch_name) when use_local=True
        mock_git_manager.full_clone.assert_called_once()
        mock_git_manager.shallow_clone.assert_not_called()
        # Source should be the local repo path, branch should be base_branch
        call_args = mock_git_manager.full_clone.call_args
        assert call_args.kwargs["remote_url"] == str(mock_git_manager.repo_path)
        assert call_args.kwargs["branch"] == "main"
        # Should create new branch in the clone since branch_name != base_branch
        mock_git_manager.run_git_command.assert_called_once_with(
            ["checkout", "-b", "feature"],
            cwd="/tmp/clones/local",
            check=True,
        )


class TestGetClone:
    """Tests for get_clone tool."""

    @pytest.mark.asyncio
    async def test_get_clone_by_id(self, registry: Any, mock_clone_storage: Any) -> None:
        """Get clone by ID."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url="https://github.com/user/repo.git",
            last_sync_at=None,
            cleanup_after=None,
            created_at="now",
            updated_at="now",
        )

        result = await registry.call("get_clone", {"clone_id": "clone-123"})

        assert result["success"] is True
        assert result["clone"]["id"] == "clone-123"
        mock_clone_storage.get.assert_called_once_with("clone-123")

    @pytest.mark.asyncio
    async def test_get_clone_not_found(self, registry: Any, mock_clone_storage: Any) -> None:
        """Get clone returns error for nonexistent clone."""
        mock_clone_storage.get.return_value = None

        result = await registry.call("get_clone", {"clone_id": "nonexistent"})

        assert result["success"] is False
        assert "not found" in result["error"].lower()


class TestListClones:
    """Tests for list_clones tool."""

    @pytest.mark.asyncio
    async def test_list_all_clones(self, registry: Any, mock_clone_storage: Any) -> None:
        """List all clones."""
        mock_clone_storage.list_clones.return_value = [
            Clone(
                id="clone-1",
                project_id="11111111-1111-4111-8111-111111110001",
                branch_name="main",
                clone_path="/tmp/clones/one",
                base_branch="main",
                task_id=None,
                agent_session_id=None,
                status="active",
                remote_url=None,
                last_sync_at=None,
                cleanup_after=None,
                created_at="now",
                updated_at="now",
            ),
            Clone(
                id="clone-2",
                project_id="11111111-1111-4111-8111-111111110001",
                branch_name="feature",
                clone_path="/tmp/clones/two",
                base_branch="main",
                task_id=None,
                agent_session_id=None,
                status="stale",
                remote_url=None,
                last_sync_at=None,
                cleanup_after=None,
                created_at="now",
                updated_at="now",
            ),
        ]

        result = await registry.call("list_clones", {})

        assert result["success"] is True
        assert len(result["clones"]) == 2
        assert result["clones"][0]["id"] == "clone-1"
        assert result["clones"][1]["id"] == "clone-2"

    @pytest.mark.asyncio
    async def test_list_clones_with_status_filter(
        self, registry: Any, mock_clone_storage: Any
    ) -> None:
        """List clones filtered by status."""
        mock_clone_storage.list_clones.return_value = []

        await registry.call("list_clones", {"status": "active"})

        mock_clone_storage.list_clones.assert_called_once()
        call_kwargs = mock_clone_storage.list_clones.call_args.kwargs
        assert call_kwargs.get("status") == "active"

    @pytest.mark.asyncio
    async def test_list_clones_empty(self, registry: Any, mock_clone_storage: Any) -> None:
        """List clones returns empty list when no clones."""
        mock_clone_storage.list_clones.return_value = []

        result = await registry.call("list_clones", {})

        assert result["success"] is True
        assert result["clones"] == []


class TestDeleteClone:
    """Tests for delete_clone tool."""

    @pytest.mark.asyncio
    async def test_delete_clone_success(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Delete clone successfully."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at="now",
            updated_at="now",
        )
        mock_git_manager.delete_clone.return_value = MagicMock(success=True)
        mock_clone_storage.delete.return_value = True

        result = await registry.call("delete_clone", {"clone_id": "clone-123"})

        assert result["success"] is True
        mock_clone_storage.update.assert_called_once_with(
            "clone-123", status=CloneStatus.DELETING.value
        )
        mock_git_manager.delete_clone.assert_called_once()
        mock_clone_storage.delete.assert_called_once_with("clone-123")

    @pytest.mark.asyncio
    async def test_delete_clone_not_found(self, registry: Any, mock_clone_storage: Any) -> None:
        """Delete clone returns error for nonexistent clone."""
        mock_clone_storage.get.return_value = None

        result = await registry.call("delete_clone", {"clone_id": "nonexistent"})

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_delete_clone_force(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Delete clone with force flag."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at="now",
            updated_at="now",
        )
        mock_git_manager.delete_clone.return_value = MagicMock(success=True)
        mock_clone_storage.delete.return_value = True

        result = await registry.call("delete_clone", {"clone_id": "clone-123", "force": True})

        assert result["success"] is True
        mock_clone_storage.update.assert_called_once_with(
            "clone-123", status=CloneStatus.DELETING.value
        )
        call_kwargs = mock_git_manager.delete_clone.call_args.kwargs
        assert call_kwargs.get("force") is True

    @pytest.mark.asyncio
    async def test_delete_clone_file_failure_preserves_original_record(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Failed file deletion leaves the existing clone record in place."""
        original_clone = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id="task-123",
            agent_session_id=None,
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at="now",
            updated_at="now",
        )
        mock_clone_storage.get.return_value = original_clone
        mock_git_manager.delete_clone.return_value = MagicMock(
            success=False, error="permission denied", message=""
        )

        result = await registry.call("delete_clone", {"clone_id": "clone-123"})

        assert result["success"] is False
        assert "permission denied" in result["error"]
        mock_clone_storage.update.assert_any_call("clone-123", status=CloneStatus.DELETING.value)
        mock_clone_storage.update.assert_any_call("clone-123", status=CloneStatus.ACTIVE.value)
        mock_git_manager.delete_clone.assert_called_once_with("/tmp/clones/test", force=False)
        mock_clone_storage.delete.assert_not_called()
        mock_clone_storage.create.assert_not_called()
        assert mock_clone_storage.get.return_value is original_clone
        assert original_clone.id == "clone-123"
        assert original_clone.task_id == "task-123"

    @pytest.mark.asyncio
    async def test_delete_clone_record_failure_leaves_retryable_deleting_state(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Failed record deletion leaves a retryable deleting clone record."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status=CloneStatus.ACTIVE.value,
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at="now",
            updated_at="now",
        )
        mock_git_manager.delete_clone.return_value = MagicMock(success=True)
        mock_clone_storage.delete.side_effect = RuntimeError("database unavailable")

        result = await registry.call("delete_clone", {"clone_id": "clone-123"})

        assert result["success"] is False
        assert "database unavailable" in result["error"]
        mock_clone_storage.update.assert_called_once_with(
            "clone-123", status=CloneStatus.DELETING.value
        )
        mock_git_manager.delete_clone.assert_called_once_with("/tmp/clones/test", force=False)
        mock_clone_storage.delete.assert_called_once_with("clone-123")


class TestSyncClone:
    """Tests for sync_clone tool."""

    @pytest.mark.asyncio
    async def test_sync_clone_pull_success(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Sync clone pull successfully."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at="now",
            updated_at="now",
        )
        mock_git_manager.sync_clone.return_value = MagicMock(success=True)

        result = await registry.call("sync_clone", {"clone_id": "clone-123", "direction": "pull"})

        assert result["success"] is True
        mock_git_manager.sync_clone.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_clone_push_success(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Sync clone push successfully."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at="now",
            updated_at="now",
        )
        mock_git_manager.sync_clone.return_value = MagicMock(success=True)

        result = await registry.call("sync_clone", {"clone_id": "clone-123", "direction": "push"})

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_sync_clone_not_found(self, registry: Any, mock_clone_storage: Any) -> None:
        """Sync clone returns error for nonexistent clone."""
        mock_clone_storage.get.return_value = None

        result = await registry.call("sync_clone", {"clone_id": "nonexistent", "direction": "pull"})

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_sync_clone_failure(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Sync clone handles sync failure."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at="now",
            updated_at="now",
        )
        mock_git_manager.sync_clone.return_value = MagicMock(success=False, error="Network error")

        result = await registry.call("sync_clone", {"clone_id": "clone-123", "direction": "pull"})

        assert result["success"] is False


class TestMergeCloneToTarget:
    """Tests for merge_clone tool."""

    @pytest.mark.asyncio
    async def test_merge_clone_success(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Merge clone to target branch successfully."""
        from unittest.mock import MagicMock

        # Setup clone
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="feature/test",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url="https://github.com/user/repo.git",
            last_sync_at=None,
            cleanup_after=None,
            created_at="now",
            updated_at="now",
        )
        mock_clone_storage.update.return_value = MagicMock()

        # Mock fetch from clone path (returncode=0 = success)
        mock_git_manager.run_git_command.return_value = MagicMock(returncode=0, stderr="")
        # Mock merge operation
        mock_git_manager.merge_branch.return_value = MagicMock(
            success=True,
            has_conflicts=False,
        )

        result = await registry.call(
            "merge_clone",
            {"clone_id": "clone-123", "target_branch": "main"},
        )

        assert result["success"] is True
        # Should have fetched from clone path (not pushed to origin)
        fetch_call = mock_git_manager.run_git_command.call_args_list[0]
        assert "fetch" in fetch_call[0][0]
        assert "/tmp/clones/test" in fetch_call[0][0]
        # Should have set cleanup_after on success
        mock_clone_storage.update.assert_called()

    @pytest.mark.asyncio
    async def test_merge_clone_not_found(self, registry: Any, mock_clone_storage: Any) -> None:
        """Merge fails for nonexistent clone."""
        mock_clone_storage.get.return_value = None

        result = await registry.call(
            "merge_clone",
            {"clone_id": "nonexistent", "target_branch": "main"},
        )

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_merge_clone_fetch_failure(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Merge fails when fetch from clone path fails."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="feature/test",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url="https://github.com/user/repo.git",
            last_sync_at=None,
            cleanup_after=None,
            created_at="now",
            updated_at="now",
        )

        # Fetch from clone path fails
        mock_git_manager.run_git_command.return_value = MagicMock(
            returncode=1,
            stderr="fatal: not a git repository",
        )

        result = await registry.call(
            "merge_clone",
            {"clone_id": "clone-123", "target_branch": "main"},
        )

        assert result["success"] is False
        assert "fetch" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_merge_clone_with_conflicts(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Merge detects conflicts and reports them."""
        from unittest.mock import MagicMock

        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="feature/test",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url="https://github.com/user/repo.git",
            last_sync_at=None,
            cleanup_after=None,
            created_at="now",
            updated_at="now",
        )

        # Fetch succeeds
        mock_git_manager.run_git_command.return_value = MagicMock(returncode=0, stderr="")
        # Merge has conflicts - error="merge_conflict" signals conflict
        mock_git_manager.merge_branch.return_value = MagicMock(
            success=False,
            error="merge_conflict",
            message="Merge conflict in 2 files",
            output="src/foo.py\nsrc/bar.py",
        )

        result = await registry.call(
            "merge_clone",
            {"clone_id": "clone-123", "target_branch": "main"},
        )

        assert result["success"] is False
        assert result.get("has_conflicts") is True
        assert "conflicted_files" in result

    @pytest.mark.asyncio
    async def test_merge_clone_sets_cleanup_after(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Successful merge sets cleanup_after to 7 days from now."""
        from unittest.mock import MagicMock

        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="feature/test",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url="https://github.com/user/repo.git",
            last_sync_at=None,
            cleanup_after=None,
            created_at="now",
            updated_at="now",
        )
        mock_clone_storage.update.return_value = MagicMock()

        # Fetch succeeds
        mock_git_manager.run_git_command.return_value = MagicMock(returncode=0, stderr="")
        mock_git_manager.merge_branch.return_value = MagicMock(
            success=True,
            has_conflicts=False,
        )

        result = await registry.call(
            "merge_clone",
            {"clone_id": "clone-123", "target_branch": "main"},
        )

        assert result["success"] is True
        # Verify update was called with cleanup_after set
        update_calls = mock_clone_storage.update.call_args_list
        # Check if any call has cleanup_after
        has_cleanup = any("cleanup_after" in (call.kwargs or {}) for call in update_calls)
        assert has_cleanup or result.get("cleanup_after") is not None


class TestClaimClone:
    """Tests for claim_clone tool."""

    @pytest.mark.asyncio
    async def test_claim_clone_success(self, registry: Any, mock_clone_storage: Any) -> None:
        """Claim clone successfully."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at="now",
            updated_at="now",
        )
        mock_clone_storage.claim.return_value = MagicMock()

        from gobby.utils.session_context import session_context_for_test

        with session_context_for_test("sess-1"):
            result = await registry.call("claim_clone", {"clone_id": "clone-123"})

        assert result["success"] is True
        mock_clone_storage.claim.assert_called_once_with("clone-123", "sess-1")

    @pytest.mark.asyncio
    async def test_claim_clone_already_claimed(
        self, registry: Any, mock_clone_storage: Any
    ) -> None:
        """Claim fails when clone is already claimed by another session."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id="other-session",
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at="now",
            updated_at="now",
        )

        from gobby.utils.session_context import session_context_for_test

        with session_context_for_test("sess-1"):
            result = await registry.call("claim_clone", {"clone_id": "clone-123"})

        assert result["success"] is False
        assert "already claimed" in result["error"]
        mock_clone_storage.claim.assert_not_called()

    @pytest.mark.asyncio
    async def test_claim_clone_same_session(self, registry: Any, mock_clone_storage: Any) -> None:
        """Claim succeeds when clone is already claimed by same session."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id="sess-1",
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at="now",
            updated_at="now",
        )
        mock_clone_storage.claim.return_value = MagicMock()

        from gobby.utils.session_context import session_context_for_test

        with session_context_for_test("sess-1"):
            result = await registry.call("claim_clone", {"clone_id": "clone-123"})

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_claim_clone_not_found(self, registry: Any, mock_clone_storage: Any) -> None:
        """Claim fails when clone not found."""
        mock_clone_storage.get.return_value = None

        from gobby.utils.session_context import session_context_for_test

        with session_context_for_test("sess-1"):
            result = await registry.call("claim_clone", {"clone_id": "nonexistent"})

        assert result["success"] is False
        assert "not found" in result["error"].lower()


class TestReleaseClone:
    """Tests for release_clone tool."""

    @pytest.mark.asyncio
    async def test_release_clone_success(self, registry: Any, mock_clone_storage: Any) -> None:
        """Release clone successfully."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id="sess-1",
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at="now",
            updated_at="now",
        )
        mock_clone_storage.release.return_value = MagicMock()

        result = await registry.call("release_clone", {"clone_id": "clone-123"})

        assert result["success"] is True
        mock_clone_storage.release.assert_called_once_with("clone-123")

    @pytest.mark.asyncio
    async def test_release_clone_not_found(self, registry: Any, mock_clone_storage: Any) -> None:
        """Release fails when clone not found."""
        mock_clone_storage.get.return_value = None

        result = await registry.call("release_clone", {"clone_id": "nonexistent"})

        assert result["success"] is False
        assert "not found" in result["error"].lower()


class TestGetCloneByTask:
    """Tests for get_clone_by_task tool."""

    @pytest.mark.asyncio
    async def test_get_clone_by_task_found(self, registry: Any, mock_clone_storage: Any) -> None:
        """Get clone linked to task."""
        mock_clone_storage.get_by_task.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="feature/task",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id="task-456",
            agent_session_id=None,
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at="now",
            updated_at="now",
        )

        result = await registry.call("get_clone_by_task", {"task_id": "task-456"})

        assert result["success"] is True
        assert result["clone"]["id"] == "clone-123"
        assert result["clone"]["task_id"] == "task-456"
        mock_clone_storage.get_by_task.assert_called_once_with("task-456")

    @pytest.mark.asyncio
    async def test_get_clone_by_task_not_found(
        self, registry: Any, mock_clone_storage: Any
    ) -> None:
        """Get clone returns success with null clone when no clone linked to task."""
        mock_clone_storage.get_by_task.return_value = None

        result = await registry.call("get_clone_by_task", {"task_id": "task-999"})

        assert result["success"] is True
        assert result["clone"] is None


class TestLinkTaskToClone:
    """Tests for link_task_to_clone tool."""

    @pytest.mark.asyncio
    async def test_link_task_success(self, registry: Any, mock_clone_storage: Any) -> None:
        """Link task to clone successfully."""
        mock_clone_storage.get.return_value = Clone(
            id="clone-123",
            project_id="11111111-1111-4111-8111-111111110001",
            branch_name="main",
            clone_path="/tmp/clones/test",
            base_branch="main",
            task_id=None,
            agent_session_id=None,
            status="active",
            remote_url=None,
            last_sync_at=None,
            cleanup_after=None,
            created_at="now",
            updated_at="now",
        )
        mock_clone_storage.update.return_value = MagicMock()

        result = await registry.call(
            "link_task_to_clone", {"clone_id": "clone-123", "task_id": "task-456"}
        )

        assert result["success"] is True
        mock_clone_storage.update.assert_called_once_with("clone-123", task_id="task-456")

    @pytest.mark.asyncio
    async def test_link_task_clone_not_found(self, registry: Any, mock_clone_storage: Any) -> None:
        """Link task fails when clone not found."""
        mock_clone_storage.get.return_value = None

        result = await registry.call(
            "link_task_to_clone", {"clone_id": "nonexistent", "task_id": "task-456"}
        )

        assert result["success"] is False
        assert "not found" in result["error"].lower()


class TestGetCloneStats:
    """Tests for get_clone_stats tool."""

    @pytest.mark.asyncio
    async def test_get_clone_stats(self, registry: Any, mock_clone_storage: Any) -> None:
        """Get clone statistics."""
        mock_clone_storage.count_by_status.return_value = {
            "active": 3,
            "stale": 1,
            "syncing": 0,
        }

        result = await registry.call("get_clone_stats", {})

        assert result["success"] is True
        assert result["project_id"] == "11111111-1111-4111-8111-111111110001"
        assert result["counts"]["active"] == 3
        assert result["counts"]["stale"] == 1
        assert result["total"] == 4
        mock_clone_storage.count_by_status.assert_called_once_with(
            "11111111-1111-4111-8111-111111110001"
        )

    @pytest.mark.asyncio
    async def test_get_clone_stats_empty(self, registry: Any, mock_clone_storage: Any) -> None:
        """Get clone stats with no clones."""
        mock_clone_storage.count_by_status.return_value = {}

        result = await registry.call("get_clone_stats", {})

        assert result["success"] is True
        assert result["total"] == 0


class TestDetectStaleClones:
    """Tests for detect_stale_clones tool."""

    @pytest.mark.asyncio
    async def test_detect_stale_clones(self, registry: Any, mock_clone_storage: Any) -> None:
        """Detect stale clones returns results."""
        mock_clone_storage.find_stale.return_value = [
            Clone(
                id="clone-1",
                project_id="11111111-1111-4111-8111-111111110001",
                branch_name="old-feature",
                clone_path="/tmp/clones/old",
                base_branch="main",
                task_id="task-1",
                agent_session_id=None,
                status="active",
                remote_url=None,
                last_sync_at=None,
                cleanup_after=None,
                created_at="old",
                updated_at="old",
            ),
        ]

        result = await registry.call("detect_stale_clones", {"hours": 48, "limit": 10})

        assert result["success"] is True
        assert result["count"] == 1
        assert result["threshold_hours"] == 48
        assert result["stale_clones"][0]["id"] == "clone-1"
        assert result["stale_clones"][0]["task_id"] == "task-1"
        mock_clone_storage.find_stale.assert_called_once_with(
            project_id="11111111-1111-4111-8111-111111110001", hours=48, limit=10
        )

    @pytest.mark.asyncio
    async def test_detect_stale_clones_empty(self, registry: Any, mock_clone_storage: Any) -> None:
        """Detect stale clones returns empty list."""
        mock_clone_storage.find_stale.return_value = []

        result = await registry.call("detect_stale_clones", {})

        assert result["success"] is True
        assert result["count"] == 0
        assert result["stale_clones"] == []


class TestCleanupStaleClones:
    """Tests for cleanup_stale_clones tool."""

    @pytest.mark.asyncio
    async def test_cleanup_dry_run(self, registry: Any, mock_clone_storage: Any) -> None:
        """Cleanup in dry_run mode reports but doesn't clean."""
        mock_clone_storage.cleanup_stale.return_value = [
            Clone(
                id="clone-1",
                project_id="11111111-1111-4111-8111-111111110001",
                branch_name="old-feature",
                clone_path="/tmp/clones/old",
                base_branch="main",
                task_id=None,
                agent_session_id=None,
                status="active",
                remote_url=None,
                last_sync_at=None,
                cleanup_after=None,
                created_at="old",
                updated_at="old",
            ),
        ]

        result = await registry.call("cleanup_stale_clones", {"hours": 24, "dry_run": True})

        assert result["success"] is True
        assert result["dry_run"] is True
        assert result["count"] == 1
        assert result["cleaned"][0]["marked_stale"] is False
        assert result["cleaned"][0]["files_deleted"] is False
        mock_clone_storage.cleanup_stale.assert_called_once_with(
            project_id="11111111-1111-4111-8111-111111110001", hours=24, dry_run=True
        )

    @pytest.mark.asyncio
    async def test_cleanup_actual_run(self, registry: Any, mock_clone_storage: Any) -> None:
        """Cleanup marks stale clones."""
        mock_clone_storage.cleanup_stale.return_value = [
            Clone(
                id="clone-1",
                project_id="11111111-1111-4111-8111-111111110001",
                branch_name="old-feature",
                clone_path="/tmp/clones/old",
                base_branch="main",
                task_id=None,
                agent_session_id=None,
                status="stale",
                remote_url=None,
                last_sync_at=None,
                cleanup_after=None,
                created_at="old",
                updated_at="old",
            ),
        ]

        result = await registry.call("cleanup_stale_clones", {"hours": 24, "dry_run": False})

        assert result["success"] is True
        assert result["dry_run"] is False
        assert result["cleaned"][0]["marked_stale"] is True
        assert result["cleaned"][0]["files_deleted"] is False

    @pytest.mark.asyncio
    async def test_cleanup_with_delete_files(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Cleanup deletes clone files when delete_files=True."""
        mock_clone_storage.cleanup_stale.return_value = [
            Clone(
                id="clone-1",
                project_id="11111111-1111-4111-8111-111111110001",
                branch_name="old-feature",
                clone_path="/tmp/clones/old",
                base_branch="main",
                task_id=None,
                agent_session_id=None,
                status="stale",
                remote_url=None,
                last_sync_at=None,
                cleanup_after=None,
                created_at="old",
                updated_at="old",
            ),
        ]
        mock_git_manager.delete_clone.return_value = MagicMock(success=True)

        result = await registry.call(
            "cleanup_stale_clones",
            {"hours": 24, "dry_run": False, "delete_files": True},
        )

        assert result["success"] is True
        assert result["cleaned"][0]["marked_stale"] is True
        assert result["cleaned"][0]["files_deleted"] is True
        mock_git_manager.delete_clone.assert_called_once_with("/tmp/clones/old", force=True)

    @pytest.mark.asyncio
    async def test_cleanup_delete_files_failure(
        self, registry: Any, mock_clone_storage: Any, mock_git_manager: Any
    ) -> None:
        """Cleanup reports file deletion failure."""
        mock_clone_storage.cleanup_stale.return_value = [
            Clone(
                id="clone-1",
                project_id="11111111-1111-4111-8111-111111110001",
                branch_name="old-feature",
                clone_path="/tmp/clones/old",
                base_branch="main",
                task_id=None,
                agent_session_id=None,
                status="stale",
                remote_url=None,
                last_sync_at=None,
                cleanup_after=None,
                created_at="old",
                updated_at="old",
            ),
        ]
        mock_git_manager.delete_clone.return_value = MagicMock(
            success=False, error="Permission denied"
        )

        result = await registry.call(
            "cleanup_stale_clones",
            {"hours": 24, "dry_run": False, "delete_files": True},
        )

        assert result["success"] is True
        assert result["cleaned"][0]["files_deleted"] is False
        assert result["cleaned"][0]["delete_error"] == "Permission denied"

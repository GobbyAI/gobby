"""Tests for MCP tools task ID resolution.

These tests verify that MCP tools correctly resolve task references:
- `#N` format (e.g., #1, #47) resolves to correct UUID before processing
- UUID format passes through unchanged
- Path format (e.g., 1.2.3) resolves to correct UUID
- `gt-*` format returns 'unknown format' error (no longer special-cased)
- Error response includes helpful information for all error cases
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.tasks import LocalTaskManager, Task, TaskNotFoundError
from gobby.sync.tasks import TaskSyncManager

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_task_manager():
    """Create a mock task manager."""
    manager = MagicMock(spec=LocalTaskManager)
    manager.db = MagicMock()
    return manager


@pytest.fixture
def mock_sync_manager():
    """Create a mock sync manager."""
    return MagicMock(spec=TaskSyncManager)


@pytest.fixture
def sample_task_uuid():
    """Create a sample task with UUID-format ID."""
    task_id = str(uuid.uuid4())
    return Task(
        id=task_id,
        project_id="proj-1",
        title="Test Task",
        priority=2,
        task_type="task",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        description="Test description",
        labels=["test"],
        seq_num=1,
        path_cache="1",
    )


class TestResolveTaskIdForMCP:
    """Tests for the resolve_task_id_for_mcp helper function."""

    def test_resolve_uuid_passthrough(self, mock_task_manager, sample_task_uuid) -> None:
        """Test that valid UUID passes through to get_task."""
        from gobby.mcp_proxy.tools.tasks import resolve_task_id_for_mcp

        mock_task_manager.get_task.return_value = sample_task_uuid

        result = resolve_task_id_for_mcp(
            mock_task_manager, sample_task_uuid.id, project_id="proj-1"
        )

        assert result == sample_task_uuid.id
        mock_task_manager.get_task.assert_called_once_with(sample_task_uuid.id)

    def test_resolve_hash_format_success(self, mock_task_manager, sample_task_uuid) -> None:
        """Test #N format resolution to UUID."""
        from gobby.mcp_proxy.tools.tasks import resolve_task_id_for_mcp

        mock_task_manager.resolve_task_reference.return_value = sample_task_uuid.id
        mock_task_manager.get_task.return_value = sample_task_uuid

        result = resolve_task_id_for_mcp(mock_task_manager, "#1", project_id="proj-1")

        assert result == sample_task_uuid.id
        mock_task_manager.resolve_task_reference.assert_called_once_with("#1", "proj-1")

    def test_resolve_hash_format_not_found(self, mock_task_manager) -> None:
        """Test #N format when task doesn't exist."""
        from gobby.mcp_proxy.tools.tasks import resolve_task_id_for_mcp

        mock_task_manager.resolve_task_reference.side_effect = TaskNotFoundError(
            "Task #999 not found in project"
        )

        with pytest.raises(TaskNotFoundError) as exc_info:
            resolve_task_id_for_mcp(mock_task_manager, "#999", project_id="proj-1")

        assert "#999" in str(exc_info.value)

    def test_resolve_path_format_success(self, mock_task_manager, sample_task_uuid) -> None:
        """Test path format (e.g., 1.2.3) resolution."""
        from gobby.mcp_proxy.tools.tasks import resolve_task_id_for_mcp

        mock_task_manager.resolve_task_reference.return_value = sample_task_uuid.id

        result = resolve_task_id_for_mcp(mock_task_manager, "1.2.3", project_id="proj-1")

        assert result == sample_task_uuid.id
        mock_task_manager.resolve_task_reference.assert_called_once_with("1.2.3", "proj-1")

    def test_resolve_gt_format_returns_error(self, mock_task_manager) -> None:
        """Test gt-* format returns 'task not found' error (treated as invalid UUID)."""
        from gobby.mcp_proxy.tools.tasks import resolve_task_id_for_mcp

        # gt-* format doesn't match #N, path, or digit patterns, so it's treated as UUID
        # get_task returns None for invalid UUID, which triggers TaskNotFoundError
        mock_task_manager.get_task.return_value = None

        with pytest.raises(TaskNotFoundError) as exc_info:
            resolve_task_id_for_mcp(mock_task_manager, "gt-abc123", project_id="proj-1")

        assert "gt-abc123" in str(exc_info.value)


class TestMCPGetTaskWithHashFormat:
    """Tests for get_task MCP tool with #N format."""

    def test_get_task_with_hash_format(
        self, mock_task_manager, mock_sync_manager, sample_task_uuid
    ) -> None:
        """Test get_task resolves #N format correctly."""
        from gobby.mcp_proxy.tools.tasks import create_task_registry

        mock_task_manager.resolve_task_reference.return_value = sample_task_uuid.id
        mock_task_manager.get_task.return_value = sample_task_uuid
        mock_task_manager.db = MagicMock()

        # Create registry and get the get_task tool
        registry = create_task_registry(mock_task_manager, mock_sync_manager)
        get_task_func = registry._tools["get_task"].func

        # Call with #1 format
        with patch(
            "gobby.mcp_proxy.tools.tasks._resolution.get_project_context",
            return_value={"id": "proj-1"},
        ):
            result = get_task_func(task_id="#1")

        # Should resolve to UUID and return task data
        assert result.get("id") == sample_task_uuid.id
        mock_task_manager.resolve_task_reference.assert_called_with("#1", "proj-1")

    def test_get_task_with_uuid_format(
        self, mock_task_manager, mock_sync_manager, sample_task_uuid
    ) -> None:
        """Test get_task passes through UUID format."""
        from gobby.mcp_proxy.tools.tasks import create_task_registry

        mock_task_manager.get_task.return_value = sample_task_uuid
        mock_task_manager.db = MagicMock()

        registry = create_task_registry(mock_task_manager, mock_sync_manager)
        get_task_func = registry._tools["get_task"].func

        with patch(
            "gobby.mcp_proxy.tools.tasks._resolution.get_project_context",
            return_value={"id": "proj-1"},
        ):
            result = get_task_func(task_id=sample_task_uuid.id)

        assert result.get("id") == sample_task_uuid.id

    def test_get_task_with_gt_format_error(self, mock_task_manager, mock_sync_manager) -> None:
        """Test get_task returns error for gt-* format (treated as invalid UUID)."""
        from gobby.mcp_proxy.tools.tasks import create_task_registry

        mock_task_manager.db = MagicMock()
        # gt-* format is treated as UUID, get_task returns None for invalid UUID
        mock_task_manager.get_task.return_value = None

        registry = create_task_registry(mock_task_manager, mock_sync_manager)
        get_task_func = registry._tools["get_task"].func

        with patch(
            "gobby.mcp_proxy.tools.tasks._resolution.get_project_context",
            return_value={"id": "proj-1"},
        ):
            result = get_task_func(task_id="gt-abc123")

        assert "error" in result
        assert "not found" in result["error"].lower() or "gt-abc123" in result["error"].lower()


class TestMCPUpdateTaskWithHashFormat:
    """Tests for update_task MCP tool with #N format."""

    def test_update_task_with_hash_format(
        self, mock_task_manager, mock_sync_manager, sample_task_uuid
    ) -> None:
        """Test update_task resolves #N format correctly."""
        from gobby.mcp_proxy.tools.tasks import create_task_registry

        mock_task_manager.resolve_task_reference.return_value = sample_task_uuid.id
        mock_task_manager.get_task.return_value = sample_task_uuid
        mock_task_manager.update_task.return_value = sample_task_uuid
        mock_task_manager.db = MagicMock()

        registry = create_task_registry(mock_task_manager, mock_sync_manager)
        update_task_func = registry._tools["update_task"].func

        with patch(
            "gobby.mcp_proxy.tools.tasks._resolution.get_project_context",
            return_value={"id": "proj-1"},
        ):
            # Note: status="in_progress", "closed", "review" are blocked
            # Use priority update instead to test #N format resolution
            update_task_func(task_id="#5", priority=1)

        mock_task_manager.resolve_task_reference.assert_called_with("#5", "proj-1")
        assert mock_task_manager.resolve_task_reference.call_count >= 1
        assert mock_task_manager.resolve_task_reference.call_args is not None
        # Update should be called with the resolved UUID
        mock_task_manager.update_task.assert_called_once()
        assert mock_task_manager.update_task.call_count == 1
        assert mock_task_manager.update_task.call_args is not None


class TestMCPCloseTaskWithHashFormat:
    """Tests for close_task MCP tool with #N format."""

    @pytest.mark.asyncio
    async def test_close_task_with_hash_format(
        self, mock_task_manager, mock_sync_manager, sample_task_uuid
    ):
        """Test close_task resolves #N format correctly."""
        from gobby.mcp_proxy.tools.tasks import create_task_registry

        mock_task_manager.resolve_task_reference.return_value = sample_task_uuid.id
        mock_task_manager.get_task.return_value = sample_task_uuid
        mock_task_manager.close_task.return_value = sample_task_uuid
        mock_task_manager.list_tasks.return_value = []  # No children
        mock_task_manager.db = MagicMock()

        registry = create_task_registry(mock_task_manager, mock_sync_manager)
        close_task_func = registry._tools["close_task"].func

        with patch(
            "gobby.mcp_proxy.tools.tasks._resolution.get_project_context",
            return_value={"id": "proj-1"},
        ):
            await close_task_func(
                task_id="#10",
                reason="obsolete",
            )

        mock_task_manager.resolve_task_reference.assert_called_with("#10", "proj-1")
        assert mock_task_manager.resolve_task_reference.call_count >= 1
        assert mock_task_manager.resolve_task_reference.call_args is not None


class TestIntegrationMCPTaskIdResolution:
    """Integration tests using real database for MCP task ID resolution."""

    @pytest.mark.integration
    def test_mcp_get_task_with_hash_format(self, temp_db, sample_project) -> None:
        """Test MCP get_task with #N format using real database."""
        from gobby.mcp_proxy.tools.tasks import create_task_registry
        from gobby.storage.tasks import LocalTaskManager
        from gobby.sync.tasks import TaskSyncManager

        manager = LocalTaskManager(temp_db)
        sync_manager = TaskSyncManager(manager)
        project_id = sample_project["id"]

        # Create tasks
        task1 = manager.create_task(project_id=project_id, title="Task 1")
        task2 = manager.create_task(project_id=project_id, title="Task 2")

        registry = create_task_registry(manager, sync_manager)
        get_task_func = registry._tools["get_task"].func

        # Test #1 resolution
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.get_project_context",
                return_value={"id": project_id},
            ),
            patch(
                "gobby.mcp_proxy.tools.tasks._resolution.get_project_context",
                return_value={"id": project_id},
            ),
        ):
            result = get_task_func(task_id="#1")

        assert result.get("id") == task1.id
        assert result.get("title") == "Task 1"

        # Test #2 resolution
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.get_project_context",
                return_value={"id": project_id},
            ),
            patch(
                "gobby.mcp_proxy.tools.tasks._resolution.get_project_context",
                return_value={"id": project_id},
            ),
        ):
            result = get_task_func(task_id="#2")

        assert result.get("id") == task2.id
        assert result.get("title") == "Task 2"

    @pytest.mark.integration
    def test_mcp_get_task_with_path_format(self, temp_db, sample_project) -> None:
        """Test MCP get_task with path format using real database."""
        from gobby.mcp_proxy.tools.tasks import create_task_registry
        from gobby.storage.tasks import LocalTaskManager
        from gobby.sync.tasks import TaskSyncManager

        manager = LocalTaskManager(temp_db)
        sync_manager = TaskSyncManager(manager)
        project_id = sample_project["id"]

        # Create hierarchy
        parent = manager.create_task(project_id=project_id, title="Parent")
        child = manager.create_task(project_id=project_id, title="Child", parent_task_id=parent.id)

        registry = create_task_registry(manager, sync_manager)
        get_task_func = registry._tools["get_task"].func

        # Test path resolution (1.2)
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.get_project_context",
                return_value={"id": project_id},
            ),
            patch(
                "gobby.mcp_proxy.tools.tasks._resolution.get_project_context",
                return_value={"id": project_id},
            ),
        ):
            result = get_task_func(task_id="1.2")

        assert result.get("id") == child.id
        assert result.get("title") == "Child"

    @pytest.mark.integration
    def test_mcp_get_task_with_gt_format_error(self, temp_db, sample_project) -> None:
        """Test MCP get_task returns error for gt-* format (unknown format)."""
        from gobby.mcp_proxy.tools.tasks import create_task_registry
        from gobby.storage.tasks import LocalTaskManager
        from gobby.sync.tasks import TaskSyncManager

        manager = LocalTaskManager(temp_db)
        sync_manager = TaskSyncManager(manager)
        project_id = sample_project["id"]

        manager.create_task(project_id=project_id, title="Task 1")

        registry = create_task_registry(manager, sync_manager)
        get_task_func = registry._tools["get_task"].func

        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.get_project_context",
                return_value={"id": project_id},
            ),
            patch(
                "gobby.mcp_proxy.tools.tasks._resolution.get_project_context",
                return_value={"id": project_id},
            ),
        ):
            result = get_task_func(task_id="gt-abc123")

        assert "error" in result
        assert "not found" in result["error"].lower() or "unknown" in result["error"].lower()

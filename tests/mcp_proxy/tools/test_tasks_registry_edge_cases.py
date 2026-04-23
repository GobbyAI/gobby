"""Focused coverage tests for task MCP tools."""

from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks import SKIP_REASONS, create_task_registry
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit


class TestSkipReasons:
    """Tests for SKIP_REASONS constant."""

    def test_skip_reasons_contains_expected_values(self) -> None:
        """Test that SKIP_REASONS contains all expected values."""
        assert "duplicate" in SKIP_REASONS
        assert "already_implemented" in SKIP_REASONS
        assert "wont_fix" in SKIP_REASONS
        assert "obsolete" in SKIP_REASONS

    def test_skip_reasons_is_frozenset(self) -> None:
        """Test that SKIP_REASONS is immutable."""
        assert isinstance(SKIP_REASONS, frozenset)


# =============================================================================
# create_task Tool Tests
# =============================================================================



class TestSessionIntegrationTools:
    """Tests for session integration MCP tools."""

    @pytest.fixture(autouse=True)
    def _set_session_context(self):
        with session_context_for_test("sess-123"):
            yield

    @pytest.mark.asyncio
    async def test_link_task_to_session_success(self, mock_task_manager, mock_sync_manager):
        """Test link_task_to_session creates a link."""
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
        ):
            mock_st_instance = MagicMock()
            MockSessionTaskManager.return_value = mock_st_instance

            # Mock session manager to return the session_id as-is
            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "sess-123"
            MockSessionManager.return_value = mock_session_manager

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            result = await registry.call(
                "link_task_to_session",
                {
                    "task_id": "550e8400-e29b-41d4-a716-446655440000",
                    "action": "worked_on",
                },
            )

            mock_st_instance.link_task.assert_called_with(
                "sess-123", "550e8400-e29b-41d4-a716-446655440000", "worked_on"
            )
            assert result == {}

    @pytest.mark.asyncio
    async def test_link_task_to_session_missing_session_id(
        self, mock_task_manager, mock_sync_manager
    ):
        """Test link_task_to_session requires session context."""
        from gobby.utils.session_context import reset_session_context, set_session_context

        # Override autouse fixture: clear session context
        token = set_session_context(None)
        try:
            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            result = await registry.call(
                "link_task_to_session", {"task_id": "550e8400-e29b-41d4-a716-446655440000"}
            )

            assert "error" in result
        finally:
            reset_session_context(token)

    @pytest.mark.asyncio
    async def test_link_task_to_session_error(self, mock_task_manager, mock_sync_manager):
        """Test link_task_to_session handles errors."""
        with patch(
            "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
        ) as MockSessionTaskManager:
            mock_st_instance = MagicMock()
            mock_st_instance.link_task.side_effect = ValueError("Invalid task")
            MockSessionTaskManager.return_value = mock_st_instance

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            result = await registry.call(
                "link_task_to_session",
                {"task_id": "00000000-0000-0000-0000-000000000000"},
            )

            assert "error" in result

    @pytest.mark.asyncio
    async def test_get_session_tasks(self, mock_task_manager, mock_sync_manager):
        """Test get_session_tasks returns tasks for a session."""
        with (
            patch(
                "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
            ) as MockSessionTaskManager,
            patch("gobby.mcp_proxy.tools.tasks._context.SessionManager") as MockSessionManager,
        ):
            mock_st_instance = MagicMock()
            mock_st_instance.get_session_tasks.return_value = [
                {"task_id": "t1", "action": "worked_on"}
            ]
            MockSessionTaskManager.return_value = mock_st_instance

            # Mock session manager to return the session_id as-is
            mock_session_manager = MagicMock()
            mock_session_manager.resolve_session_reference.return_value = "sess-123"
            MockSessionManager.return_value = mock_session_manager

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            result = await registry.call("get_session_tasks", {"session_id": "sess-123"})

            assert result["session_id"] == "sess-123"
            assert len(result["tasks"]) == 1

    @pytest.mark.asyncio
    async def test_get_task_sessions(self, mock_task_manager, mock_sync_manager):
        """Test get_task_sessions returns sessions for a task."""
        with patch(
            "gobby.mcp_proxy.tools.tasks._context.SessionTaskManager"
        ) as MockSessionTaskManager:
            mock_st_instance = MagicMock()
            mock_st_instance.get_task_sessions.return_value = [
                {"session_id": "sess-1", "action": "created"}
            ]
            MockSessionTaskManager.return_value = mock_st_instance

            registry = create_task_registry(mock_task_manager, mock_sync_manager)

            result = await registry.call(
                "get_task_sessions", {"task_id": "550e8400-e29b-41d4-a716-446655440000"}
            )

            assert result["task_id"] == "550e8400-e29b-41d4-a716-446655440000"
            assert len(result["sessions"]) == 1


# =============================================================================
# Registry Integration Tests
# =============================================================================



class TestRegistryIntegration:
    """Tests for registry merging and tool availability."""

    def test_registry_name_and_description(self, task_registry) -> None:
        """Test registry has correct name and description."""
        assert task_registry.name == "gobby-tasks"
        assert "Task management" in task_registry.description

    def test_crud_tools_registered(self, task_registry) -> None:
        """Test all CRUD tools are registered."""
        crud_tools = [
            "create_task",
            "get_task",
            "update_task",
            "close_task",
            "delete_task",
            "list_tasks",
        ]

        tools_list = task_registry.list_tools()
        tool_names = [t["name"] for t in tools_list]

        for tool_name in crud_tools:
            assert tool_name in tool_names, f"Missing CRUD tool: {tool_name}"

    def test_label_tools_registered(self, task_registry) -> None:
        """Test label tools are registered."""
        label_tools = ["add_label", "remove_label"]

        tools_list = task_registry.list_tools()
        tool_names = [t["name"] for t in tools_list]

        for tool_name in label_tools:
            assert tool_name in tool_names, f"Missing label tool: {tool_name}"

    def test_session_tools_registered(self, task_registry) -> None:
        """Test session integration tools are registered."""
        session_tools = ["link_task_to_session", "get_session_tasks", "get_task_sessions"]

        tools_list = task_registry.list_tools()
        tool_names = [t["name"] for t in tools_list]

        for tool_name in session_tools:
            assert tool_name in tool_names, f"Missing session tool: {tool_name}"

    def test_reopen_task_registered(self, task_registry) -> None:
        """Test reopen_task is registered."""
        tools_list = task_registry.list_tools()
        tool_names = [t["name"] for t in tools_list]

        assert "reopen_task" in tool_names

    def test_merged_registries_available(self, task_registry) -> None:
        """Test tools from merged registries are available."""
        merged_tools = [
            # From task_dependencies
            "add_dependency",
            "remove_dependency",
            # From task_readiness
            "list_ready_tasks",
            "list_blocked_tasks",
            # From task_sync (commit tools only)
            "link_commit",
            "auto_link_commits",
            # From affected_files (core only)
            "update_observed_files",
        ]

        tools_list = task_registry.list_tools()
        tool_names = [t["name"] for t in tools_list]

        for tool_name in merged_tools:
            assert tool_name in tool_names, f"Missing merged tool: {tool_name}"


# =============================================================================
# Schema Tests
# =============================================================================

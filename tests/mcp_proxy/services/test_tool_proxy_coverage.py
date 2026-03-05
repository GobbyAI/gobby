"""Additional coverage tests for ToolProxyService in services/tool_proxy.py.

Targets uncovered lines: 97-104, 125, 133, 183, 188-220, 250-278, 280-309
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.models import MCPError
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService, safe_truncate

pytestmark = pytest.mark.unit


class TestSafeTruncate:
    """Tests for safe_truncate helper function."""

    def test_safe_truncate_none(self) -> None:
        """Test truncating None returns empty string."""
        assert safe_truncate(None) == ""

    def test_safe_truncate_bytes(self) -> None:
        """Test truncating bytes converts to string."""
        result = safe_truncate(b"hello world", 100)
        assert result == "hello world"

    def test_safe_truncate_short_text(self) -> None:
        """Test short text is not truncated."""
        result = safe_truncate("short text", 100)
        assert result == "short text"

    def test_safe_truncate_long_text(self) -> None:
        """Test long text is truncated with ellipsis."""
        long_text = "a" * 150
        result = safe_truncate(long_text, 100)
        assert len(result) == 103  # 100 chars + "..."
        assert result.endswith("...")

    def test_safe_truncate_unicode_bytes(self) -> None:
        """Test truncating bytes with unicode chars."""
        result = safe_truncate("héllo wörld".encode(), 100)
        assert "héllo wörld" in result


class TestListToolsInternalServer:
    """Tests for list_tools with internal servers (lines 97-104)."""

    @pytest.fixture
    def mock_mcp_manager(self):
        """Create a mock MCP manager."""
        manager = MagicMock()
        manager.project_id = "test-project"
        manager.has_server.return_value = False
        return manager

    @pytest.fixture
    def mock_internal_manager(self):
        """Create a mock internal registry manager."""
        manager = MagicMock()
        return manager

    @pytest.mark.asyncio
    async def test_list_tools_internal_server_success(
        self, mock_mcp_manager, mock_internal_manager
    ):
        """Test listing tools from an internal server."""
        mock_internal_manager.is_internal.return_value = True
        mock_registry = MagicMock()
        mock_registry.list_tools.return_value = [
            {"name": "tool1", "brief": "First tool"},
            {"name": "tool2", "brief": "Second tool"},
        ]
        mock_internal_manager.get_registry.return_value = mock_registry

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
        )

        result = await proxy.list_tools("gobby-tasks")

        assert result["success"] is True
        assert result["tool_count"] == 2
        assert len(result["tools"]) == 2
        mock_internal_manager.get_registry.assert_called_once_with("gobby-tasks")

    @pytest.mark.asyncio
    async def test_list_tools_internal_server_not_found(
        self, mock_mcp_manager, mock_internal_manager
    ):
        """Test listing tools from non-existent internal server."""
        mock_internal_manager.is_internal.return_value = True
        mock_internal_manager.get_registry.return_value = None

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
        )

        result = await proxy.list_tools("gobby-nonexistent")

        assert result["success"] is False
        assert "not found" in result["error"]
        assert result["tools"] == []

    @pytest.mark.asyncio
    async def test_list_tools_internal_with_tool_filter(
        self, mock_mcp_manager, mock_internal_manager
    ):
        """Test listing tools with workflow phase filtering."""
        mock_internal_manager.is_internal.return_value = True
        mock_registry = MagicMock()
        mock_registry.list_tools.return_value = [
            {"name": "tool1", "brief": "First tool"},
            {"name": "tool2", "brief": "Second tool"},
            {"name": "tool3", "brief": "Third tool"},
        ]
        mock_internal_manager.get_registry.return_value = mock_registry

        mock_filter = MagicMock()
        mock_filter.filter_tools.return_value = [
            {"name": "tool1", "brief": "First tool"},
        ]

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
            tool_filter=mock_filter,
        )

        result = await proxy.list_tools("gobby-tasks", session_id="session-123")

        assert result["success"] is True
        assert result["tool_count"] == 1
        mock_filter.filter_tools.assert_called_once()


class TestListToolsExternalServer:
    """Tests for list_tools with external servers (lines 125, 133)."""

    @pytest.fixture
    def mock_mcp_manager(self):
        """Create a mock MCP manager."""
        manager = MagicMock()
        manager.project_id = "test-project"
        return manager

    @pytest.fixture
    def mock_internal_manager(self):
        """Create a mock internal registry manager."""
        manager = MagicMock()
        manager.is_internal.return_value = False
        return manager

    @pytest.mark.asyncio
    async def test_list_tools_external_object_tools(self, mock_mcp_manager, mock_internal_manager):
        """Test listing tools when tools are objects not dicts."""
        mock_mcp_manager.has_server.return_value = True

        # Create mock tool objects (not dicts)
        mock_tool1 = MagicMock()
        mock_tool1.name = "external_tool"
        mock_tool1.description = "A tool from an external server"

        mock_tool2 = MagicMock()
        mock_tool2.name = "another_tool"
        mock_tool2.description = "Another external tool"

        mock_mcp_manager.list_tools = AsyncMock(
            return_value={"ext-server": [mock_tool1, mock_tool2]}
        )

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
        )

        result = await proxy.list_tools("ext-server")

        assert result["success"] is True
        assert result["tool_count"] == 2
        assert result["tools"][0]["name"] == "external_tool"
        assert result["tools"][1]["name"] == "another_tool"

    @pytest.mark.asyncio
    async def test_list_tools_external_with_filter(self, mock_mcp_manager, mock_internal_manager):
        """Test external tools with workflow phase filtering."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.list_tools = AsyncMock(
            return_value={
                "ext-server": [
                    {"name": "tool1", "description": "Tool 1"},
                    {"name": "tool2", "description": "Tool 2"},
                ]
            }
        )

        mock_filter = MagicMock()
        mock_filter.filter_tools.return_value = [
            {"name": "tool1", "brief": "Tool 1"},
        ]

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
            tool_filter=mock_filter,
        )

        result = await proxy.list_tools("ext-server", session_id="session-456")

        assert result["success"] is True
        assert result["tool_count"] == 1
        mock_filter.filter_tools.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_tools_server_not_found(self, mock_mcp_manager, mock_internal_manager):
        """Test listing tools from non-existent server."""
        mock_mcp_manager.has_server.return_value = False

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
        )

        result = await proxy.list_tools("nonexistent-server")

        assert result["success"] is False
        assert "not found" in result["error"]


class TestCallToolInternal:
    """Tests for call_tool with internal servers (lines 183)."""

    @pytest.fixture
    def mock_mcp_manager(self):
        """Create a mock MCP manager."""
        manager = MagicMock()
        manager.project_id = "test-project"
        return manager

    @pytest.fixture
    def mock_internal_manager(self):
        """Create a mock internal registry manager."""
        return MagicMock()

    @pytest.mark.asyncio
    async def test_call_tool_internal_not_found(self, mock_mcp_manager, mock_internal_manager):
        """Test calling tool on non-existent internal server raises MCPError."""
        mock_internal_manager.is_internal.return_value = True
        mock_internal_manager.get_registry.return_value = None

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
            validate_arguments=False,
        )

        result = await proxy.call_tool("gobby-nonexistent", "some_tool", {})

        assert result["success"] is False
        assert "not found" in result["error"]


class TestCallToolFallback:
    """Tests for call_tool exception handling with fallback (lines 188-220)."""

    @pytest.fixture
    def mock_mcp_manager(self):
        """Create a mock MCP manager."""
        manager = MagicMock()
        manager.project_id = "test-project"
        return manager

    @pytest.fixture
    def mock_internal_manager(self):
        """Create a mock internal registry manager."""
        manager = MagicMock()
        manager.is_internal.return_value = False
        return manager

    @pytest.mark.asyncio
    async def test_call_tool_exception_with_fallback(self, mock_mcp_manager, mock_internal_manager):
        """Test that fallback suggestions are included on error."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.call_tool = AsyncMock(side_effect=Exception("Tool failed"))

        mock_fallback = AsyncMock()
        mock_fallback.find_alternatives_for_error = AsyncMock(
            return_value=[
                {"tool": "alt_tool", "server": "other-server", "score": 0.8},
            ]
        )

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
            validate_arguments=False,
            fallback_resolver=mock_fallback,
        )

        result = await proxy.call_tool("test-server", "failing_tool", {})

        assert result["success"] is False
        assert "Tool failed" in result["error"]
        assert "fallback_suggestions" in result
        assert len(result["fallback_suggestions"]) == 1

        mock_fallback.find_alternatives_for_error.assert_called_once_with(
            server_name="test-server",
            tool_name="failing_tool",
            error_message="Tool failed",
            project_id="test-project",
        )

    @pytest.mark.asyncio
    async def test_call_tool_exception_no_project_id(self, mock_internal_manager):
        """Test fallback when project_id is not available."""
        mock_mcp_manager = MagicMock()
        mock_mcp_manager.project_id = None
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.call_tool = AsyncMock(side_effect=Exception("Tool failed"))

        mock_fallback = AsyncMock()

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
            validate_arguments=False,
            fallback_resolver=mock_fallback,
        )

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            result = await proxy.call_tool("test-server", "failing_tool", {})

        assert result["success"] is False
        assert result["fallback_suggestions"] == []
        # Fallback resolver should not be called without project_id
        mock_fallback.find_alternatives_for_error.assert_not_called()

    @pytest.mark.asyncio
    async def test_call_tool_fallback_fails(self, mock_mcp_manager, mock_internal_manager):
        """Test when fallback resolver itself fails."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.call_tool = AsyncMock(side_effect=Exception("Tool failed"))

        mock_fallback = AsyncMock()
        mock_fallback.find_alternatives_for_error = AsyncMock(
            side_effect=RuntimeError("Fallback error")
        )

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
            validate_arguments=False,
            fallback_resolver=mock_fallback,
        )

        result = await proxy.call_tool("test-server", "failing_tool", {})

        assert result["success"] is False
        assert "Tool failed" in result["error"]
        assert result["fallback_suggestions"] == []

    @pytest.mark.asyncio
    async def test_call_tool_no_fallback_resolver(self, mock_mcp_manager, mock_internal_manager):
        """Test error response without fallback resolver."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.call_tool = AsyncMock(side_effect=Exception("Tool failed"))

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
            validate_arguments=False,
            fallback_resolver=None,
        )

        result = await proxy.call_tool("test-server", "failing_tool", {})

        assert result["success"] is False
        assert result["fallback_suggestions"] == []


class TestFindToolServer:
    """Tests for find_tool_server method (lines 250-278)."""

    @pytest.fixture
    def mock_mcp_manager(self):
        """Create a mock MCP manager."""
        manager = MagicMock()
        manager.project_id = "test-project"
        manager._configs = {}
        return manager

    @pytest.fixture
    def mock_internal_manager(self):
        """Create a mock internal registry manager."""
        return MagicMock()

    def test_find_tool_server_internal(self, mock_mcp_manager, mock_internal_manager) -> None:
        """Test finding tool in internal registry."""
        mock_internal_manager.find_tool_server.return_value = "gobby-tasks"

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
        )

        result = proxy.find_tool_server("create_task")

        assert result == "gobby-tasks"
        mock_internal_manager.find_tool_server.assert_called_once_with("create_task")

    def test_find_tool_server_external_dict_tools(
        self, mock_mcp_manager, mock_internal_manager
    ) -> None:
        """Test finding tool in external server configs with dict tools."""
        mock_internal_manager.find_tool_server.return_value = None

        mock_config = MagicMock()
        mock_config.tools = [
            {"name": "external_tool", "description": "External tool"},
            {"name": "another_tool", "description": "Another tool"},
        ]
        mock_mcp_manager._configs = {"ext-server": mock_config}

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
        )

        result = proxy.find_tool_server("external_tool")

        assert result == "ext-server"

    def test_find_tool_server_external_object_tools(
        self, mock_mcp_manager, mock_internal_manager
    ) -> None:
        """Test finding tool in external server configs with object tools."""
        mock_internal_manager.find_tool_server.return_value = None

        mock_tool = MagicMock()
        mock_tool.name = "object_tool"

        mock_config = MagicMock()
        mock_config.tools = [mock_tool]
        mock_mcp_manager._configs = {"ext-server": mock_config}

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
        )

        result = proxy.find_tool_server("object_tool")

        assert result == "ext-server"

    def test_find_tool_server_not_found(self, mock_mcp_manager, mock_internal_manager) -> None:
        """Test when tool is not found anywhere."""
        mock_internal_manager.find_tool_server.return_value = None

        mock_config = MagicMock()
        mock_config.tools = []
        mock_mcp_manager._configs = {"ext-server": mock_config}

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
        )

        result = proxy.find_tool_server("nonexistent_tool")

        assert result is None

    def test_find_tool_server_no_internal_manager(self, mock_mcp_manager) -> None:
        """Test finding tool without internal manager."""
        mock_config = MagicMock()
        mock_config.tools = [{"name": "ext_tool", "description": "Ext"}]
        mock_mcp_manager._configs = {"ext-server": mock_config}

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=None,
        )

        result = proxy.find_tool_server("ext_tool")

        assert result == "ext-server"

    def test_find_tool_server_empty_configs(self, mock_mcp_manager, mock_internal_manager) -> None:
        """Test finding tool with no external server configs."""
        mock_internal_manager.find_tool_server.return_value = None
        mock_mcp_manager._configs = {}

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
        )

        result = proxy.find_tool_server("any_tool")

        assert result is None


class TestCheckArguments:
    """Tests for _check_arguments method (lines 55-75)."""

    @pytest.fixture
    def proxy_service(self):
        """Create a proxy service for validation testing."""
        mock_mcp_manager = MagicMock()
        mock_mcp_manager.project_id = "test-project"
        return ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=None,
            validate_arguments=True,
        )

    def test_check_arguments_valid(self, proxy_service) -> None:
        """Test valid arguments pass validation."""
        schema = {
            "properties": {"name": {"type": "string"}, "value": {"type": "number"}},
            "required": ["name"],
        }
        errors = proxy_service._check_arguments({"name": "test", "value": 42}, schema)
        assert errors == []

    def test_check_arguments_missing_required(self, proxy_service) -> None:
        """Test missing required parameter is detected."""
        schema = {
            "properties": {"name": {"type": "string"}, "value": {"type": "number"}},
            "required": ["name", "value"],
        }
        errors = proxy_service._check_arguments({"name": "test"}, schema)
        assert len(errors) == 1
        assert "Missing required parameter 'value'" in errors[0]

    def test_check_arguments_unknown_with_similar(self, proxy_service) -> None:
        """Test unknown parameter suggests similar ones."""
        schema = {
            "properties": {"workflow_name": {"type": "string"}},
            "required": [],
        }
        errors = proxy_service._check_arguments({"name": "test"}, schema)
        assert len(errors) == 1
        assert "Did you mean 'workflow_name'" in errors[0]

    def test_check_arguments_unknown_no_similar(self, proxy_service) -> None:
        """Test unknown parameter shows valid parameters when no similar names."""
        schema = {
            "properties": {"alpha": {"type": "string"}, "beta": {"type": "number"}},
            "required": [],
        }
        errors = proxy_service._check_arguments({"xyz": "test"}, schema)
        assert len(errors) == 1
        assert "Valid parameters:" in errors[0]
        assert "alpha" in errors[0]
        assert "beta" in errors[0]


class TestCallToolWithValidation:
    """Tests for call_tool with argument validation (lines 162-168)."""

    @pytest.fixture
    def mock_mcp_manager(self):
        """Create a mock MCP manager."""
        manager = MagicMock()
        manager.project_id = "test-project"
        return manager

    @pytest.fixture
    def mock_internal_manager(self):
        """Create a mock internal registry manager."""
        manager = MagicMock()
        manager.is_internal.return_value = True
        return manager

    @pytest.mark.asyncio
    async def test_call_tool_validation_error_returns_schema(
        self, mock_mcp_manager, mock_internal_manager
    ):
        """Test that validation errors include the schema for self-correction."""
        mock_registry = MagicMock()
        mock_registry.get_schema.return_value = {
            "inputSchema": {
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            }
        }
        mock_internal_manager.get_registry.return_value = mock_registry

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
            validate_arguments=True,  # Enable validation
        )

        result = await proxy.call_tool(
            "gobby-tasks",
            "update_task",
            {"unknown_param": "value"},  # Missing required task_id
        )

        assert result["success"] is False
        assert "Invalid arguments" in result["error"]
        assert "schema" in result
        assert result["hint"] is not None


class TestReadResource:
    """Tests for read_resource method (line 224)."""

    @pytest.mark.asyncio
    async def test_read_resource_delegates_to_manager(self):
        """Test that read_resource delegates to MCP manager."""
        mock_mcp_manager = MagicMock()
        mock_mcp_manager.project_id = "test-project"
        mock_mcp_manager.read_resource = AsyncMock(return_value={"content": "resource data"})

        proxy = ToolProxyService(mcp_manager=mock_mcp_manager)

        result = await proxy.read_resource("ext-server", "file:///path/to/resource")

        assert result == {"content": "resource data"}
        mock_mcp_manager.read_resource.assert_called_once_with(
            "ext-server", "file:///path/to/resource"
        )


class TestGetToolSchema:
    """Tests for get_tool_schema method (lines 229-248)."""

    @pytest.fixture
    def mock_mcp_manager(self):
        """Create a mock MCP manager."""
        manager = MagicMock()
        manager.project_id = "test-project"
        return manager

    @pytest.fixture
    def mock_internal_manager(self):
        """Create a mock internal registry manager."""
        return MagicMock()

    @pytest.mark.asyncio
    async def test_get_tool_schema_internal_success(self, mock_mcp_manager, mock_internal_manager):
        """Test getting schema from internal server."""
        mock_internal_manager.is_internal.return_value = True
        mock_registry = MagicMock()
        mock_registry.get_schema.return_value = {
            "name": "create_task",
            "inputSchema": {"properties": {"title": {"type": "string"}}},
        }
        mock_internal_manager.get_registry.return_value = mock_registry

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
        )

        result = await proxy.get_tool_schema("gobby-tasks", "create_task")

        assert result["success"] is True
        assert result["tool"]["name"] == "create_task"

    @pytest.mark.asyncio
    async def test_get_tool_schema_internal_tool_not_found(
        self, mock_mcp_manager, mock_internal_manager
    ):
        """Test error when tool not found on internal server."""
        mock_internal_manager.is_internal.return_value = True
        mock_registry = MagicMock()
        mock_registry.get_schema.return_value = None  # Tool not found
        mock_internal_manager.get_registry.return_value = mock_registry

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
        )

        result = await proxy.get_tool_schema("gobby-tasks", "nonexistent_tool")

        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_get_tool_schema_internal_server_not_found(
        self, mock_mcp_manager, mock_internal_manager
    ):
        """Test error when internal server not found."""
        mock_internal_manager.is_internal.return_value = True
        mock_internal_manager.get_registry.return_value = None  # Server not found

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
        )

        result = await proxy.get_tool_schema("gobby-nonexistent", "some_tool")

        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_get_tool_schema_external_server_not_found(
        self, mock_mcp_manager, mock_internal_manager
    ):
        """Test error when external server not found."""
        mock_internal_manager.is_internal.return_value = False
        mock_mcp_manager.has_server.return_value = False

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
        )

        result = await proxy.get_tool_schema("nonexistent-server", "some_tool")

        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_get_tool_schema_external_exception(
        self, mock_mcp_manager, mock_internal_manager
    ):
        """Test exception handling for external server schema request."""
        mock_internal_manager.is_internal.return_value = False
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.get_tool_input_schema = AsyncMock(
            side_effect=Exception("Connection timeout")
        )

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
        )

        with pytest.raises(MCPError) as exc_info:
            await proxy.get_tool_schema("ext-server", "failing_tool")

        assert "Failed to get schema" in str(exc_info.value)
        assert "Connection timeout" in str(exc_info.value)


class TestCallToolByName:
    """Tests for call_tool_by_name method (lines 280-309)."""

    @pytest.fixture
    def mock_mcp_manager(self):
        """Create a mock MCP manager."""
        manager = MagicMock()
        manager.project_id = "test-project"
        manager._configs = {}
        return manager

    @pytest.fixture
    def mock_internal_manager(self):
        """Create a mock internal registry manager."""
        manager = MagicMock()
        return manager

    @pytest.mark.asyncio
    async def test_call_tool_by_name_routes_to_internal(
        self, mock_mcp_manager, mock_internal_manager
    ):
        """Test that call_tool_by_name routes to correct internal server."""
        mock_internal_manager.find_tool_server.return_value = "gobby-tasks"
        mock_internal_manager.is_internal.return_value = True

        mock_registry = MagicMock()
        mock_registry.call = AsyncMock(return_value={"id": "task-123"})
        mock_internal_manager.get_registry.return_value = mock_registry

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
            validate_arguments=False,
        )

        result = await proxy.call_tool_by_name("create_task", {"title": "Test"})

        assert result["id"] == "task-123"
        mock_internal_manager.find_tool_server.assert_called_once_with("create_task")

    @pytest.mark.asyncio
    async def test_call_tool_by_name_routes_to_external(
        self, mock_mcp_manager, mock_internal_manager
    ):
        """Test that call_tool_by_name routes to correct external server."""
        mock_internal_manager.find_tool_server.return_value = None
        mock_internal_manager.is_internal.return_value = False

        mock_config = MagicMock()
        mock_config.tools = [{"name": "ext_tool", "description": "External"}]
        mock_mcp_manager._configs = {"ext-server": mock_config}
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.call_tool = AsyncMock(return_value={"result": "success"})

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
            validate_arguments=False,
        )

        result = await proxy.call_tool_by_name("ext_tool", {"arg": "value"})

        assert result["result"] == "success"
        mock_mcp_manager.call_tool.assert_called_once_with(
            "ext-server", "ext_tool", {"arg": "value"}
        )

    @pytest.mark.asyncio
    async def test_call_tool_by_name_not_found(self, mock_mcp_manager, mock_internal_manager):
        """Test error when tool is not found on any server."""
        mock_internal_manager.find_tool_server.return_value = None
        mock_mcp_manager._configs = {}

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
        )

        result = await proxy.call_tool_by_name("nonexistent_tool", {"arg": "value"})

        assert result["success"] is False
        assert "not found" in result["error"]
        assert result["tool_name"] == "nonexistent_tool"

    @pytest.mark.asyncio
    async def test_call_tool_by_name_with_none_arguments(
        self, mock_mcp_manager, mock_internal_manager
    ):
        """Test call_tool_by_name with None arguments."""
        mock_internal_manager.find_tool_server.return_value = "gobby-tasks"
        mock_internal_manager.is_internal.return_value = True

        mock_registry = MagicMock()
        mock_registry.call = AsyncMock(return_value={"status": "ok"})
        mock_internal_manager.get_registry.return_value = mock_registry

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
            validate_arguments=False,
        )

        result = await proxy.call_tool_by_name("list_tasks", None)

        assert result["status"] == "ok"


@pytest.mark.asyncio
class TestProxyNamespaceResolution:
    """Tests for server_name='gobby' auto-resolution."""

    @pytest.fixture
    def mock_mcp_manager(self) -> MagicMock:
        """Create a mock MCP manager."""
        manager = MagicMock()
        manager.project_id = "test-project"
        manager._configs = {}
        manager.has_server.return_value = False
        return manager

    @pytest.fixture
    def mock_internal_manager(self) -> MagicMock:
        """Create a mock internal registry manager."""
        manager = MagicMock()
        manager.is_internal.return_value = False
        return manager

    async def test_list_tools_gobby_aggregates_all_internal(
        self, mock_mcp_manager, mock_internal_manager
    ) -> None:
        """Test that list_tools('gobby') aggregates tools from all internal registries."""
        registry1 = MagicMock()
        registry1.list_tools.return_value = [
            {"name": "create_task", "brief": "Create a task"},
        ]
        registry2 = MagicMock()
        registry2.list_tools.return_value = [
            {"name": "create_memory", "brief": "Create a memory"},
            {"name": "search_memories", "brief": "Search memories"},
        ]
        mock_internal_manager.get_all_registries.return_value = [registry1, registry2]

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
        )

        result = await proxy.list_tools("gobby")

        assert result["success"] is True
        assert result["tool_count"] == 3
        assert len(result["tools"]) == 3
        names = [t["name"] for t in result["tools"]]
        assert "create_task" in names
        assert "create_memory" in names
        assert "search_memories" in names

    async def test_list_tools_gobby_no_internal_manager(self, mock_mcp_manager) -> None:
        """Test list_tools('gobby') with no internal manager returns empty."""
        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=None,
        )

        result = await proxy.list_tools("gobby")

        assert result["success"] is True
        assert result["tool_count"] == 0
        assert result["tools"] == []

    async def test_list_tools_gobby_with_filter(
        self, mock_mcp_manager, mock_internal_manager
    ) -> None:
        """Test list_tools('gobby') applies workflow phase filtering."""
        registry = MagicMock()
        registry.list_tools.return_value = [
            {"name": "tool1", "brief": "Tool 1"},
            {"name": "tool2", "brief": "Tool 2"},
        ]
        mock_internal_manager.get_all_registries.return_value = [registry]

        mock_filter = MagicMock()
        mock_filter.filter_tools.return_value = [{"name": "tool1", "brief": "Tool 1"}]

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
            tool_filter=mock_filter,
        )

        result = await proxy.list_tools("gobby", session_id="session-123")

        assert result["success"] is True
        assert result["tool_count"] == 1
        mock_filter.filter_tools.assert_called_once()

    async def test_get_tool_schema_gobby_resolves_to_real_server(
        self, mock_mcp_manager, mock_internal_manager
    ) -> None:
        """Test get_tool_schema('gobby', 'create_task') auto-resolves to gobby-tasks."""
        mock_internal_manager.find_tool_server.return_value = "gobby-tasks"
        mock_internal_manager.is_internal.side_effect = lambda name: name.startswith("gobby-")
        mock_registry = MagicMock()
        mock_registry.get_schema.return_value = {
            "name": "create_task",
            "inputSchema": {"properties": {"title": {"type": "string"}}},
        }
        mock_internal_manager.get_registry.return_value = mock_registry

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
        )

        result = await proxy.get_tool_schema("gobby", "create_task")

        assert result["success"] is True
        assert result["tool"]["name"] == "create_task"
        mock_internal_manager.find_tool_server.assert_called_once_with("create_task")

    async def test_get_tool_schema_gobby_tool_not_found(
        self, mock_mcp_manager, mock_internal_manager
    ) -> None:
        """Test get_tool_schema('gobby', 'nonexistent') returns helpful error."""
        mock_internal_manager.find_tool_server.return_value = None

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
        )

        result = await proxy.get_tool_schema("gobby", "nonexistent_tool")

        assert result["success"] is False
        assert "not a real server" in result["error"]


class TestCallToolStringArgumentCoercion:
    """Tests for string-to-dict argument coercion in call_tool."""

    @pytest.fixture
    def mock_mcp_manager(self):
        manager = MagicMock()
        manager.project_id = "test-project"
        manager._configs = {}
        return manager

    @pytest.fixture
    def mock_internal_manager(self):
        manager = MagicMock()
        manager.is_internal.return_value = True
        return manager

    @pytest.mark.asyncio
    async def test_string_arguments_coerced_to_dict(self, mock_mcp_manager, mock_internal_manager):
        """Test that JSON string arguments are parsed to dict."""
        mock_registry = MagicMock()
        mock_registry.call = AsyncMock(return_value={"status": "ok"})
        mock_internal_manager.get_registry.return_value = mock_registry

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
            validate_arguments=False,
        )

        result = await proxy.call_tool("gobby-tasks", "create_task", '{"title": "Test task"}')

        assert result["status"] == "ok"
        mock_registry.call.assert_called_once()
        call_args = mock_registry.call.call_args
        # registry.call(tool_name, arguments, context=...)
        assert call_args[0][1] == {"title": "Test task"}

    @pytest.mark.asyncio
    async def test_string_arguments_with_special_chars(
        self, mock_mcp_manager, mock_internal_manager
    ):
        """Test string arguments with newlines and unicode are handled."""
        mock_registry = MagicMock()
        mock_registry.call = AsyncMock(return_value={"status": "ok"})
        mock_internal_manager.get_registry.return_value = mock_registry

        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
            validate_arguments=False,
        )

        json_str = '{"title": "Fix\\nbug", "desc": "Héllo wörld"}'
        result = await proxy.call_tool("gobby-tasks", "create_task", json_str)

        assert result["status"] == "ok"
        call_args = mock_registry.call.call_args
        # registry.call(tool_name, arguments, context=...)
        parsed = call_args[0][1]
        assert parsed["title"] == "Fix\nbug"
        assert "Héllo" in parsed["desc"]

    @pytest.mark.asyncio
    async def test_invalid_string_arguments_returns_error(
        self, mock_mcp_manager, mock_internal_manager
    ):
        """Test that non-JSON string arguments return validation error."""
        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
            validate_arguments=False,
        )

        result = await proxy.call_tool("gobby-tasks", "create_task", "not valid json {")

        assert result["success"] is False
        assert "Invalid arguments" in result["error"]

    @pytest.mark.asyncio
    async def test_json_array_arguments_rejected(self, mock_mcp_manager, mock_internal_manager):
        """Test that JSON array arguments (non-dict) are rejected."""
        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
            validate_arguments=False,
        )

        result = await proxy.call_tool("gobby-tasks", "create_task", "[1, 2, 3]")

        assert result["success"] is False
        assert "expected dict" in result["error"]
        assert "list" in result["error"]

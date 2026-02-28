"""Tests for GobbyDaemonTools handler class in server.py."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.server import GobbyDaemonTools, create_mcp_server

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_mcp_manager():
    """Create a mock MCP client manager."""
    manager = MagicMock()
    manager.project_id = "test-project-id"
    manager.connections = {}
    manager.health = {}
    manager.server_configs = []
    return manager


@pytest.fixture
def mock_internal_manager():
    """Create a mock internal tool manager."""
    return MagicMock()


@pytest.fixture
def mock_llm_service():
    """Create a mock LLM service."""
    return MagicMock()


@pytest.fixture
def tools_handler(mock_mcp_manager, mock_internal_manager):
    """Create a GobbyDaemonTools instance for testing."""
    return GobbyDaemonTools(
        mcp_manager=mock_mcp_manager,
        daemon_port=8787,
        websocket_port=8788,
        start_time=1000.0,
        internal_manager=mock_internal_manager,
    )


class TestGobbyDaemonToolsInit:
    """Tests for GobbyDaemonTools initialization."""

    def test_init_creates_services(self, mock_mcp_manager, mock_internal_manager) -> None:
        """Test that all services are initialized."""
        handler = GobbyDaemonTools(
            mcp_manager=mock_mcp_manager,
            daemon_port=8787,
            websocket_port=8788,
            start_time=1000.0,
            internal_manager=mock_internal_manager,
        )

        assert handler.system_service is not None
        assert handler.tool_proxy is not None
        assert handler.server_mgmt is not None
        assert handler.recommendation is not None

    def test_init_stores_mcp_manager(self, mock_mcp_manager, mock_internal_manager) -> None:
        """Test that MCP manager is stored for project_id access."""
        handler = GobbyDaemonTools(
            mcp_manager=mock_mcp_manager,
            daemon_port=8787,
            websocket_port=8788,
            start_time=1000.0,
            internal_manager=mock_internal_manager,
        )

        assert handler._mcp_manager is mock_mcp_manager

    def test_init_with_semantic_search(self, mock_mcp_manager, mock_internal_manager) -> None:
        """Test initialization with semantic search service."""
        mock_semantic = MagicMock()

        handler = GobbyDaemonTools(
            mcp_manager=mock_mcp_manager,
            daemon_port=8787,
            websocket_port=8788,
            start_time=1000.0,
            internal_manager=mock_internal_manager,
            semantic_search=mock_semantic,
        )

        assert handler._semantic_search is mock_semantic


class TestGobbyDaemonToolsStatus:
    """Tests for status tool."""

    @pytest.mark.asyncio
    async def test_status_returns_daemon_info(self, mock_mcp_manager, mock_internal_manager):
        """Test that status returns daemon status info."""
        # Mock the manager methods used by SystemService
        mock_mcp_manager.get_server_health.return_value = {}
        mock_mcp_manager.get_lazy_connection_states.return_value = {}

        handler = GobbyDaemonTools(
            mcp_manager=mock_mcp_manager,
            daemon_port=8787,
            websocket_port=8788,
            start_time=1000.0,
            internal_manager=mock_internal_manager,
        )

        result = await handler.status()

        assert "running" in result
        assert "http_port" in result
        assert "websocket_port" in result
        assert "mcp_servers" in result

    @pytest.mark.asyncio
    async def test_status_includes_mcp_servers(self, mock_mcp_manager, mock_internal_manager):
        """Test that status includes MCP server connection states."""
        # Mock server health data
        mock_mcp_manager.get_server_health.return_value = {
            "test-server": {"state": "connected", "health": "healthy"}
        }
        mock_mcp_manager.get_lazy_connection_states.return_value = {}
        mock_mcp_manager.connections = {"test-server": MagicMock()}

        handler = GobbyDaemonTools(
            mcp_manager=mock_mcp_manager,
            daemon_port=8787,
            websocket_port=8788,
            start_time=1000.0,
            internal_manager=mock_internal_manager,
        )

        result = await handler.status()

        assert "mcp_servers" in result
        assert "test-server" in result["mcp_servers"]
        assert result["mcp_servers"]["test-server"]["state"] == "connected"


class TestGobbyDaemonToolsListMcpServers:
    """Tests for list_mcp_servers tool."""

    @pytest.mark.asyncio
    async def test_list_mcp_servers_empty(self, mock_mcp_manager, mock_internal_manager):
        """Test listing when no servers configured."""
        mock_internal_manager.get_all_registries.return_value = []
        mock_mcp_manager.server_configs = []
        mock_mcp_manager.connections = {}
        mock_mcp_manager.health = {}

        handler = GobbyDaemonTools(
            mcp_manager=mock_mcp_manager,
            daemon_port=8787,
            websocket_port=8788,
            start_time=1000.0,
            internal_manager=mock_internal_manager,
        )

        result = await handler.list_mcp_servers()

        assert "servers" in result
        assert "total" in result
        assert "connected" in result
        assert result["total"] == 0
        assert result["connected"] == 0

    @pytest.mark.asyncio
    async def test_list_mcp_servers_with_servers(self, mock_mcp_manager, mock_internal_manager):
        """Test listing with configured servers."""
        mock_internal_manager.get_all_registries.return_value = []

        config1 = MagicMock()
        config1.name = "server1"
        config1.transport = "http"
        config1.enabled = True

        config2 = MagicMock()
        config2.name = "server2"
        config2.transport = "stdio"
        config2.enabled = True

        health1 = MagicMock()
        health1.state.value = "connected"
        health2 = MagicMock()
        health2.state.value = "pending"

        mock_mcp_manager.server_configs = [config1, config2]
        mock_mcp_manager.connections = {"server1": MagicMock()}
        mock_mcp_manager.health = {"server1": health1, "server2": health2}

        handler = GobbyDaemonTools(
            mcp_manager=mock_mcp_manager,
            daemon_port=8787,
            websocket_port=8788,
            start_time=1000.0,
            internal_manager=mock_internal_manager,
        )

        result = await handler.list_mcp_servers()

        assert result["total"] == 2
        assert result["connected"] == 1

        # Verify slim fields on each server
        server1 = next(s for s in result["servers"] if s["name"] == "server1")
        assert server1["state"] == "connected"
        assert server1["transport"] == "http"

        server2 = next(s for s in result["servers"] if s["name"] == "server2")
        assert server2["state"] == "pending"
        assert server2["transport"] == "stdio"


class TestGobbyDaemonToolsCallTool:
    """Tests for call_tool functionality."""

    @pytest.mark.asyncio
    async def test_call_tool_delegates_to_proxy(self, tools_handler):
        """Test that call_tool delegates to tool_proxy service."""
        tools_handler.tool_proxy.call_tool = AsyncMock(
            return_value={"success": True, "result": "test output"}
        )

        result = await tools_handler.call_tool(
            server_name="test-server",
            tool_name="test-tool",
            arguments={"key": "value"},
        )

        tools_handler.tool_proxy.call_tool.assert_called_once_with(
            "test-server", "test-tool", {"key": "value"}, None, call_context=None
        )
        # MCP layer strips "success" from successful responses (server.py:140-142)
        assert "success" not in result
        assert result["result"] == "test output"

    @pytest.mark.asyncio
    async def test_call_tool_passes_arguments_correctly(self, tools_handler):
        """Test that arguments are passed correctly to tool."""
        tools_handler.tool_proxy.call_tool = AsyncMock(return_value={"success": True})

        complex_args = {
            "string_arg": "value",
            "number_arg": 42,
            "bool_arg": True,
            "list_arg": [1, 2, 3],
            "nested": {"key": "nested_value"},
        }

        await tools_handler.call_tool(
            server_name="server",
            tool_name="complex-tool",
            arguments=complex_args,
        )

        call_args = tools_handler.tool_proxy.call_tool.call_args
        assert call_args[0][2] == complex_args

    @pytest.mark.asyncio
    async def test_call_tool_with_none_arguments(self, tools_handler):
        """Test call_tool with no arguments."""
        tools_handler.tool_proxy.call_tool = AsyncMock(return_value={"success": True})

        await tools_handler.call_tool(
            server_name="server",
            tool_name="no-args-tool",
            arguments=None,
        )

        tools_handler.tool_proxy.call_tool.assert_called_once_with(
            "server", "no-args-tool", None, None, call_context=None
        )

    @pytest.mark.asyncio
    async def test_call_tool_propagates_errors(self, tools_handler):
        """Test that errors from tool_proxy are propagated."""
        tools_handler.tool_proxy.call_tool = AsyncMock(
            side_effect=ValueError("Tool not found: nonexistent")
        )

        with pytest.raises(ValueError, match="Tool not found"):
            await tools_handler.call_tool(
                server_name="server",
                tool_name="nonexistent",
                arguments={},
            )


class TestGobbyDaemonToolsListTools:
    """Tests for list_tools functionality."""

    @pytest.mark.asyncio
    async def test_list_tools_returns_tools_for_server(self, tools_handler):
        """Test that list_tools returns tools for a specific server."""
        tools_handler.tool_proxy.list_tools = AsyncMock(
            return_value={
                "status": "success",
                "tools": [
                    {"name": "tool1", "brief": "First tool"},
                    {"name": "tool2", "brief": "Second tool"},
                ],
                "tool_count": 2,
            }
        )

        result = await tools_handler.list_tools(server_name="server1")

        assert result["status"] == "success"
        assert "tools" in result
        assert len(result["tools"]) == 2
        assert result["tool_count"] == 2

    @pytest.mark.asyncio
    async def test_list_tools_filters_by_server(self, tools_handler):
        """Test that list_tools can filter by server."""
        tools_handler.tool_proxy.list_tools = AsyncMock(
            return_value={
                "tools": [{"name": "tool1", "server": "server1"}],
                "count": 1,
            }
        )

        await tools_handler.list_tools(server_name="server1")

        tools_handler.tool_proxy.list_tools.assert_called_once_with("server1", session_id=None)

    @pytest.mark.asyncio
    async def test_list_tools_with_session_id(self, tools_handler):
        """Test that list_tools passes session_id for workflow filtering."""
        tools_handler.tool_proxy.list_tools = AsyncMock(
            return_value={"status": "success", "tools": [], "tool_count": 0}
        )

        await tools_handler.list_tools(server_name="server1", session_id="session-123")

        tools_handler.tool_proxy.list_tools.assert_called_once_with(
            "server1", session_id="session-123"
        )


class TestGobbyDaemonToolsGetToolSchema:
    """Tests for get_tool_schema functionality."""

    @pytest.mark.asyncio
    async def test_get_tool_schema_returns_complete_schema(self, tools_handler):
        """Test that get_tool_schema returns complete schema with inputSchema."""
        expected_schema = {
            "name": "test-tool",
            "description": "A test tool",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "param1": {"type": "string"},
                    "param2": {"type": "integer"},
                },
                "required": ["param1"],
            },
        }

        tools_handler.tool_proxy.get_tool_schema = AsyncMock(return_value=expected_schema)

        result = await tools_handler.get_tool_schema("server1", "test-tool")

        assert result["name"] == "test-tool"
        assert result["description"] == "A test tool"
        assert "inputSchema" in result
        assert result["inputSchema"]["type"] == "object"
        assert "param1" in result["inputSchema"]["properties"]

    @pytest.mark.asyncio
    async def test_get_tool_schema_delegates_to_proxy(self, tools_handler):
        """Test that get_tool_schema delegates to tool_proxy."""
        tools_handler.tool_proxy.get_tool_schema = AsyncMock(return_value={"name": "tool"})

        await tools_handler.get_tool_schema("my-server", "my-tool")

        tools_handler.tool_proxy.get_tool_schema.assert_called_once_with("my-server", "my-tool")


class TestGobbyDaemonToolsServerManagement:
    """Tests for server management tools."""

    @pytest.mark.asyncio
    async def test_add_mcp_server_delegates_to_service(self, tools_handler):
        """Test that add_mcp_server delegates to server_mgmt service."""
        tools_handler.server_mgmt.add_server = AsyncMock(
            return_value={"success": True, "name": "new-server"}
        )

        result = await tools_handler.add_mcp_server(
            name="new-server",
            transport="http",
            url="http://localhost:8080",
            enabled=True,
        )

        tools_handler.server_mgmt.add_server.assert_called_once()
        assert result["success"] is True
        assert result["name"] == "new-server"

    @pytest.mark.asyncio
    async def test_remove_mcp_server_delegates_to_service(self, tools_handler):
        """Test that remove_mcp_server delegates to server_mgmt service."""
        tools_handler.server_mgmt.remove_server = AsyncMock(return_value={"success": True})

        result = await tools_handler.remove_mcp_server("old-server")

        tools_handler.server_mgmt.remove_server.assert_called_once_with("old-server")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_import_mcp_server_delegates_to_service(self, tools_handler):
        """Test that import_mcp_server delegates to server_mgmt service."""
        tools_handler.server_mgmt.import_server = AsyncMock(
            return_value={"success": True, "imported": ["server1"]}
        )

        result = await tools_handler.import_mcp_server(from_project="source-project")

        tools_handler.server_mgmt.import_server.assert_called_once()
        assert result["success"] is True


class TestGobbyDaemonToolsRecommendation:
    """Tests for recommendation tools."""

    @pytest.mark.asyncio
    async def test_recommend_tools_delegates_to_service(self, tools_handler):
        """Test that recommend_tools delegates to recommendation service."""
        tools_handler.recommendation.recommend_tools = AsyncMock(
            return_value={
                "success": True,
                "recommendations": [
                    {"server": "server1", "tool": "tool1", "reason": "Best match"},
                ],
            }
        )

        result = await tools_handler.recommend_tools(
            task_description="I need to search for files",
        )

        tools_handler.recommendation.recommend_tools.assert_called_once()
        assert result["success"] is True
        assert "recommendations" in result


class TestGobbyDaemonToolsSemanticSearch:
    """Tests for semantic search tools."""

    @pytest.mark.asyncio
    async def test_search_tools_without_semantic_search(self, tools_handler):
        """Test search_tools returns error when semantic search not configured."""
        tools_handler._semantic_search = None

        result = await tools_handler.search_tools(query="find files")

        assert result["success"] is False
        assert "not configured" in result["error"]

    @pytest.mark.asyncio
    async def test_search_tools_without_project_id(self, tools_handler):
        """Test search_tools returns error when no project_id."""
        tools_handler._semantic_search = MagicMock()
        tools_handler._mcp_manager.project_id = None

        with patch("gobby.mcp_proxy.server.get_project_context", return_value=None):
            result = await tools_handler.search_tools(query="find files")

        assert result["success"] is False
        assert "No project_id" in result["error"]

    @pytest.mark.asyncio
    async def test_search_tools_success(self, tools_handler):
        """Test successful semantic search."""
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {
            "tool_name": "search_files",
            "server": "filesystem",
            "similarity": 0.85,
        }

        mock_semantic = AsyncMock()
        mock_semantic.search_tools = AsyncMock(return_value=[mock_result])

        tools_handler._semantic_search = mock_semantic
        tools_handler._mcp_manager.project_id = "test-project"

        result = await tools_handler.search_tools(
            query="find files",
            top_k=5,
            min_similarity=0.5,
        )

        assert result["success"] is True
        assert result["total_results"] == 1
        assert len(result["results"]) == 1

    @pytest.mark.asyncio
    async def test_search_tools_with_server_filter(self, tools_handler):
        """Test semantic search with server filter."""
        mock_semantic = AsyncMock()
        mock_semantic.search_tools = AsyncMock(return_value=[])

        tools_handler._semantic_search = mock_semantic
        tools_handler._mcp_manager.project_id = "test-project"

        await tools_handler.search_tools(query="find", server_name="specific-server")

        mock_semantic.search_tools.assert_called_once()
        call_kwargs = mock_semantic.search_tools.call_args[1]
        assert call_kwargs["server_filter"] == "specific-server"


class TestCreateMcpServer:
    """Tests for create_mcp_server factory function."""

    def test_create_mcp_server_returns_fastmcp(self, tools_handler) -> None:
        """Test that create_mcp_server returns a FastMCP instance."""
        mcp = create_mcp_server(tools_handler)

        assert mcp is not None
        assert mcp.name == "gobby"

    def test_create_mcp_server_registers_all_tools(self, tools_handler) -> None:
        """Test that all expected tools are registered."""
        mcp = create_mcp_server(tools_handler)

        # Check that tools were added (FastMCP stores them internally)
        # This verifies the function runs without error
        assert mcp is not None


class TestGobbyDaemonToolsReadResource:
    """Tests for read_mcp_resource functionality."""

    @pytest.mark.asyncio
    async def test_read_mcp_resource_delegates_to_proxy(self, tools_handler):
        """Test that read_mcp_resource delegates to tool_proxy."""
        tools_handler.tool_proxy.read_resource = AsyncMock(
            return_value={"content": "resource content"}
        )

        result = await tools_handler.read_mcp_resource(
            server_name="server1",
            resource_uri="file:///path/to/resource",
        )

        tools_handler.tool_proxy.read_resource.assert_called_once_with(
            "server1", "file:///path/to/resource"
        )
        assert result["content"] == "resource content"

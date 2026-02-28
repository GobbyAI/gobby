import sys
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from gobby.config.app import DaemonConfig
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.server import GobbyDaemonTools

pytestmark = pytest.mark.unit


# Define dummy classes for mocking imports effectively
class AssistantMessage:
    def __init__(self, content):
        self.content = content


class UserMessage:
    def __init__(self, content):
        self.content = content


class TextBlock:
    def __init__(self, text):
        self.text = text


class ToolResultBlock:
    def __init__(self, content):
        self.content = content


class ClaudeAgentOptions:
    def __init__(self, **kwargs):
        pass


async def mock_query_gen(*args, **kwargs):
    pass


# Mock claude_agent_sdk locally for tests
module_mock = Mock()
module_mock.AssistantMessage = AssistantMessage
module_mock.UserMessage = UserMessage
module_mock.TextBlock = TextBlock
module_mock.ToolResultBlock = ToolResultBlock
module_mock.ClaudeAgentOptions = ClaudeAgentOptions
module_mock.query = mock_query_gen


@pytest.fixture(autouse=True)
def mock_claude_sdk():
    with patch.dict(sys.modules, {"claude_agent_sdk": module_mock}):
        yield


@pytest.fixture
def mock_mcp_manager():
    manager = MagicMock(spec=MCPClientManager)
    manager.connections = {}
    manager._connections = {}  # Private attr used by ServerManagementService
    manager.project_id = "test-project-id"
    manager.call_tool = AsyncMock()
    manager.read_resource = AsyncMock()

    mock_config = MagicMock()
    mock_config.name = "downstream"
    mock_config.project_id = "p1"
    mock_config.tools = [{"name": "dt1", "brief": "desc"}]
    manager.server_configs = [mock_config]

    manager.mcp_db_manager = MagicMock()
    manager.mcp_db_manager.get_cached_tools.return_value = []

    # Lazy connection attributes (added in Sprint 13)
    manager.lazy_connect = True
    manager.get_lazy_connection_states = MagicMock(return_value={})

    return manager


@pytest.fixture
def mock_llm_service():
    service = MagicMock()
    provider = MagicMock()
    provider.generate_text = AsyncMock(
        return_value='{"recommendations": [{"server": "s1", "tool": "t1", "reason": "r1"}]}'
    )
    service.get_provider.return_value = provider
    service.get_default_provider.return_value = provider
    # get_provider_for_feature returns tuple (provider, model, prompt)
    service.get_provider_for_feature.return_value = (provider, "test-model", "test prompt")
    # Mock generate directly on the service instance since RecommendationService calls it
    service.generate = AsyncMock(
        return_value='{"recommendations": [{"server": "s1", "tool": "t1", "reason": "r1"}]}'
    )
    return service


@pytest.fixture
def daemon_tools(mock_mcp_manager, mock_llm_service):
    internal_manager = MagicMock()
    internal_manager.is_internal.return_value = False

    # Mock config to return mcp_proxy config
    mock_config = MagicMock(spec=DaemonConfig)
    mock_proxy_config = MagicMock()
    mock_proxy_config.enabled = True
    mock_proxy_config.tool_timeout = 60.0
    mock_config.get_mcp_client_proxy_config.return_value = mock_proxy_config
    mock_config.compression = None  # Disable compression for tests

    # Mock recommend tools config
    mock_rec_tools = MagicMock()
    mock_rec_tools.enabled = True
    mock_rec_tools.provider = "claude"
    mock_rec_tools.prompt = "rec prompt"
    mock_rec_tools.llm_prompt_path = None  # Use default prompt path
    mock_config.get_recommend_tools_config.return_value = mock_rec_tools
    mock_config.recommend_tools = mock_rec_tools  # Direct property access

    tools = GobbyDaemonTools(
        mcp_manager=mock_mcp_manager,
        daemon_port=8080,
        websocket_port=60888,
        start_time=1000.0,
        internal_manager=internal_manager,
        config=mock_config,
        llm_service=mock_llm_service,
    )
    return tools


@pytest.mark.asyncio
async def test_status_tool(daemon_tools):
    result = await daemon_tools.status()
    assert result["running"] is True
    assert result["healthy"] is True
    assert result["http_port"] == 8080


@pytest.mark.asyncio
async def test_list_mcp_servers(daemon_tools, mock_mcp_manager):
    # Set up server config and health for direct access
    config = MagicMock()
    config.name = "server1"
    config.transport = "http"
    config.enabled = True
    mock_mcp_manager.server_configs = [config]

    health = MagicMock()
    health.state.value = "connected"
    mock_mcp_manager.health = {"server1": health}
    mock_mcp_manager.connections = {"server1": MagicMock()}

    # Internal manager returns no registries
    daemon_tools.internal_manager.get_all_registries.return_value = []

    result = await daemon_tools.list_mcp_servers()
    assert len(result["servers"]) == 1
    assert result["servers"][0]["name"] == "server1"
    assert result["servers"][0]["state"] == "connected"
    assert result["connected"] == 1


@pytest.mark.asyncio
async def test_call_tool_internal(daemon_tools):
    daemon_tools.internal_manager.is_internal.return_value = True
    mock_registry = MagicMock()
    mock_registry.call = AsyncMock(return_value={"tasks": [], "count": 0})
    daemon_tools.internal_manager.get_registry.return_value = mock_registry

    result = await daemon_tools.call_tool("gobby-tasks", "list_tasks", {})
    assert result == {"tasks": [], "count": 0}


@pytest.mark.asyncio
async def test_call_tool_downstream(daemon_tools, mock_mcp_manager):
    daemon_tools.internal_manager.is_internal.return_value = False
    mock_mcp_manager.call_tool.return_value = {"data": "downstream_result"}

    result = await daemon_tools.call_tool("server1", "tool1", {})
    assert result == {"data": "downstream_result"}


@pytest.mark.asyncio
async def test_read_mcp_resource(daemon_tools, mock_mcp_manager):
    mock_resource = MagicMock()
    mock_content = MagicMock()
    mock_content.model_dump.return_value = {"text": "content"}
    mock_resource.contents = [mock_content]
    mock_mcp_manager.read_resource.return_value = mock_resource

    result = await daemon_tools.read_mcp_resource("server1", "uri1")
    # read_resource returns raw result from mcp_manager
    assert result == mock_resource


@pytest.mark.asyncio
async def test_add_mcp_server(daemon_tools, mock_mcp_manager):
    # The ServerManagementService.add_server creates an MCPServerConfig and adds it
    # We need to mock add_server_config and connect_all on the mcp_manager
    mock_mcp_manager.add_server_config = MagicMock()
    mock_mcp_manager.connect_all = AsyncMock(return_value={"s1": True})

    result = await daemon_tools.add_mcp_server(
        name="s1", transport="http", url="http://localhost:8000"
    )
    assert result["success"] is True
    mock_mcp_manager.add_server_config.assert_called_once()


@pytest.mark.asyncio
async def test_remove_mcp_server(daemon_tools, mock_mcp_manager):
    # ServerManagementService.remove_server calls mcp_manager.remove_server_config
    mock_mcp_manager.remove_server_config = MagicMock()

    result = await daemon_tools.remove_mcp_server(name="s1")
    assert result["success"] is True
    mock_mcp_manager.remove_server_config.assert_called_once_with("s1")


@pytest.mark.asyncio
async def test_list_tools(daemon_tools, mock_mcp_manager):
    # Mock the external tools - mcp_manager.list_tools returns dict mapping server -> tools
    mock_mcp_manager.list_tools = AsyncMock(
        return_value={"downstream": [{"name": "dt1", "description": "downstream tool"}]}
    )
    mock_mcp_manager.has_server.return_value = True

    # list_tools now requires a server parameter
    result = await daemon_tools.list_tools(server_name="downstream")
    assert result["success"] is True
    assert "tools" in result
    assert "tool_count" in result


@pytest.mark.asyncio
async def test_get_tool_schema(daemon_tools, mock_mcp_manager):
    # For internal tools, the internal_manager.get_registry().get_schema() is called
    # For external tools, mcp_manager.get_tool_input_schema() is called

    # Test with external tool - mock the mcp_manager method
    mock_mcp_manager.get_tool_input_schema = AsyncMock(
        return_value={
            "success": True,
            "server": "downstream",
            "tool": {"name": "dt1", "description": "desc", "inputSchema": {"type": "object"}},
        }
    )

    result = await daemon_tools.get_tool_schema("downstream", "dt1")
    assert result["success"] is True
    assert result["tool"]["name"] == "dt1"


@pytest.mark.asyncio
async def test_recommend_tools(daemon_tools, mock_mcp_manager):
    # The RecommendationService.recommend_tools accesses mcp_manager._configs
    mock_mcp_manager._configs = {"server1": MagicMock()}

    with patch.object(daemon_tools.recommendation, "_loader") as mock_loader:
        mock_loader.render.return_value = "Recommend tools for: {task}"
        result = await daemon_tools.recommend_tools("find logic")
    assert result["success"] is True
    # The actual implementation returns a stubbed recommendation
    assert "recommendation" in result


@pytest.mark.skip(reason="GobbyDaemonTools does not have call_hook method - removed in refactor")
@pytest.mark.asyncio
async def test_call_hook(daemon_tools):
    # GobbyDaemonTools no longer exposes call_hook - this functionality
    # is handled by the hook system directly, not through MCP tools
    pass


@pytest.mark.asyncio
async def test_call_tool_returns_mcp_error_on_validation_failure(daemon_tools):
    """Test that call_tool returns CallToolResult(isError=True) when validation fails."""
    from mcp.types import CallToolResult, TextContent

    # Mock tool_proxy.call_tool to return an error dict
    daemon_tools.tool_proxy.call_tool = AsyncMock(
        return_value={
            "success": False,
            "error": "Invalid arguments: ['Missing required parameter foo']",
            "hint": "Review the schema below and retry with correct parameters",
            "schema": {
                "type": "object",
                "required": ["foo"],
                "properties": {"foo": {"type": "string"}},
            },
        }
    )

    result = await daemon_tools.call_tool("gobby-tasks", "create_task", {"wrong": "arg"})

    # Should return CallToolResult with isError=True
    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert len(result.content) == 1
    assert isinstance(result.content[0], TextContent)
    # Error message should include the error, hint, and schema
    assert "Invalid arguments" in result.content[0].text
    assert "Review the schema" in result.content[0].text
    assert '"foo"' in result.content[0].text  # Schema should be included


@pytest.mark.asyncio
async def test_call_tool_returns_mcp_error_without_schema(daemon_tools):
    """Test that call_tool returns CallToolResult(isError=True) even without schema info."""
    from mcp.types import CallToolResult

    # Mock tool_proxy.call_tool to return an error dict without schema
    daemon_tools.tool_proxy.call_tool = AsyncMock(
        return_value={
            "success": False,
            "error": "Server 'unknown' not found",
        }
    )

    result = await daemon_tools.call_tool("unknown", "some_tool", {})

    # Should return CallToolResult with isError=True
    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert "Server 'unknown' not found" in result.content[0].text


@pytest.mark.asyncio
async def test_call_tool_success_returns_raw_result(daemon_tools):
    """Test that successful call_tool returns raw result (not CallToolResult wrapper)."""
    # Mock tool_proxy.call_tool to return a successful result
    daemon_tools.tool_proxy.call_tool = AsyncMock(
        return_value={"tasks": [{"id": "1", "title": "Test"}], "count": 1}
    )

    result = await daemon_tools.call_tool("gobby-tasks", "list_tasks", {})

    # Should return raw dict, not CallToolResult
    assert isinstance(result, dict)
    assert result.get("tasks") is not None
    assert result.get("count") == 1

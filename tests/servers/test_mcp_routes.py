"""
Comprehensive unit tests for MCP routes HTTP handlers.

This module tests the MCP endpoints in src/gobby/servers/routes/mcp.py including:
- list_mcp_tools
- list_mcp_servers
- list_all_mcp_tools
- get_tool_schema
- call_mcp_tool
- add_mcp_server
- import_mcp_server
- remove_mcp_server
- recommend_mcp_tools
- search_mcp_tools
- embed_mcp_tools
- get_mcp_status
- mcp_proxy
- refresh_mcp_tools
- Code execution endpoints
- Hooks endpoints
- Plugins endpoints
- Webhooks endpoints
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from gobby.app_context import ServiceContainer
from gobby.servers.http import HTTPServer
from gobby.storage.database import LocalDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def session_storage(temp_db: LocalDatabase) -> SessionManager:
    """Create session storage."""
    return SessionManager(temp_db)


@pytest.fixture
def project_storage(temp_db: LocalDatabase) -> LocalProjectManager:
    """Create project storage."""
    return LocalProjectManager(temp_db)


@pytest.fixture
def test_project(project_storage: LocalProjectManager, temp_dir: Path) -> dict[str, Any]:
    """Create a test project with project.json file."""
    project = project_storage.create(name="test-project", repo_path=str(temp_dir))

    # Create .gobby/project.json for project resolution
    gobby_dir = temp_dir / ".gobby"
    gobby_dir.mkdir()
    (gobby_dir / "project.json").write_text(f'{{"id": "{project.id}", "name": "test-project"}}')

    return project.to_dict()


@pytest.fixture
def basic_http_server(session_storage: SessionManager) -> HTTPServer:
    """Create a basic HTTP server instance for testing."""
    mock_config = MagicMock()
    mock_config.logging.max_size_mb = 10
    mock_config.logging.backup_count = 3
    mock_config.logging.hook_manager = "/tmp/test_hook.log"
    mock_config.memory.backend = "null"
    mock_config.workflow.timeout = 30
    mock_config.workflow.enabled = True
    mock_config.get_gobby_tasks_config.return_value.enabled = False

    services = ServiceContainer(
        config=mock_config,
        database=session_storage.db,
        session_manager=session_storage,
        task_manager=MagicMock(),
    )
    return HTTPServer(
        services=services,
        port=60887,
        test_mode=True,
    )


@pytest.fixture
def client(basic_http_server: HTTPServer) -> Iterator[TestClient]:
    """Create a test client that runs lifespan to set app.state.server."""
    with patch("gobby.servers.app_factory.HookManager") as MockHM:
        mock_instance = MockHM.return_value
        mock_instance._stop_registry = MagicMock()
        mock_instance.shutdown = MagicMock()
        with TestClient(basic_http_server.app) as c:
            yield c


# ============================================================================
# Fake MCP Manager Classes
# ============================================================================


class FakeServerHealth:
    """Fake server health for testing."""

    def __init__(self, state: str = "connected", health: str = "healthy") -> None:
        self.state = MagicMock(value=state)
        self.health = MagicMock(value=health)
        self.consecutive_failures = 0


class FakeServerConfig:
    """Fake server config for testing."""

    def __init__(
        self,
        name: str = "test-server",
        transport: str = "http",
        enabled: bool = True,
    ) -> None:
        self.name = name
        self.transport = transport
        self.enabled = enabled


class FakeTool:
    """Fake MCP tool for testing."""

    def __init__(
        self,
        name: str = "test-tool",
        description: str = "Test tool description",
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.inputSchema = input_schema or {"type": "object", "properties": {}}


class FakeToolsResult:
    """Fake tools list result for testing."""

    def __init__(self, tools: list[FakeTool] | None = None) -> None:
        self.tools = tools or []


class FakeMCPSession:
    """Fake MCP session for testing."""

    def __init__(self, tools: list[FakeTool] | None = None) -> None:
        self._tools = tools or []

    async def list_tools(self) -> FakeToolsResult:
        """Return fake tools list."""
        return FakeToolsResult(self._tools)


class FakeMCPManager:
    """Fake MCP manager for testing."""

    def __init__(self) -> None:
        self.server_configs: list[FakeServerConfig] = []
        self.connections: dict[str, Any] = {}
        self.health: dict[str, FakeServerHealth] = {}
        self._configs: dict[str, FakeServerConfig] = {}
        self.project_id = "test-project"
        self._sessions: dict[str, FakeMCPSession] = {}

    def has_server(self, server_name: str) -> bool:
        """Check if server is configured."""
        return server_name in self._configs

    async def ensure_connected(self, server_name: str) -> FakeMCPSession:
        """Get or create a session for a server."""
        if server_name not in self._configs:
            raise KeyError(f"Unknown server: {server_name}")
        if server_name not in self._sessions:
            self._sessions[server_name] = FakeMCPSession()
        return self._sessions[server_name]

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Call a tool on a server."""
        if server_name not in self._configs:
            raise ValueError(f"Server not found: {server_name}")
        return {"result": "success", "tool": tool_name, "args": arguments}

    async def get_tool_input_schema(self, server_name: str, tool_name: str) -> dict[str, Any]:
        """Get tool input schema."""
        return {"type": "object", "properties": {}}

    async def add_server(self, config: Any) -> None:
        """Add a server configuration."""
        self._configs[config.name] = config
        self.server_configs.append(config)

    async def remove_server(self, name: str) -> None:
        """Remove a server configuration."""
        if name not in self._configs:
            raise ValueError(f"Server not found: {name}")
        del self._configs[name]
        self.server_configs = [c for c in self.server_configs if c.name != name]


class FakeInternalRegistry:
    """Fake internal tool registry for testing."""

    def __init__(
        self,
        name: str = "gobby-tasks",
        tools: list[dict[str, Any]] | None = None,
    ) -> None:
        self.name = name
        self._tools = tools or [
            {"name": "list_tasks", "description": "List tasks"},
            {"name": "create_task", "description": "Create a task"},
        ]
        self._schemas = {t["name"]: {"type": "object", "properties": {}} for t in self._tools}

    def list_tools(self) -> list[dict[str, Any]]:
        """List available tools."""
        return self._tools

    def get_schema(self, tool_name: str) -> dict[str, Any] | None:
        """Get tool schema."""
        return self._schemas.get(tool_name)

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool."""
        if tool_name not in self._schemas:
            raise ValueError(f"Tool not found: {tool_name}")
        return {"success": True, "tool": tool_name}


class FakeInternalManager:
    """Fake internal registry manager for testing."""

    def __init__(self, registries: list[FakeInternalRegistry] | None = None) -> None:
        self._registries = {r.name: r for r in (registries or [])}

    def is_internal(self, server_name: str) -> bool:
        """Check if server is an internal server."""
        return server_name.startswith("gobby-")

    def get_registry(self, server_name: str) -> FakeInternalRegistry | None:
        """Get registry by name."""
        return self._registries.get(server_name)

    def get_all_registries(self) -> list[FakeInternalRegistry]:
        """Get all registries."""
        return list(self._registries.values())

    def __len__(self) -> int:
        """Return number of registries."""
        return len(self._registries)


# ============================================================================
# list_mcp_tools Endpoint Tests
# ============================================================================


class TestListMCPTools:
    """Tests for GET /mcp/{server_name}/tools endpoint."""

    def test_list_tools_no_mcp_manager(self, client: TestClient) -> None:
        """Test listing tools when MCP manager is not available."""
        response = client.get("/api/mcp/test-server/tools")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "MCP manager not available" in data["error"]

    def test_list_tools_internal_server_success(self, session_storage: SessionManager) -> None:
        """Test listing tools from internal server."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        registry = FakeInternalRegistry(name="gobby-tasks")
        server._internal_manager = FakeInternalManager([registry])

        with TestClient(server.app) as client:
            response = client.get("/api/mcp/gobby-tasks/tools")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["tool_count"] == 2
        assert len(data["tools"]) == 2
        assert "response_time_ms" in data

    def test_list_tools_records_session_discovery_state(
        self, session_storage: SessionManager
    ) -> None:
        """Session header should drive listed_servers tracking for discovery routes."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        registry = FakeInternalRegistry(name="gobby-tasks")
        server._internal_manager = FakeInternalManager([registry])
        server._tools_handler = MagicMock(tool_proxy=MagicMock())

        with TestClient(server.app) as client:
            response = client.get(
                "/api/mcp/gobby-tasks/tools",
                headers={"X-Gobby-Session-Id": "123e4567-e89b-12d3-a456-426614174000"},
            )

        assert response.status_code == 200
        server._tools_handler.tool_proxy.record_listed_server.assert_called_once_with(
            "gobby-tasks",
            session_id="123e4567-e89b-12d3-a456-426614174000",
        )

    def test_list_tools_emits_proxy_after_tool(self, session_storage: SessionManager) -> None:
        """Successful list_tools should emit the synthetic proxy AFTER_TOOL event."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        registry = FakeInternalRegistry(name="gobby-tasks")
        server._internal_manager = FakeInternalManager([registry])
        server._tools_handler = MagicMock(tool_proxy=MagicMock())
        server._tools_handler.tool_proxy.emit_synthetic_proxy_after_tool = AsyncMock()

        with TestClient(server.app) as client:
            response = client.get(
                "/api/mcp/gobby-tasks/tools",
                headers={"X-Gobby-Session-Id": "123e4567-e89b-12d3-a456-426614174000"},
            )

        assert response.status_code == 200
        result = response.json()
        server._tools_handler.tool_proxy.emit_synthetic_proxy_after_tool.assert_awaited_once_with(
            session_id="123e4567-e89b-12d3-a456-426614174000",
            tool_name="list_tools",
            tool_input={"server_name": "gobby-tasks"},
            result=result,
            is_failure=False,
        )

    def test_list_tools_internal_server_fallthrough(self, session_storage: SessionManager) -> None:
        """Test listing tools falls through to MCP manager when internal registry not found."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )

        with TestClient(server.app) as client:
            # No internal manager, should fall through to MCP manager check
            response = client.get("/api/mcp/gobby-nonexistent/tools")

        # Returns 200 with success=False because mcp_manager is None
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "MCP manager not available" in data["error"]

    def test_list_tools_external_server_not_configured(
        self, session_storage: SessionManager
    ) -> None:
        """Test listing tools from non-configured external server."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server.mcp_manager = FakeMCPManager()

        with TestClient(server.app) as client:
            response = client.get("/api/mcp/unknown-server/tools")

        assert response.status_code == 404
        assert "Unknown MCP server" in response.json()["detail"]["error"]

    def test_list_tools_external_server_success(self, session_storage: SessionManager) -> None:
        """Test listing tools from external server."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        config = FakeServerConfig(name="external-server")
        mcp_manager._configs["external-server"] = config
        mcp_manager.server_configs.append(config)
        mcp_manager._sessions["external-server"] = FakeMCPSession(
            [FakeTool(name="external-tool", description="External tool")]
        )
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.get("/api/mcp/external-server/tools")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["tool_count"] == 1
        assert data["tools"][0]["name"] == "external-tool"

    def test_list_tools_external_server_connection_failure(
        self, session_storage: SessionManager
    ) -> None:
        """Test listing tools when external server connection fails."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        config = FakeServerConfig(name="failing-server")
        mcp_manager._configs["failing-server"] = config
        mcp_manager.ensure_connected = AsyncMock(side_effect=RuntimeError("Connection failed"))
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.get("/api/mcp/failing-server/tools")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "connection failed" in data["error"]

    def test_list_tools_external_server_list_tools_failure(
        self, session_storage: SessionManager
    ) -> None:
        """Test handling of list_tools failure from external server."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        config = FakeServerConfig(name="error-server")
        mcp_manager._configs["error-server"] = config

        # Create a session that fails on list_tools
        session = MagicMock()
        session.list_tools = AsyncMock(side_effect=RuntimeError("List tools failed"))
        mcp_manager._sessions["error-server"] = session
        mcp_manager.ensure_connected = AsyncMock(return_value=session)
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.get("/api/mcp/error-server/tools")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Failed to list tools" in data["error"]

    def test_list_tools_with_input_schema_dict(self, session_storage: SessionManager) -> None:
        """Test listing tools with inputSchema as dict."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        config = FakeServerConfig(name="schema-server")
        mcp_manager._configs["schema-server"] = config

        tool = MagicMock()
        tool.name = "schema-tool"
        tool.description = "Tool with schema"
        tool.inputSchema = {"type": "object", "properties": {"arg1": {"type": "string"}}}

        session = MagicMock()
        tools_result = MagicMock()
        tools_result.tools = [tool]
        session.list_tools = AsyncMock(return_value=tools_result)
        mcp_manager._sessions["schema-server"] = session
        mcp_manager.ensure_connected = AsyncMock(return_value=session)
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.get("/api/mcp/schema-server/tools")

        assert response.status_code == 200
        data = response.json()
        assert data["tools"][0]["inputSchema"]["type"] == "object"

    def test_list_tools_with_input_schema_model_dump(self, session_storage: SessionManager) -> None:
        """Test listing tools with inputSchema having model_dump method."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        config = FakeServerConfig(name="model-server")
        mcp_manager._configs["model-server"] = config

        # Create a schema with model_dump method
        mock_schema = MagicMock()
        mock_schema.model_dump.return_value = {"type": "object", "required": ["id"]}

        tool = MagicMock()
        tool.name = "model-tool"
        tool.description = "Tool with model schema"
        tool.inputSchema = mock_schema

        session = MagicMock()
        tools_result = MagicMock()
        tools_result.tools = [tool]
        session.list_tools = AsyncMock(return_value=tools_result)
        mcp_manager.ensure_connected = AsyncMock(return_value=session)
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.get("/api/mcp/model-server/tools")

        assert response.status_code == 200
        data = response.json()
        assert data["tools"][0]["inputSchema"]["type"] == "object"


# ============================================================================
# list_mcp_servers Endpoint Tests
# ============================================================================


class TestListMCPServers:
    """Tests for GET /mcp/servers endpoint."""

    def test_list_servers_empty(self, client: TestClient) -> None:
        """Test listing servers when none configured."""
        response = client.get("/api/mcp/servers")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["connected"] == 0
        assert data["servers"] == []

    def test_list_servers_with_internal_registries(self, session_storage: SessionManager) -> None:
        """Test listing servers includes internal registries."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._internal_manager = FakeInternalManager(
            [
                FakeInternalRegistry(name="gobby-tasks"),
                FakeInternalRegistry(name="gobby-memory"),
            ]
        )

        with TestClient(server.app) as client:
            response = client.get("/api/mcp/servers")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert data["connected"] == 2
        assert all(s["transport"] == "internal" for s in data["servers"])

    def test_list_servers_with_external_servers(self, session_storage: SessionManager) -> None:
        """Test listing servers includes external MCP servers."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        config = FakeServerConfig(name="external-server", transport="http")
        mcp_manager.server_configs.append(config)
        mcp_manager.health["external-server"] = FakeServerHealth()
        mcp_manager.connections["external-server"] = MagicMock()
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.get("/api/mcp/servers")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["connected"] == 1
        assert data["servers"][0]["name"] == "external-server"
        assert data["servers"][0]["transport"] == "http"

    def test_list_servers_with_disconnected_servers(self, session_storage: SessionManager) -> None:
        """Test listing servers shows disconnected servers."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        config = FakeServerConfig(name="disconnected-server", transport="stdio")
        mcp_manager.server_configs.append(config)
        # No connection in connections dict
        mcp_manager.health["disconnected-server"] = FakeServerHealth(state="disconnected")
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.get("/api/mcp/servers")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["connected"] == 0
        assert data["servers"][0]["state"] == "disconnected"

    def test_list_servers_with_unknown_health(self, session_storage: SessionManager) -> None:
        """Test listing servers handles servers with no health info."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        config = FakeServerConfig(name="no-health-server")
        mcp_manager.server_configs.append(config)
        # No health info
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.get("/api/mcp/servers")

        assert response.status_code == 200
        data = response.json()
        assert data["servers"][0]["state"] == "unknown"

    def test_list_servers_error_handling(self, session_storage: SessionManager) -> None:
        """Test listing servers handles errors gracefully."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        # Create a manager that raises on server_configs access
        mcp_manager = MagicMock()
        type(mcp_manager).server_configs = PropertyMock(side_effect=RuntimeError("Config error"))
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.get("/api/mcp/servers")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False


# ============================================================================
# list_all_mcp_tools Endpoint Tests
# ============================================================================


class TestListAllMCPTools:
    """Tests for GET /mcp/tools endpoint."""

    def test_list_all_tools_empty(self, client: TestClient) -> None:
        """Test listing all tools when none available."""
        response = client.get("/api/mcp/tools")
        assert response.status_code == 200
        data = response.json()
        assert "tools" in data
        assert "response_time_ms" in data

    def test_list_all_tools_with_server_filter(self, session_storage: SessionManager) -> None:
        """Test listing tools filtered by server."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._internal_manager = FakeInternalManager(
            [
                FakeInternalRegistry(name="gobby-tasks"),
                FakeInternalRegistry(name="gobby-memory"),
            ]
        )

        with TestClient(server.app) as client:
            response = client.get("/api/mcp/tools?server_filter=gobby-tasks")

        assert response.status_code == 200
        data = response.json()
        assert "gobby-tasks" in data["tools"]
        assert "gobby-memory" not in data["tools"]

    def test_list_all_tools_with_metrics(self, session_storage: SessionManager) -> None:
        """Test listing tools with metrics included."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._internal_manager = FakeInternalManager(
            [
                FakeInternalRegistry(name="gobby-tasks"),
            ]
        )

        # Mock metrics manager
        mock_metrics_manager = MagicMock()
        mock_metrics_manager.get_metrics.return_value = {
            "tools": [
                {
                    "server_name": "gobby-tasks",
                    "tool_name": "list_tasks",
                    "call_count": 10,
                    "success_rate": 0.95,
                    "avg_latency_ms": 50.5,
                }
            ]
        }
        server.metrics_manager = mock_metrics_manager

        with (
            TestClient(server.app) as client,
            patch.object(server, "resolve_project_id", return_value="test-project-id"),
        ):
            response = client.get("/api/mcp/tools?include_metrics=true")

        assert response.status_code == 200
        data = response.json()
        # Find the list_tasks tool
        tasks_tools = data["tools"].get("gobby-tasks", [])
        list_tasks_tool = next((t for t in tasks_tools if t["name"] == "list_tasks"), None)
        if list_tasks_tool:
            assert list_tasks_tool["call_count"] == 10

    def test_list_all_tools_external_server_disabled(self, session_storage: SessionManager) -> None:
        """Test listing tools skips disabled external servers."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        # Add a disabled server
        config = FakeServerConfig(name="disabled-server", enabled=False)
        mcp_manager._configs["disabled-server"] = config
        mcp_manager.server_configs.append(config)
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.get("/api/mcp/tools?server_filter=disabled-server")

        assert response.status_code == 200
        data = response.json()
        # Tools list should be empty for disabled server
        assert data["tools"].get("disabled-server") == []

    def test_list_all_tools_external_server_failure(self, session_storage: SessionManager) -> None:
        """Test listing tools handles external server failure."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        config = FakeServerConfig(name="failing-server", enabled=True)
        mcp_manager._configs["failing-server"] = config
        mcp_manager.server_configs.append(config)
        mcp_manager.ensure_connected = AsyncMock(side_effect=RuntimeError("Connection failed"))
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.get("/api/mcp/tools")

        assert response.status_code == 200
        data = response.json()
        # Should return empty list for failing server
        assert data["tools"].get("failing-server") == []


# ============================================================================
# get_tool_schema Endpoint Tests
# ============================================================================


class TestGetToolSchema:
    """Tests for POST /mcp/tools/schema endpoint."""

    def test_get_schema_missing_fields(self, client: TestClient) -> None:
        """Test getting schema with missing required fields."""
        response = client.post("/api/mcp/tools/schema", json={"server_name": "test"})
        assert response.status_code == 400
        assert "server_name, tool_name" in response.json()["detail"]["error"]

    def test_get_schema_internal_server_success(self, session_storage: SessionManager) -> None:
        """Test getting schema from internal server."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._internal_manager = FakeInternalManager(
            [
                FakeInternalRegistry(name="gobby-tasks"),
            ]
        )

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/tools/schema",
                json={"server_name": "gobby-tasks", "tool_name": "list_tasks"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "list_tasks"
        assert data["server"] == "gobby-tasks"
        assert "inputSchema" in data

    def test_get_schema_emits_after_tool_with_session_header(
        self, session_storage: SessionManager
    ) -> None:
        """Session header should propagate to the synthetic AFTER_TOOL event.

        ``unlocked_tools`` is now owned by the ``track-schema-lookup`` rule
        firing off this synthetic event (no direct mutation), so the test
        asserts the dispatch carries the session id.
        """
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._internal_manager = FakeInternalManager(
            [
                FakeInternalRegistry(name="gobby-tasks"),
            ]
        )
        server._tools_handler = MagicMock(tool_proxy=MagicMock())
        server._tools_handler.tool_proxy.emit_synthetic_proxy_after_tool = AsyncMock()

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/tools/schema",
                headers={"X-Gobby-Session-Id": "123e4567-e89b-12d3-a456-426614174000"},
                json={"server_name": "gobby-tasks", "tool_name": "list_tasks"},
            )

        assert response.status_code == 200
        result = response.json()
        server._tools_handler.tool_proxy.emit_synthetic_proxy_after_tool.assert_awaited_once_with(
            session_id="123e4567-e89b-12d3-a456-426614174000",
            tool_name="get_tool_schema",
            tool_input={"server_name": "gobby-tasks", "tool_name": "list_tasks"},
            result=result,
            is_failure=False,
        )

    def test_get_schema_emits_proxy_after_tool(self, session_storage: SessionManager) -> None:
        """Successful get_tool_schema should emit the synthetic proxy AFTER_TOOL event."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._internal_manager = FakeInternalManager(
            [
                FakeInternalRegistry(name="gobby-tasks"),
            ]
        )
        server._tools_handler = MagicMock(tool_proxy=MagicMock())
        server._tools_handler.tool_proxy.emit_synthetic_proxy_after_tool = AsyncMock()

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/tools/schema",
                headers={"X-Gobby-Session-Id": "123e4567-e89b-12d3-a456-426614174000"},
                json={"server_name": "gobby-tasks", "tool_name": "list_tasks"},
            )

        assert response.status_code == 200
        result = response.json()
        server._tools_handler.tool_proxy.emit_synthetic_proxy_after_tool.assert_awaited_once_with(
            session_id="123e4567-e89b-12d3-a456-426614174000",
            tool_name="get_tool_schema",
            tool_input={"server_name": "gobby-tasks", "tool_name": "list_tasks"},
            result=result,
            is_failure=False,
        )

    def test_get_schema_resolves_numeric_body_session_ref_via_header_session_project(
        self,
        session_storage: SessionManager,
        project_storage: LocalProjectManager,
        temp_dir: Path,
    ) -> None:
        """A body #N session ref should inherit project scope from the header session."""
        project = project_storage.create(name="test-project", repo_path=str(temp_dir))
        session = session_storage.register(
            external_id="external-session-1",
            machine_id="machine-1",
            source="codex",
            project_id=project.id,
        )

        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._internal_manager = FakeInternalManager([FakeInternalRegistry(name="gobby-tasks")])
        server._tools_handler = MagicMock(tool_proxy=MagicMock())
        server._tools_handler.tool_proxy.emit_synthetic_proxy_after_tool = AsyncMock()

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/tools/schema",
                headers={"X-Gobby-Session-Id": session.id},
                json={
                    "server_name": "gobby-tasks",
                    "tool_name": "list_tasks",
                    "session_id": f"#{session.seq_num}",
                },
            )

        assert response.status_code == 200
        server._tools_handler.tool_proxy.emit_synthetic_proxy_after_tool.assert_awaited_once_with(
            session_id=session.id,
            tool_name="get_tool_schema",
            tool_input={"server_name": "gobby-tasks", "tool_name": "list_tasks"},
            result=response.json(),
            is_failure=False,
        )

    def test_get_schema_internal_server_tool_not_found(
        self, session_storage: SessionManager
    ) -> None:
        """Test getting schema for non-existent tool on internal server."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        registry = FakeInternalRegistry(name="gobby-tasks")
        registry._schemas = {}  # Empty schemas
        server._internal_manager = FakeInternalManager([registry])

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/tools/schema",
                json={"server_name": "gobby-tasks", "tool_name": "nonexistent"},
            )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]["error"]

    def test_get_schema_external_server_no_manager(self, client: TestClient) -> None:
        """Test getting schema when MCP manager not available."""
        response = client.post(
            "/api/mcp/tools/schema",
            json={"server_name": "external-server", "tool_name": "tool"},
        )
        assert response.status_code == 503
        assert "MCP manager not available" in response.json()["detail"]["error"]

    def test_get_schema_external_server_success(self, session_storage: SessionManager) -> None:
        """Test getting schema from external server."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        mcp_manager._configs["external-server"] = FakeServerConfig(name="external-server")
        mcp_manager.get_tool_info = AsyncMock(
            return_value={
                "name": "get_item",
                "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
            }
        )
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/tools/schema",
                json={"server_name": "external-server", "tool_name": "get_item"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "get_item"
        assert data["inputSchema"]["type"] == "object"

    def test_get_schema_external_server_failure(self, session_storage: SessionManager) -> None:
        """Test getting schema when external server fails."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        mcp_manager._configs["external-server"] = FakeServerConfig(name="external-server")
        mcp_manager.get_tool_info = AsyncMock(side_effect=ValueError("Tool not found"))
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/tools/schema",
                json={"server_name": "external-server", "tool_name": "missing"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False


# ============================================================================
# call_mcp_tool Endpoint Tests
# ============================================================================


class TestCallMCPTool:
    """Tests for POST /mcp/tools/call endpoint."""

    def test_call_tool_missing_fields(self, client: TestClient) -> None:
        """Test calling tool with missing required fields."""
        response = client.post("/api/mcp/tools/call", json={"tool_name": "test"})
        assert response.status_code == 400
        assert "server_name" in response.json()["detail"]["error"]

    def test_call_tool_invalid_json_returns_400(self, client: TestClient) -> None:
        """Malformed JSON should be a client error."""
        response = client.post(
            "/api/mcp/tools/call",
            content='{"server_name":"gobby-tasks"}{"tool_name":"list_tasks"}',
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 400
        assert response.json()["detail"]["error"].startswith("Invalid JSON:")

    def test_call_tool_tool_proxy_failure_is_flattened(
        self, session_storage: SessionManager
    ) -> None:
        """ToolProxy failures should stay flat at the HTTP boundary."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._tools_handler = MagicMock()
        server._tools_handler.tool_proxy = MagicMock()
        server._tools_handler.tool_proxy.call_tool = AsyncMock(
            return_value={
                "success": False,
                "error": "Tool not found",
                "error_code": "TOOL_NOT_FOUND",
                "server_name": "gobby-tasks",
                "tool_name": "missing_tool",
            }
        )

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/tools/call",
                json={
                    "server_name": "gobby-tasks",
                    "tool_name": "missing_tool",
                    "arguments": {},
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["error"] == "Tool not found"
        assert data["error_code"] == "TOOL_NOT_FOUND"
        assert "result" not in data
        assert "response_time_ms" in data

    def test_call_tool_internal_server_success(self, session_storage: SessionManager) -> None:
        """Test calling tool on internal server."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._internal_manager = FakeInternalManager(
            [
                FakeInternalRegistry(name="gobby-tasks"),
            ]
        )

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/tools/call",
                json={
                    "server_name": "gobby-tasks",
                    "tool_name": "list_tasks",
                    "arguments": {"status": "open"},
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["result"] == {"tool": "list_tasks"}
        assert "response_time_ms" in data

    def test_call_tool_internal_server_failure(self, session_storage: SessionManager) -> None:
        """Test calling tool on internal server with error."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        # Include failing_tool in the registry so it gets past schema check
        registry = FakeInternalRegistry(
            name="gobby-tasks",
            tools=[{"name": "failing_tool", "description": "A tool that fails"}],
        )
        registry.call = AsyncMock(side_effect=ValueError("Tool execution failed"))
        server._internal_manager = FakeInternalManager([registry])

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/tools/call",
                json={
                    "server_name": "gobby-tasks",
                    "tool_name": "failing_tool",
                    "arguments": {},
                },
            )

        assert response.status_code == 500

    def test_call_tool_external_server_no_manager(self, client: TestClient) -> None:
        """Test calling tool when MCP manager not available."""
        response = client.post(
            "/api/mcp/tools/call",
            json={
                "server_name": "external-server",
                "tool_name": "tool",
                "arguments": {},
            },
        )
        assert response.status_code == 503

    def test_call_tool_external_server_success(self, session_storage: SessionManager) -> None:
        """Test calling tool on external server."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        mcp_manager._configs["external-server"] = FakeServerConfig(name="external-server")
        mcp_manager.call_tool = AsyncMock(return_value={"data": [1, 2, 3]})
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/tools/call",
                json={
                    "server_name": "external-server",
                    "tool_name": "list_items",
                    "arguments": {"limit": 10},
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["result"] == {"data": [1, 2, 3]}

    def test_call_tool_external_server_failure(self, session_storage: SessionManager) -> None:
        """Test calling tool on external server with error."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        mcp_manager._configs["external-server"] = FakeServerConfig(name="external-server")
        mcp_manager.call_tool = AsyncMock(side_effect=RuntimeError("Tool execution error"))
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/tools/call",
                json={
                    "server_name": "external-server",
                    "tool_name": "failing_tool",
                    "arguments": {},
                },
            )

        assert response.status_code == 500


# ============================================================================
# add_mcp_server Endpoint Tests
# ============================================================================


class TestAddMCPServer:
    """Tests for POST /mcp/servers endpoint."""

    def test_add_server_missing_fields(self, client: TestClient) -> None:
        """Test adding server with missing required fields."""
        response = client.post("/api/mcp/servers", json={"name": "test-server"})
        assert response.status_code == 400
        assert "transport" in response.json()["detail"]["error"]

    def test_add_server_no_project_context(self, session_storage: SessionManager) -> None:
        """Test adding server without project context."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server.mcp_manager = FakeMCPManager()

        with (
            TestClient(server.app) as client,
            patch("gobby.utils.project_context.get_project_context", return_value=None),
        ):
            response = client.post(
                "/api/mcp/servers",
                json={
                    "name": "new-server",
                    "transport": "http",
                    "url": "http://example.com",
                },
            )

        assert response.status_code == 400
        assert "No current project" in response.json()["detail"]["error"]

    def test_add_server_no_mcp_manager(self, session_storage: SessionManager) -> None:
        """Test adding server when MCP manager not available."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": "test-project", "name": "test"},
            ),
        ):
            response = client.post(
                "/api/mcp/servers",
                json={
                    "name": "new-server",
                    "transport": "http",
                    "url": "http://example.com",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "MCP manager not available" in data["error"]

    def test_add_server_success(self, session_storage: SessionManager) -> None:
        """Test adding server successfully."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        mcp_manager.add_server = AsyncMock()
        server.mcp_manager = mcp_manager

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": "test-project", "name": "test"},
            ),
        ):
            response = client.post(
                "/api/mcp/servers",
                json={
                    "name": "new-server",
                    "transport": "http",
                    "url": "http://example.com",
                    "enabled": True,
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "new-server" in data["message"]

    def test_add_server_with_all_options(self, session_storage: SessionManager) -> None:
        """Test adding server with all configuration options."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        mcp_manager.add_server = AsyncMock()
        server.mcp_manager = mcp_manager

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": "test-project", "name": "test"},
            ),
        ):
            response = client.post(
                "/api/mcp/servers",
                json={
                    "name": "full-server",
                    "transport": "stdio",
                    "command": "/usr/bin/python",
                    "args": ["-m", "mcp_server"],
                    "env": {"API_KEY": "secret"},
                    "headers": {"Authorization": "Bearer token"},
                    "enabled": True,
                },
            )

        assert response.status_code == 200
        mcp_manager.add_server.assert_called_once()

    def test_add_server_validation_error(self, session_storage: SessionManager) -> None:
        """Test adding server with validation error."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        mcp_manager.add_server = AsyncMock(side_effect=ValueError("Invalid config"))
        server.mcp_manager = mcp_manager

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": "test-project", "name": "test"},
            ),
        ):
            response = client.post(
                "/api/mcp/servers",
                json={
                    "name": "invalid-server",
                    "transport": "invalid",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False


# ============================================================================
# remove_mcp_server Endpoint Tests
# ============================================================================


class TestRemoveMCPServer:
    """Tests for DELETE /mcp/servers/{name} endpoint."""

    def test_remove_server_no_manager(self, client: TestClient) -> None:
        """Test removing server when MCP manager not available."""
        response = client.delete("/api/mcp/servers/test-server")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "MCP manager not available" in data["error"]

    def test_remove_server_success(self, session_storage: SessionManager) -> None:
        """Test removing server successfully."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        mcp_manager._configs["test-server"] = FakeServerConfig(name="test-server")
        mcp_manager.remove_server = AsyncMock()
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.delete("/api/mcp/servers/test-server")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_remove_server_not_found(self, session_storage: SessionManager) -> None:
        """Test removing non-existent server."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        mcp_manager.remove_server = AsyncMock(side_effect=ValueError("Server not found"))
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.delete("/api/mcp/servers/nonexistent")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Server not found" in data["error"]


# ============================================================================
# import_mcp_server Endpoint Tests
# ============================================================================


class TestImportMCPServer:
    """Tests for POST /mcp/servers/import endpoint."""

    def test_import_server_missing_source(self, client: TestClient) -> None:
        """Test importing server without specifying source."""
        response = client.post("/api/mcp/servers/import", json={})
        assert response.status_code == 400
        assert "at least one" in response.json()["detail"]["error"]

    def test_import_server_no_project_context(self, session_storage: SessionManager) -> None:
        """Test importing server without project context."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )

        with (
            TestClient(server.app) as client,
            patch("gobby.utils.project_context.get_project_context", return_value=None),
        ):
            response = client.post(
                "/api/mcp/servers/import",
                json={"from_project": "other-project"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "No current project" in data["error"]

    # Note: Server import tests with complex config are tested via integration tests
    # as they require proper lifespan initialization with config


# ============================================================================
# recommend_mcp_tools Endpoint Tests
# ============================================================================


class TestRecommendMCPTools:
    """Tests for POST /mcp/tools/recommend endpoint."""

    def test_recommend_tools_missing_task(self, client: TestClient) -> None:
        """Test recommending tools without task description."""
        response = client.post("/api/mcp/tools/recommend", json={})
        assert response.status_code == 400
        assert "task_description" in response.json()["detail"]["error"]

    def test_recommend_tools_no_handler(self, session_storage: SessionManager) -> None:
        """Test recommending tools when handler not available."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/tools/recommend",
                json={"task_description": "Query database"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "not initialized" in data["error"]

    def test_recommend_tools_with_handler(self, session_storage: SessionManager) -> None:
        """Test recommending tools with tools handler."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_handler = MagicMock()
        mock_handler.recommend_tools = AsyncMock(
            return_value={
                "success": True,
                "recommendations": [{"tool": "list_tables", "server": "supabase", "score": 0.9}],
            }
        )
        server._tools_handler = mock_handler

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/tools/recommend",
                json={
                    "task_description": "Query database tables",
                    "search_mode": "llm",
                    "top_k": 5,
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["recommendations"]) == 1

    def test_recommend_tools_semantic_mode_project_resolution_failure(
        self, session_storage: SessionManager
    ) -> None:
        """Test recommending tools with semantic mode when project resolution fails."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )

        with (
            TestClient(server.app) as client,
            patch.object(
                server,
                "resolve_project_id",
                side_effect=ValueError("No project found"),
            ),
        ):
            response = client.post(
                "/api/mcp/tools/recommend",
                json={
                    "task_description": "Query database",
                    "search_mode": "semantic",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "No project found" in data["error"]


# ============================================================================
# search_mcp_tools Endpoint Tests
# ============================================================================


class TestSearchMCPTools:
    """Tests for POST /mcp/tools/search endpoint."""

    def test_search_tools_missing_query(self, client: TestClient) -> None:
        """Test searching tools without query."""
        response = client.post("/api/mcp/tools/search", json={})
        assert response.status_code == 400
        assert "query" in response.json()["detail"]["error"]

    def test_search_tools_project_resolution_failure(self, session_storage: SessionManager) -> None:
        """Test searching tools when project resolution fails."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )

        with (
            TestClient(server.app) as client,
            patch.object(
                server,
                "resolve_project_id",
                side_effect=ValueError("No project"),
            ),
        ):
            response = client.post(
                "/api/mcp/tools/search",
                json={"query": "create file"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "No project" in data["error"]

    def test_search_tools_no_semantic_search(self, session_storage: SessionManager) -> None:
        """Test searching tools when semantic search not configured."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )

        with (
            TestClient(server.app) as client,
            patch.object(server, "resolve_project_id", return_value="test-project"),
        ):
            response = client.post(
                "/api/mcp/tools/search",
                json={"query": "create file"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "not configured" in data["error"]

    def test_search_tools_success(self, session_storage: SessionManager) -> None:
        """Test searching tools successfully."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )

        # Mock semantic search
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {
            "server_name": "filesystem",
            "tool_name": "create_file",
            "similarity": 0.85,
        }

        mock_semantic_search = MagicMock()
        mock_semantic_search.has_embeddings = AsyncMock(return_value=True)
        mock_semantic_search.search_tools = AsyncMock(return_value=[mock_result])

        mock_handler = MagicMock()
        mock_handler._semantic_search = mock_semantic_search
        server._tools_handler = mock_handler

        with (
            TestClient(server.app) as client,
            patch.object(server, "resolve_project_id", return_value="test-project"),
        ):
            response = client.post(
                "/api/mcp/tools/search",
                json={
                    "query": "create file",
                    "top_k": 5,
                    "min_similarity": 0.5,
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["total_results"] == 1


# ============================================================================
# embed_mcp_tools Endpoint Tests
# ============================================================================


class TestEmbedMCPTools:
    """Tests for POST /mcp/tools/embed endpoint."""

    def test_embed_tools_project_resolution_failure(self, session_storage: SessionManager) -> None:
        """Test embedding tools when project resolution fails."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )

        with (
            TestClient(server.app) as client,
            patch.object(
                server,
                "resolve_project_id",
                side_effect=ValueError("No project"),
            ),
        ):
            response = client.post(
                "/api/mcp/tools/embed",
                json={},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False

    def test_embed_tools_no_semantic_search(self, session_storage: SessionManager) -> None:
        """Test embedding tools when semantic search not configured."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )

        with (
            TestClient(server.app) as client,
            patch.object(server, "resolve_project_id", return_value="test-project"),
        ):
            response = client.post(
                "/api/mcp/tools/embed",
                json={"force": True},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "not configured" in data["error"]

    def test_embed_tools_success(self, session_storage: SessionManager) -> None:
        """Test embedding tools successfully."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )

        mock_semantic_search = MagicMock()
        mock_semantic_search.embed_all_tools = AsyncMock(
            return_value={"tools_embedded": 10, "time_ms": 500}
        )

        mock_handler = MagicMock()
        mock_handler._semantic_search = mock_semantic_search
        server._tools_handler = mock_handler

        with (
            TestClient(server.app) as client,
            patch.object(server, "resolve_project_id", return_value="test-project"),
        ):
            response = client.post(
                "/api/mcp/tools/embed",
                json={"force": False},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["stats"]["tools_embedded"] == 10


# ============================================================================
# get_mcp_status Endpoint Tests
# ============================================================================


class TestGetMCPStatus:
    """Tests for GET /mcp/status endpoint."""

    def test_get_status_empty(self, client: TestClient) -> None:
        """Test getting status with no servers."""
        response = client.get("/api/mcp/status")
        assert response.status_code == 200
        data = response.json()
        assert data["total_servers"] == 0
        assert data["connected_servers"] == 0

    def test_get_status_with_internal_servers(self, session_storage: SessionManager) -> None:
        """Test getting status includes internal servers."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._internal_manager = FakeInternalManager(
            [
                FakeInternalRegistry(name="gobby-tasks"),
            ]
        )

        with TestClient(server.app) as client:
            response = client.get("/api/mcp/status")

        assert response.status_code == 200
        data = response.json()
        assert data["total_servers"] == 1
        assert data["connected_servers"] == 1
        assert data["cached_tools"] == 2  # 2 tools in registry

    def test_get_status_with_external_servers(self, session_storage: SessionManager) -> None:
        """Test getting status includes external servers."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        config = FakeServerConfig(name="external-server")
        mcp_manager.server_configs.append(config)
        mcp_manager.health["external-server"] = FakeServerHealth()
        mcp_manager.connections["external-server"] = MagicMock()
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.get("/api/mcp/status")

        assert response.status_code == 200
        data = response.json()
        assert data["total_servers"] == 1
        assert data["connected_servers"] == 1
        assert "external-server" in data["server_health"]


# ============================================================================
# mcp_proxy Endpoint Tests
# ============================================================================


class TestMCPProxy:
    """Tests for POST /mcp/{server_name}/tools/{tool_name} endpoint."""

    def test_proxy_invalid_json(self, client: TestClient) -> None:
        """Test proxy with invalid JSON body."""
        response = client.post(
            "/api/mcp/test-server/tools/test-tool",
            content="not valid json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert "Invalid JSON" in response.json()["detail"]["error"]

    def test_proxy_internal_server_success(self, session_storage: SessionManager) -> None:
        """Test proxy to internal server."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._internal_manager = FakeInternalManager(
            [
                FakeInternalRegistry(name="gobby-tasks"),
            ]
        )

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/gobby-tasks/tools/list_tasks",
                json={"status": "open"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["result"] == {"tool": "list_tasks"}

    def test_proxy_internal_server_fallthrough(self, session_storage: SessionManager) -> None:
        """Test proxy falls through to MCP manager when no internal manager."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )

        with TestClient(server.app) as client:
            # No internal manager, should fall through to MCP manager check
            response = client.post(
                "/api/mcp/gobby-nonexistent/tools/test",
                json={},
            )

        # Returns 503 because mcp_manager is None
        assert response.status_code == 503

    def test_proxy_internal_server_tool_error(self, session_storage: SessionManager) -> None:
        """Test proxy to internal server with tool error."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        # Include failing_tool in the registry so it gets past schema check
        registry = FakeInternalRegistry(
            name="gobby-tasks",
            tools=[{"name": "failing_tool", "description": "A tool that fails"}],
        )
        registry.call = AsyncMock(side_effect=RuntimeError("Tool failed"))
        server._internal_manager = FakeInternalManager([registry])

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/gobby-tasks/tools/failing_tool",
                json={},
            )

        assert response.status_code == 500

    def test_proxy_no_mcp_manager(self, client: TestClient) -> None:
        """Test proxy when MCP manager not available."""
        response = client.post(
            "/api/mcp/external-server/tools/test-tool",
            json={},
        )
        assert response.status_code == 503

    def test_proxy_external_server_success(self, session_storage: SessionManager) -> None:
        """Test proxy to external server."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        mcp_manager._configs["external-server"] = FakeServerConfig(name="external-server")
        mcp_manager.call_tool = AsyncMock(return_value={"items": [1, 2, 3]})
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/external-server/tools/list_items",
                json={"limit": 10},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["result"] == {"items": [1, 2, 3]}

    def test_proxy_external_server_tool_not_found(self, session_storage: SessionManager) -> None:
        """Test proxy when tool not found on external server."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        mcp_manager._configs["external-server"] = FakeServerConfig(name="external-server")
        mcp_manager.call_tool = AsyncMock(side_effect=ValueError("Tool not found"))
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/external-server/tools/missing_tool",
                json={},
            )

        assert response.status_code == 404

    def test_proxy_external_server_error(self, session_storage: SessionManager) -> None:
        """Test proxy when external server returns error."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        mcp_manager._configs["external-server"] = FakeServerConfig(name="external-server")
        mcp_manager.call_tool = AsyncMock(side_effect=RuntimeError("Server error"))
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/external-server/tools/failing_tool",
                json={},
            )

        assert response.status_code == 500


# ============================================================================
# refresh_mcp_tools Endpoint Tests
# ============================================================================


class TestRefreshMCPTools:
    """Tests for POST /mcp/refresh endpoint."""

    def test_refresh_tools_project_resolution_failure(
        self, session_storage: SessionManager
    ) -> None:
        """Test refreshing tools when project resolution fails."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )

        with (
            TestClient(server.app) as client,
            patch.object(
                server,
                "resolve_project_id",
                side_effect=ValueError("No project"),
            ),
        ):
            response = client.post(
                "/api/mcp/refresh",
                json={},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False

    def test_refresh_tools_no_mcp_db_manager(self, session_storage: SessionManager) -> None:
        """Test refreshing tools when MCP DB manager not configured."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )

        with (
            TestClient(server.app) as client,
            patch.object(server, "resolve_project_id", return_value="test-project"),
        ):
            response = client.post(
                "/api/mcp/refresh",
                json={},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "not configured" in data["error"]

    def test_refresh_tools_with_internal_servers(self, session_storage: SessionManager) -> None:
        """Test refreshing tools with internal servers."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._internal_manager = FakeInternalManager(
            [
                FakeInternalRegistry(name="gobby-tasks"),
            ]
        )

        # Mock MCP DB manager
        mock_db = MagicMock()
        mock_mcp_db_manager = MagicMock()
        mock_mcp_db_manager.db = mock_db
        server._mcp_db_manager = mock_mcp_db_manager

        # Mock schema hash manager
        mock_schema_hash_manager = MagicMock()
        mock_schema_hash_manager.check_tools_for_changes.return_value = {
            "new": ["list_tasks"],
            "changed": [],
            "unchanged": ["create_task"],
        }
        mock_schema_hash_manager.cleanup_stale_hashes.return_value = 0

        with (
            TestClient(server.app) as client,
            patch.object(server, "resolve_project_id", return_value="test-project"),
            patch(
                "gobby.mcp_proxy.schema_hash.SchemaHashManager",
                return_value=mock_schema_hash_manager,
            ),
            patch("gobby.mcp_proxy.schema_hash.compute_schema_hash", return_value="abc123"),
        ):
            response = client.post(
                "/api/mcp/refresh",
                json={"force": False},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["stats"]["servers_processed"] == 1

    def test_refresh_tools_force_mode(self, session_storage: SessionManager) -> None:
        """Test refreshing tools with force mode."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._internal_manager = FakeInternalManager(
            [
                FakeInternalRegistry(name="gobby-tasks"),
            ]
        )

        mock_db = MagicMock()
        mock_mcp_db_manager = MagicMock()
        mock_mcp_db_manager.db = mock_db
        server._mcp_db_manager = mock_mcp_db_manager

        mock_schema_hash_manager = MagicMock()
        mock_schema_hash_manager.cleanup_stale_hashes.return_value = 0

        with (
            TestClient(server.app) as client,
            patch.object(server, "resolve_project_id", return_value="test-project"),
            patch(
                "gobby.mcp_proxy.schema_hash.SchemaHashManager",
                return_value=mock_schema_hash_manager,
            ),
            patch("gobby.mcp_proxy.schema_hash.compute_schema_hash", return_value="abc123"),
        ):
            response = client.post(
                "/api/mcp/refresh",
                json={"force": True},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["force"] is True
        # In force mode, all tools are treated as new
        assert data["stats"]["tools_new"] == 2


# ============================================================================
# Code Execution Endpoint Tests
# ============================================================================


class TestCodeExecutionEndpoints:
    """Tests for /code/execute and /code/process-dataset endpoints."""

    @pytest.fixture
    def code_server(self, session_storage: SessionManager) -> HTTPServer:
        """Create server for code endpoint tests."""
        return create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )

    @pytest.fixture
    def code_client(self, code_server: HTTPServer) -> Iterator[TestClient]:
        """Create test client for code endpoints."""
        with TestClient(code_server.app) as c:
            yield c

    def test_execute_code_missing_code(self, code_client: TestClient) -> None:
        """Test execute_code endpoint was removed."""
        response = code_client.post(
            "/code/execute",
            json={"language": "python"},
        )
        # Code execution endpoints have been removed
        assert response.status_code == 404

    # Note: test_execute_code_success is tested via integration tests as it requires
    # full CodeExecutionService setup that interacts with lifespan

    def test_process_dataset_missing_data(self, code_client: TestClient) -> None:
        """Test process_dataset endpoint was removed."""
        response = code_client.post(
            "/code/process-dataset",
            json={"operation": "summarize"},
        )
        # Code execution endpoints have been removed
        assert response.status_code == 404

    def test_process_dataset_missing_operation(self, code_client: TestClient) -> None:
        """Test process_dataset endpoint was removed."""
        response = code_client.post(
            "/code/process-dataset",
            json={"data": [1, 2, 3]},
        )
        # Code execution endpoints have been removed
        assert response.status_code == 404

    # Note: test_process_dataset_success is tested via integration tests as it requires
    # full CodeExecutionService setup that interacts with lifespan


# ============================================================================
# Hooks Endpoint Tests
# ============================================================================


class TestHooksEndpoints:
    """Tests for /hooks/execute endpoint."""

    def test_execute_hook_missing_hook_type(self, client: TestClient) -> None:
        """Test execute hook with missing hook_type."""
        response = client.post(
            "/api/hooks/execute",
            json={"source": "claude"},
        )
        assert response.status_code == 400
        assert "hook_type" in response.json()["detail"]

    def test_execute_hook_client_disconnect_returns_graceful_response(
        self, client: TestClient
    ) -> None:
        """Client disconnects during body parsing should not log endpoint errors."""
        with (
            patch(
                "starlette.requests.Request.json",
                new=AsyncMock(side_effect=ClientDisconnect()),
            ),
            patch("gobby.servers.routes.mcp.hooks.logger") as mock_logger,
        ):
            response = client.post(
                "/api/hooks/execute",
                content=b'{"hook_type":"session-start","source":"claude"}',
                headers={"Content-Type": "application/json"},
            )

        assert response.status_code == 200
        assert response.json() == {"continue": True, "decision": "approve"}
        assert mock_logger.error.called is False
        assert mock_logger.debug.called is True

    def test_execute_hook_missing_source(self, client: TestClient) -> None:
        """Test execute hook with missing source."""
        response = client.post(
            "/api/hooks/execute",
            json={"hook_type": "session-start"},
        )
        assert response.status_code == 400
        assert "source" in response.json()["detail"]

    def test_execute_hook_unsupported_source(self, session_storage: SessionManager) -> None:
        """Test execute hook with unsupported source."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server.app.state.hook_manager = MagicMock()

        with TestClient(server.app) as client:
            response = client.post(
                "/api/hooks/execute",
                json={
                    "hook_type": "session-start",
                    "source": "unsupported",
                },
            )

        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "Unsupported source" in detail
        assert "droid" in detail

    def test_execute_hook_no_hook_manager(self, session_storage: SessionManager) -> None:
        """Test execute hook when hook manager not initialized."""
        # Create server without HookManager patch so hook_manager is not set
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
            config=None,  # No config means HookManager won't be initialized
        )

        with TestClient(server.app) as client:
            response = client.post(
                "/api/hooks/execute",
                json={"hook_type": "session-start", "source": "claude"},
            )
        assert response.status_code == 503
        assert "HookManager not initialized" in response.json()["detail"]

    def test_execute_hook_claude_source(self, session_storage: SessionManager) -> None:
        """Test execute hook with Claude source."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = MagicMock()
        server.app.state.hook_manager = mock_hook_manager

        with (
            TestClient(server.app) as client,
            patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as MockAdapter,
        ):
            mock_adapter = MagicMock()
            mock_adapter.handle_native.return_value = {"continue": True}
            MockAdapter.return_value = mock_adapter

            response = client.post(
                "/api/hooks/execute",
                json={
                    "hook_type": "session-start",
                    "source": "claude",
                    "input_data": {},
                },
            )

        assert response.status_code == 200
        assert response.json()["continue"] is True

    def test_execute_hook_claude_envelope_source(self, session_storage: SessionManager) -> None:
        """Envelope-shaped Claude requests should normalize to the flat adapter payload."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = MagicMock()
        server.app.state.hook_manager = mock_hook_manager

        with (
            TestClient(server.app) as client,
            patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as MockAdapter,
        ):
            mock_adapter = MagicMock()
            mock_adapter.handle_native.return_value = {"continue": True}
            MockAdapter.return_value = mock_adapter

            response = client.post(
                "/api/hooks/execute",
                json={
                    "schema_version": 1,
                    "enqueued_at": "2026-04-16T12:00:00Z",
                    "critical": False,
                    "hook_type": "session-start",
                    "source": "claude",
                    "input_data": {"session_id": "claude-envelope"},
                    "headers": {"X-Gobby-Session-Id": "embedded-session"},
                },
            )

        assert response.status_code == 200
        assert response.json()["continue"] is True
        assert mock_adapter.handle_native.call_args.args[0] == {
            "hook_type": "session-start",
            "source": "claude",
            "input_data": {"session_id": "claude-envelope"},
        }

    def test_execute_hook_gemini_source(self, session_storage: SessionManager) -> None:
        """Test execute hook with Gemini source."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = MagicMock()
        server.app.state.hook_manager = mock_hook_manager

        with (
            TestClient(server.app) as client,
            patch("gobby.adapters.gemini.GeminiAdapter") as MockAdapter,
        ):
            mock_adapter = MagicMock()
            mock_adapter.handle_native.return_value = {"continue": True}
            MockAdapter.return_value = mock_adapter

            response = client.post(
                "/api/hooks/execute",
                json={
                    "hook_type": "session-start",
                    "source": "gemini",
                },
            )

        assert response.status_code == 200

    def test_execute_hook_droid_source(self, session_storage: SessionManager) -> None:
        """Test execute hook with Droid source."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = MagicMock()
        server.app.state.hook_manager = mock_hook_manager

        with (
            TestClient(server.app) as client,
            patch("gobby.adapters.droid.DroidAdapter") as MockAdapter,
        ):
            mock_adapter = MagicMock()
            mock_adapter.handle_native.return_value = {"continue": True}
            MockAdapter.return_value = mock_adapter

            response = client.post(
                "/api/hooks/execute",
                json={
                    "hook_type": "PreToolUse",
                    "source": "droid",
                    "input_data": {"session_id": "droid-123", "cwd": "/tmp"},
                },
            )

        assert response.status_code == 200
        assert response.json()["continue"] is True
        MockAdapter.assert_called_once_with(hook_manager=mock_hook_manager)
        assert mock_adapter.handle_native.call_args.args[0] == {
            "hook_type": "PreToolUse",
            "source": "droid",
            "input_data": {"session_id": "droid-123", "cwd": "/tmp"},
        }

    def test_execute_hook_droid_adapter_error_is_graceful(
        self,
        session_storage: SessionManager,
    ) -> None:
        """Droid adapter failures should return Droid-shaped non-fatal output."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = MagicMock()
        mock_hook_manager.handle.side_effect = RuntimeError("droid adapter failed")
        server.app.state.hook_manager = mock_hook_manager

        with TestClient(server.app) as client:
            response = client.post(
                "/api/hooks/execute",
                json={
                    "hook_type": "PreToolUse",
                    "source": "droid",
                    "input_data": {"session_id": "droid-123", "tool_name": "Read"},
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["continue"] is True
        assert "droid adapter failed" in data["systemMessage"]
        assert "hookSpecificOutput" not in data

    @pytest.mark.parametrize(
        ("source", "hook_type", "adapter_patch"),
        [
            ("claude", "pre-tool-use", "gobby.adapters.claude_code.ClaudeCodeAdapter"),
            ("gemini", "BeforeTool", "gobby.adapters.gemini.GeminiAdapter"),
            ("qwen", "BeforeTool", "gobby.adapters.qwen.QwenAdapter"),
            ("droid", "PreToolUse", "gobby.adapters.droid.DroidAdapter"),
        ],
    )
    def test_execute_hook_normalizes_provider_pre_tool_use_for_hold_open(
        self,
        session_storage: SessionManager,
        source: str,
        hook_type: str,
        adapter_patch: str,
    ) -> None:
        """Web-chat approval hold-open must normalize raw provider hook names."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = MagicMock()
        server.app.state.hook_manager = mock_hook_manager

        with (
            TestClient(server.app) as client,
            patch(adapter_patch) as MockAdapter,
            patch(
                "gobby.servers.routes.mcp.hooks._maybe_hold_open",
                new_callable=AsyncMock,
            ) as mock_hold_open,
        ):
            mock_adapter = MagicMock()
            mock_adapter.handle_native.return_value = {"continue": True}
            MockAdapter.return_value = mock_adapter
            mock_hold_open.return_value = {"decision": "approve"}

            response = client.post(
                "/api/hooks/execute",
                headers={"X-Gobby-Session-Id": "sess-web-1"},
                json={
                    "hook_type": hook_type,
                    "source": source,
                    "input_data": {"tool_name": "Bash", "arguments": {"command": "pwd"}},
                },
            )

        assert response.status_code == 200
        assert response.json() == {"decision": "approve"}
        mock_hold_open.assert_awaited_once()
        args = mock_hold_open.await_args.args
        assert args[1] == "sess-web-1"
        assert args[2] == "PreToolUse"
        assert args[4] == source

    def test_execute_hook_codex_source(self, session_storage: SessionManager) -> None:
        """Test execute hook with Codex source uses CodexHooksAdapter."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = MagicMock()
        server.app.state.hook_manager = mock_hook_manager

        with (
            TestClient(server.app) as client,
            patch("gobby.adapters.codex_impl.hooks_adapter.CodexHooksAdapter") as MockAdapter,
        ):
            mock_adapter = MagicMock()
            mock_adapter.handle_native.return_value = {"continue": True}
            MockAdapter.return_value = mock_adapter

            response = client.post(
                "/api/hooks/execute",
                json={
                    "hook_type": "SessionStart",
                    "source": "codex",
                    "input_data": {"session_id": "test-123", "cwd": "/tmp"},
                },
            )

        assert response.status_code == 200
        assert response.json()["continue"] is True
        MockAdapter.assert_called_once_with(hook_manager=mock_hook_manager)
        assert mock_adapter.handle_native.call_args.args[0] == {
            "hook_type": "SessionStart",
            "source": "codex",
            "input_data": {"session_id": "test-123", "cwd": "/tmp"},
        }

    def test_execute_hook_codex_root_cwd_project_miss_logs_debug(
        self, session_storage: SessionManager
    ) -> None:
        """Benign Codex GUI root-cwd hooks must not emit invalid-hook warnings."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = MagicMock()
        server.app.state.hook_manager = mock_hook_manager

        with (
            TestClient(server.app) as client,
            patch("gobby.adapters.codex_impl.hooks_adapter.CodexHooksAdapter") as MockAdapter,
            patch("gobby.servers.routes.mcp.hooks.logger.warning") as warning,
            patch("gobby.servers.routes.mcp.hooks.logger.debug") as debug,
        ):
            mock_adapter = MagicMock()
            mock_adapter.handle_native.side_effect = ValueError(
                "No .gobby/project.json found in /. "
                "Run 'gobby init' in your project directory first."
            )
            MockAdapter.return_value = mock_adapter

            response = client.post(
                "/api/hooks/execute",
                json={
                    "hook_type": "SessionStart",
                    "source": "codex",
                    "input_data": {"session_id": "test-123", "cwd": "/"},
                },
            )

        assert response.status_code == 200
        assert response.json()["continue"] is True
        warning.assert_not_called()
        debug.assert_called()

    def test_execute_hook_codex_envelope_source(self, session_storage: SessionManager) -> None:
        """Envelope-shaped Codex requests should normalize before adapter dispatch."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = MagicMock()
        server.app.state.hook_manager = mock_hook_manager

        with (
            TestClient(server.app) as client,
            patch("gobby.adapters.codex_impl.hooks_adapter.CodexHooksAdapter") as MockAdapter,
        ):
            mock_adapter = MagicMock()
            mock_adapter.handle_native.return_value = {"continue": True}
            MockAdapter.return_value = mock_adapter

            response = client.post(
                "/api/hooks/execute",
                json={
                    "schema_version": 1,
                    "enqueued_at": "2026-04-16T12:00:00Z",
                    "critical": True,
                    "hook_type": "SessionStart",
                    "source": "codex",
                    "input_data": {"session_id": "test-envelope", "cwd": "/tmp"},
                    "headers": {"X-Gobby-Session-Id": "embedded-codex"},
                },
            )

        assert response.status_code == 200
        assert response.json()["continue"] is True
        assert mock_adapter.handle_native.call_args.args[0] == {
            "hook_type": "SessionStart",
            "source": "codex",
            "input_data": {"session_id": "test-envelope", "cwd": "/tmp"},
        }

    def test_execute_hook_rejects_unsupported_envelope_schema_version(
        self, session_storage: SessionManager
    ) -> None:
        """Envelope requests with an unknown schema version should fail fast."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server.app.state.hook_manager = MagicMock()

        with TestClient(server.app) as client:
            response = client.post(
                "/api/hooks/execute",
                json={
                    "schema_version": 99,
                    "hook_type": "session-start",
                    "source": "claude",
                    "input_data": {},
                },
            )

        assert response.status_code == 400
        assert "Unsupported schema_version" in response.json()["detail"]

    def test_execute_hook_envelope_requires_source(self, session_storage: SessionManager) -> None:
        """Envelope requests still require source after normalization."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server.app.state.hook_manager = MagicMock()

        with TestClient(server.app) as client:
            response = client.post(
                "/api/hooks/execute",
                json={
                    "schema_version": 1,
                    "hook_type": "session-start",
                    "input_data": {},
                },
            )

        assert response.status_code == 400
        assert response.json()["detail"] == "source required"

    def test_execute_hook_envelope_requires_hook_type(
        self, session_storage: SessionManager
    ) -> None:
        """Envelope requests still require hook_type after normalization."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server.app.state.hook_manager = MagicMock()

        with TestClient(server.app) as client:
            response = client.post(
                "/api/hooks/execute",
                json={
                    "schema_version": 1,
                    "source": "claude",
                    "input_data": {},
                },
            )

        assert response.status_code == 400
        assert response.json()["detail"] == "hook_type required"

    def test_execute_hook_envelope_critical_metadata_does_not_change_payload(
        self, session_storage: SessionManager
    ) -> None:
        """Critical metadata is observational and must not affect normalized hook handling."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = MagicMock()
        server.app.state.hook_manager = mock_hook_manager

        with (
            TestClient(server.app) as client,
            patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as MockAdapter,
        ):
            mock_adapter = MagicMock()
            mock_adapter.handle_native.return_value = {"continue": True}
            MockAdapter.return_value = mock_adapter

            base_envelope = {
                "schema_version": 1,
                "enqueued_at": "2026-04-16T12:00:00Z",
                "hook_type": "post-tool-use",
                "source": "claude",
                "input_data": {"tool_name": "Bash"},
            }
            responses = []
            normalized_payloads = []
            for critical in (False, True):
                response = client.post(
                    "/api/hooks/execute",
                    json={**base_envelope, "critical": critical},
                )
                responses.append(response.json())
                normalized_payloads.append(mock_adapter.handle_native.call_args.args[0])

        assert responses == [{"continue": True}, {"continue": True}]
        assert normalized_payloads == [
            {
                "hook_type": "post-tool-use",
                "source": "claude",
                "input_data": {"tool_name": "Bash"},
            },
            {
                "hook_type": "post-tool-use",
                "source": "claude",
                "input_data": {"tool_name": "Bash"},
            },
        ]

    def test_execute_hook_envelope_headers_do_not_override_http_headers(
        self, session_storage: SessionManager
    ) -> None:
        """Ingress must trust actual HTTP headers over the embedded envelope copy."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = MagicMock()
        server.app.state.hook_manager = mock_hook_manager

        with (
            TestClient(server.app) as client,
            patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as MockAdapter,
            patch(
                "gobby.servers.routes.mcp.hooks._maybe_hold_open",
                new_callable=AsyncMock,
            ) as mock_hold_open,
        ):
            mock_adapter = MagicMock()
            mock_adapter.handle_native.return_value = {"continue": True}
            MockAdapter.return_value = mock_adapter
            mock_hold_open.return_value = {"decision": "approve"}

            response = client.post(
                "/api/hooks/execute",
                headers={"X-Gobby-Session-Id": "real-session"},
                json={
                    "schema_version": 1,
                    "enqueued_at": "2026-04-16T12:00:00Z",
                    "critical": False,
                    "hook_type": "pre-tool-use",
                    "source": "claude",
                    "input_data": {"tool_name": "Bash", "arguments": {"command": "pwd"}},
                    "headers": {"X-Gobby-Session-Id": "embedded-session"},
                },
            )

        assert response.status_code == 200
        assert response.json() == {"decision": "approve"}
        mock_hold_open.assert_awaited_once()
        assert mock_hold_open.await_args.args[1] == "real-session"

    def test_execute_hook_logs_enqueued_at_for_envelope_requests(
        self, session_storage: SessionManager
    ) -> None:
        """Envelope metadata should be surfaced in structured route logging."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = MagicMock()
        server.app.state.hook_manager = mock_hook_manager

        with (
            TestClient(server.app) as client,
            patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as MockAdapter,
            patch("gobby.servers.routes.mcp.hooks.logger") as mock_logger,
        ):
            mock_adapter = MagicMock()
            mock_adapter.handle_native.return_value = {"continue": True}
            MockAdapter.return_value = mock_adapter

            response = client.post(
                "/api/hooks/execute",
                json={
                    "schema_version": 1,
                    "enqueued_at": "2026-04-16T12:34:56Z",
                    "critical": False,
                    "hook_type": "session-start",
                    "source": "claude",
                    "input_data": {},
                },
            )

        assert response.status_code == 200
        matching_logs = [
            call
            for call in mock_logger.debug.call_args_list
            if call.args and call.args[0] == "Hook executed: session-start"
        ]
        assert len(matching_logs) == 1
        assert matching_logs[0].kwargs["extra"]["request_shape"] == "envelope"
        assert matching_logs[0].kwargs["extra"]["enqueued_at"] == "2026-04-16T12:34:56Z"

    def test_execute_hook_codex_uses_hooks_adapter_not_app_server_adapter(
        self, session_storage: SessionManager
    ) -> None:
        """Regression: Codex HTTP hooks must use CodexHooksAdapter even when
        the app-server CodexAdapter is connected.

        The app-server adapter expects JSON-RPC format and silently drops
        hooks.json payloads, breaking all hook enforcement for Codex.
        """
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = MagicMock()
        server.app.state.hook_manager = mock_hook_manager
        # Simulate connected app-server adapter on app.state
        ws_adapter = MagicMock(name="WebSocketCodexAdapter")
        server.app.state.codex_adapter = ws_adapter

        with (
            TestClient(server.app) as client,
            patch("gobby.adapters.codex_impl.hooks_adapter.CodexHooksAdapter") as MockHooksAdapter,
        ):
            mock_adapter = MagicMock()
            mock_adapter.handle_native.return_value = {"continue": True}
            MockHooksAdapter.return_value = mock_adapter

            response = client.post(
                "/api/hooks/execute",
                json={
                    "hook_type": "SessionStart",
                    "source": "codex",
                    "input_data": {"session_id": "test-456", "cwd": "/tmp"},
                },
            )

        assert response.status_code == 200
        # Must use CodexHooksAdapter, NOT the app-server adapter
        MockHooksAdapter.assert_called_once_with(hook_manager=mock_hook_manager)
        assert MockHooksAdapter.call_count == 1
        assert MockHooksAdapter.call_args is not None
        mock_adapter.handle_native.assert_called_once()
        assert mock_adapter.handle_native.call_count == 1
        assert mock_adapter.handle_native.call_args is not None
        # The app-server adapter must NOT have been called
        ws_adapter.handle_native.assert_not_called()
        assert ws_adapter.handle_native.call_count == 0
        assert not ws_adapter.handle_native.called

    def test_execute_hook_codex_stop_block_propagates(
        self, session_storage: SessionManager
    ) -> None:
        """Codex Stop hook block decisions must propagate through the HTTP response."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = MagicMock()
        server.app.state.hook_manager = mock_hook_manager

        with (
            TestClient(server.app) as client,
            patch("gobby.adapters.codex_impl.hooks_adapter.CodexHooksAdapter") as MockAdapter,
        ):
            mock_adapter = MagicMock()
            mock_adapter.handle_native.return_value = {
                "continue": False,
                "stopReason": "Task #11678 is still claimed — commit and close first.",
            }
            MockAdapter.return_value = mock_adapter

            response = client.post(
                "/api/hooks/execute",
                json={
                    "hook_type": "Stop",
                    "source": "codex",
                    "input_data": {"session_id": "test-stop"},
                },
            )

        assert response.status_code == 200
        result = response.json()
        assert result["continue"] is False
        assert "claimed" in result["stopReason"]


class TestCodexValidateSettings:
    """Tests for Codex entry in validate_settings CLI_VALIDATION_CONFIGS."""

    def test_codex_config_exists(self) -> None:
        """Codex must be registered in CLI_VALIDATION_CONFIGS."""
        from gobby.install.shared.hooks.validate_settings import CLI_VALIDATION_CONFIGS

        assert "codex" in CLI_VALIDATION_CONFIGS

    def test_codex_config_hooks_json(self) -> None:
        """Codex validation must target hooks.json, not settings.json."""
        from gobby.install.shared.hooks.validate_settings import CLI_VALIDATION_CONFIGS

        config = CLI_VALIDATION_CONFIGS["codex"]
        assert config.settings_file == "hooks.json"
        assert config.settings_dir == ".codex"

    def test_codex_required_hooks(self) -> None:
        """All critical Codex hook types must be required."""
        from gobby.install.shared.hooks.validate_settings import CLI_VALIDATION_CONFIGS

        config = CLI_VALIDATION_CONFIGS["codex"]
        for hook in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"):
            assert hook in config.required_hooks, f"Missing required hook: {hook}"


# ============================================================================
# Webhooks Endpoint Tests
# ============================================================================


class TestWebhooksEndpoints:
    """Tests for /webhooks endpoints."""

    @pytest.fixture
    def webhooks_server(self, session_storage: SessionManager) -> HTTPServer:
        """Create server for webhooks tests."""
        return create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )

    @pytest.fixture
    def webhooks_client(self, webhooks_server: HTTPServer) -> Iterator[TestClient]:
        """Create test client for webhooks endpoints."""
        with TestClient(webhooks_server.app) as c:
            yield c

    def test_list_webhooks_no_config(self, webhooks_client: TestClient) -> None:
        """Test list webhooks when config is None."""
        response = webhooks_client.get("/api/webhooks")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["enabled"] is False
        assert data["endpoints"] == []

    # Note: Webhook tests with config are tested via integration tests as they require
    # proper config setup that interacts with lifespan

    def test_test_webhook_missing_name(self, webhooks_client: TestClient) -> None:
        """Test webhook test with missing name."""
        response = webhooks_client.post("/api/webhooks/test", json={})
        assert response.status_code == 400
        assert "Webhook name required" in response.json()["detail"]

    def test_test_webhook_no_config(self, webhooks_client: TestClient) -> None:
        """Test webhook test when config is None."""
        response = webhooks_client.post(
            "/api/webhooks/test",
            json={"name": "test-webhook"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Configuration not available" in data["error"]

    # Note: Webhook test endpoint tests with config are tested via integration tests

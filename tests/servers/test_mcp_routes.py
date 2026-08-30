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

import asyncio
import concurrent.futures
import json
import threading
import time
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from gobby.adapters.qwen import QwenAdapter
from gobby.app_context import ServiceContainer
from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.hooks.agent_run_ingress import validate_managed_agent_hook
from gobby.hooks.envelope_dedupe import is_envelope_processed
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.inbox import drain_hook_inbox_barrier
from gobby.hooks.runtime_compat import SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION
from gobby.mcp_proxy.lazy import CircuitBreakerOpen
from gobby.mcp_proxy.models import MCPError
from gobby.mcp_proxy.schema_hash import SchemaHashManager, compute_schema_hash
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.mcp_proxy.tools.internal import InternalRegistryManager
from gobby.mcp_proxy.wait_tools import (
    MCP_WRAPPER_PROTOCOL_VERSION,
    MCP_WRAPPER_PROTOCOL_VERSION_HEADER,
    MCP_WRAPPER_STALE_ERROR_CODE,
)
from gobby.servers.http import HTTPServer
from gobby.storage.auth import AuthStore, hash_token
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import GLOBAL_PROJECT_ID, LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import TERMINAL_CONTEXT_HEADER
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime
from gobby.workflows.hooks import WorkflowEvaluationTimeout, WorkflowHookHandler
from tests.servers.conftest import authenticate_test_server, create_http_server

pytestmark = pytest.mark.unit

# sessions.id is a native uuid column; web-chat session ids in the
# X-Gobby-Session-Id header must be valid UUID strings.
WEB_SESSION_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1"

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


# ============================================================================
# Fixtures
# ============================================================================


def _mock_hook_manager() -> MagicMock:
    manager = MagicMock()
    manager.shutdown_async = AsyncMock()
    return manager


def _hook_envelope(**payload: Any) -> dict[str, Any]:
    envelope = {
        "schema_version": SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
        "enqueued_at": "2026-04-16T12:00:00Z",
        "critical": False,
        "input_data": {},
    }
    envelope.update(payload)
    return envelope


@pytest.fixture
def session_storage(temp_db: HubDatabase) -> SessionManager:
    """Create session storage."""
    return SessionManager(temp_db)


@pytest.fixture
def project_storage(temp_db: HubDatabase) -> LocalProjectManager:
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
    mock_config.logging.dir = "/tmp"
    mock_config.memory.backend = "null"
    mock_config.workflow.timeout = 30
    mock_config.workflow.enabled = True
    mock_config.get_gobby_tasks_config.return_value.enabled = False

    services = ServiceContainer(
        database=session_storage.db,
        session_manager=session_storage,
        task_manager=MagicMock(),
    )
    return authenticate_test_server(
        HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
            bootstrap_config=BootstrapConfig(),
        )
    )


@pytest.fixture
def client(basic_http_server: HTTPServer) -> Iterator[TestClient]:
    """Create a test client that runs lifespan to set app.state.server."""
    with patch("gobby.servers.app_factory.HookManager") as MockHM:
        mock_instance = MockHM.return_value
        mock_instance._stop_registry = MagicMock()
        mock_instance.shutdown = MagicMock()
        mock_instance.shutdown_async = AsyncMock()
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
        self.url: str | None = None
        self.command: str | None = None
        self.args: list[str] | None = None
        self.env: dict[str, str] | None = None
        self.headers: dict[str, str] | None = None
        self.id = name
        self.project_id = GLOBAL_PROJECT_ID
        self.template: str | None = None
        self.template_id: str | None = None
        self.template_values: dict[str, str] | None = None
        self.description: str | None = None
        self.requires_oauth = False
        self.oauth_provider: str | None = None
        self.connect_timeout = 30.0
        self.tools: list[dict[str, Any]] | None = None


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
        self.input_schema = input_schema or {"type": "object", "properties": {}}


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
        self.project_id = GLOBAL_PROJECT_ID
        self.last_project_id: str | None = None
        self._sessions: dict[str, FakeMCPSession] = {}

    def put(self, config: FakeServerConfig) -> FakeServerConfig:
        self._configs[config.name] = config
        self._configs[config.id] = config
        if all(existing is not config for existing in self.server_configs):
            self.server_configs.append(config)
        return config

    def has_server(self, server_name: str) -> bool:
        """Check if server is configured."""
        return server_name in self._configs or any(
            getattr(config, "id", None) == server_name or config.name == server_name
            for config in self.server_configs
        )

    def get_server_config(self, server_id: str) -> FakeServerConfig | None:
        if server_id in self._configs:
            return self._configs[server_id]
        for config in self.server_configs:
            if config.name == server_id or getattr(config, "id", None) == server_id:
                return config
        return None

    def is_connected(self, server_id: str) -> bool:
        if server_id in self.connections:
            return True
        config = self.get_server_config(server_id)
        return bool(config is not None and config.name in self.connections)

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

    async def add_server(self, config: Any) -> dict[str, Any]:
        """Add a server configuration."""
        self.put(config)
        return {
            "success": True,
            "id": getattr(config, "id", config.name),
            "connected": False,
        }

    async def disconnect_all(self) -> None:
        self.connections.clear()
        self._sessions.clear()

    async def remove_server(self, name: str, project_id: str | None = None) -> None:
        """Remove a server configuration."""
        self.last_project_id = project_id
        config = self.get_server_config(name)
        if config is None:
            raise ValueError(f"Server not found: {name}")
        self._configs.pop(config.name, None)
        self._configs.pop(getattr(config, "id", ""), None)
        self.server_configs = [item for item in self.server_configs if item.name != config.name]

    async def update_server(
        self, name: str, config: Any, project_id: str | None = None
    ) -> dict[str, Any]:
        """Update a server configuration."""
        self.last_project_id = project_id
        existing = self.get_server_config(name)
        if existing is None:
            raise ValueError(f"Server not found: {name}")
        if isinstance(config, Mapping):
            for field in (
                "transport",
                "url",
                "command",
                "args",
                "env",
                "headers",
                "description",
                "enabled",
                "requires_oauth",
                "oauth_provider",
                "connect_timeout",
            ):
                if field in config:
                    setattr(existing, field, config[field])
            stored = existing
        else:
            stored = config
        self.put(stored)
        return {"success": True, "name": existing.name, "id": getattr(stored, "id", existing.name)}

    async def set_server_enabled(
        self, name: str, enabled: bool, project_id: str | None = None
    ) -> dict[str, Any]:
        """Toggle a server's enabled flag, mirroring the real manager."""
        self.last_project_id = project_id
        config = self.get_server_config(name)
        if config is None:
            raise ValueError(f"Server not found: {name}")
        config.enabled = enabled
        return {"success": True, "name": config.name, "enabled": enabled}


class FakeInternalRegistry:
    """Fake internal tool registry for testing."""

    def __init__(
        self,
        name: str = "gobby-tasks",
        tools: list[dict[str, Any]] | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.result = result
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
        if self.result is not None:
            return self.result
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
        """Resolved session identity should drive discovery tracking."""
        session = session_storage.register(
            external_id="discovery-session",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=None,
        )
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
                headers={"X-Gobby-Session-Id": session.id},
            )

        assert response.status_code == 200
        server._tools_handler.tool_proxy.record_listed_server.assert_called_once_with(
            "gobby-tasks",
            session_id=session.id,
        )

    def test_list_tools_does_not_emit_proxy_after_tool(
        self, session_storage: SessionManager
    ) -> None:
        """Successful list_tools should not emit a proxy AFTER_TOOL event."""
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
        server._tools_handler.tool_proxy.emit_synthetic_proxy_after_tool.assert_not_awaited()

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

    def test_list_tools_external_server_resolves_name_to_id_keyed_manager(
        self, session_storage: SessionManager
    ) -> None:
        """A name lookup must resolve to the id-keyed manager entry (#21292)."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        config = FakeServerConfig(name="external-server")
        config.id = "srv-uuid-1"
        mcp_manager._configs["srv-uuid-1"] = config
        mcp_manager.server_configs.append(config)
        mcp_manager._sessions["srv-uuid-1"] = FakeMCPSession(
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
        """Test listing tools with input_schema as dict."""
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
        tool.input_schema = {"type": "object", "properties": {"arg1": {"type": "string"}}}

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

    def test_list_tools_with_sdk_tool_model(self, session_storage: SessionManager) -> None:
        """Test listing tools from real SDK Tool models keeps the camelCase wire key."""
        from mcp.types import ListToolsResult, Tool

        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        config = FakeServerConfig(name="model-server")
        mcp_manager._configs["model-server"] = config

        tools_result = ListToolsResult(
            tools=[
                Tool(
                    name="model-tool",
                    description="Tool with model schema",
                    input_schema={"type": "object", "required": ["id"]},
                )
            ]
        )
        session = MagicMock()
        session.list_tools = AsyncMock(return_value=tools_result)
        mcp_manager.ensure_connected = AsyncMock(return_value=session)
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.get("/api/mcp/model-server/tools")

        assert response.status_code == 200
        data = response.json()
        assert data["tools"][0]["inputSchema"] == {"type": "object", "required": ["id"]}
        assert "input_schema" not in data["tools"][0]


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
        config.env = {
            "RAW_TOKEN": "raw-env-secret",
            "TOKEN_REF": "$secret:mcp_token",
        }
        config.headers = {
            "Authorization": "Bearer raw-header-secret",
            "X-Token-Ref": "$secret:mcp_header",
        }
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
        assert data["servers"][0]["env"] == {"TOKEN_REF": "$secret:mcp_token"}
        assert data["servers"][0]["headers"] == {"X-Token-Ref": "$secret:mcp_header"}
        assert "raw-env-secret" not in response.text
        assert "raw-header-secret" not in response.text

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
        server._internal_manager = cast(
            Any,
            FakeInternalManager(
                [
                    FakeInternalRegistry(name="gobby-tasks"),
                ]
            ),
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
        assert "tool_name" in response.json()["detail"]["error"]

    def test_get_schema_internal_server_success(self, session_storage: SessionManager) -> None:
        """Test getting schema from internal server."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._internal_manager = cast(
            Any,
            FakeInternalManager(
                [
                    FakeInternalRegistry(name="gobby-tasks"),
                ]
            ),
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

    def test_get_schema_does_not_emit_after_tool_with_session_header(
        self, session_storage: SessionManager
    ) -> None:
        """Session header should not trigger proxy AFTER_TOOL synthesis."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._internal_manager = FakeInternalManager(
            [
                FakeInternalRegistry(
                    name="gobby-tasks",
                    result={
                        "success": True,
                        "tool": "list_tasks",
                        "created_at": datetime(2026, 7, 3, 12, 34, tzinfo=UTC),
                    },
                ),
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
        server._tools_handler.tool_proxy.emit_synthetic_proxy_after_tool.assert_not_awaited()

    def test_get_schema_does_not_emit_proxy_after_tool(
        self, session_storage: SessionManager
    ) -> None:
        """Successful get_tool_schema should not emit a proxy AFTER_TOOL event."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._internal_manager = cast(
            Any,
            FakeInternalManager(
                [
                    FakeInternalRegistry(name="gobby-tasks"),
                ]
            ),
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
        server._tools_handler.tool_proxy.emit_synthetic_proxy_after_tool.assert_not_awaited()

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
            machine_id="21000000-0000-4000-8000-000000000001",
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
        server._tools_handler.tool_proxy.emit_synthetic_proxy_after_tool.assert_not_awaited()

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

    def test_get_schema_internal_success_records_lease(
        self, session_storage: SessionManager
    ) -> None:
        """A successfully served internal schema grants the unlocked_tools lease."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._internal_manager = cast(
            Any,
            FakeInternalManager(
                [
                    FakeInternalRegistry(name="gobby-tasks"),
                ]
            ),
        )
        server._tools_handler = MagicMock(tool_proxy=MagicMock())

        with patch("gobby.servers.routes.mcp.endpoints.execution.record_schema_shown") as record:
            with TestClient(server.app) as client:
                response = client.post(
                    "/api/mcp/tools/schema",
                    json={
                        "server_name": "gobby-tasks",
                        "tool_name": "list_tasks",
                        "session_id": "#77",
                    },
                )

        assert response.status_code == 200
        record.assert_called_once_with(
            server.tool_proxy,
            "#77",
            server_name="gobby-tasks",
            tool_name="list_tasks",
        )

    def test_get_schema_external_success_records_lease(
        self, session_storage: SessionManager
    ) -> None:
        """A successfully served external schema grants the unlocked_tools lease."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        mcp_manager.put(FakeServerConfig(name="external-server"))
        mcp_manager.get_tool_info = AsyncMock(
            return_value={
                "name": "get_item",
                "inputSchema": {"type": "object"},
            }
        )
        server.mcp_manager = mcp_manager
        tool_proxy = MagicMock()
        tool_proxy.get_tool_schema = AsyncMock(
            return_value={
                "success": True,
                "tool": {"name": "get_item", "inputSchema": {"type": "object"}},
            }
        )
        server._tools_handler = MagicMock(tool_proxy=tool_proxy)

        with patch("gobby.servers.routes.mcp.endpoints.execution.record_schema_shown") as record:
            with TestClient(server.app) as client:
                response = client.post(
                    "/api/mcp/tools/schema",
                    json={
                        "server_name": "external-server",
                        "tool_name": "get_item",
                        "session_id": "#77",
                    },
                )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["name"] == "get_item"
        assert data["inputSchema"] == {"type": "object"}
        record.assert_called_once_with(
            server.tool_proxy,
            "#77",
            server_name="external-server",
            tool_name="get_item",
        )

    def test_get_schema_failure_records_no_lease(self, session_storage: SessionManager) -> None:
        """A failed schema lookup must not grant a lease."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        mcp_manager._configs["external-server"] = FakeServerConfig(name="external-server")
        mcp_manager.get_tool_info = AsyncMock(side_effect=ValueError("Tool not found"))
        server.mcp_manager = mcp_manager
        server._tools_handler = MagicMock(tool_proxy=MagicMock())

        with patch("gobby.servers.routes.mcp.endpoints.execution.record_schema_shown") as record:
            with TestClient(server.app) as client:
                response = client.post(
                    "/api/mcp/tools/schema",
                    json={
                        "server_name": "external-server",
                        "tool_name": "missing",
                        "session_id": "#77",
                    },
                )

        assert response.status_code == 200
        assert response.json()["success"] is False
        record.assert_not_called()


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

    def test_call_tool_tool_proxy_success_is_flattened(
        self, session_storage: SessionManager
    ) -> None:
        """ToolProxy success envelopes should stay flat at the HTTP boundary."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._tools_handler = MagicMock()
        server._tools_handler.tool_proxy = MagicMock()
        server._tools_handler.tool_proxy.call_tool = AsyncMock(
            return_value={"success": True, "items": [1, 2, 3]}
        )

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/tools/call",
                json={
                    "server_name": "gobby-tasks",
                    "tool_name": "list_tasks",
                    "arguments": {},
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["items"] == [1, 2, 3]
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
                FakeInternalRegistry(
                    name="gobby-tasks",
                    result={
                        "success": True,
                        "tool": "list_tasks",
                        "created_at": datetime(2026, 7, 3, 12, 34, tzinfo=UTC),
                    },
                ),
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
        assert data["result"] == {
            "tool": "list_tasks",
            "created_at": "2026-07-03T12:34:00+00:00",
        }
        assert "response_time_ms" in data

    def test_call_tool_allows_structured_wait_without_wrapper_protocol_header(
        self, session_storage: SessionManager
    ) -> None:
        """Structured calls remain available to callers outside the stdio wrapper."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._internal_manager = FakeInternalManager(
            [
                FakeInternalRegistry(
                    name="gobby-agents",
                    tools=[{"name": "wait_for_agent", "description": "Wait for an agent"}],
                    result={
                        "success": True,
                        "completed": False,
                        "notification_registered": True,
                        "notification_session_id": "session-123",
                    },
                ),
            ]
        )

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/tools/call",
                headers={"X-Gobby-Caller-Project-Id": "project-123"},
                json={
                    "server_name": "gobby-agents",
                    "tool_name": "wait_for_agent",
                    "arguments": {"run_id": "run-123"},
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["result"] == {
            "completed": False,
            "notification_registered": True,
            "notification_session_id": "session-123",
        }

    def test_call_tool_dispatches_drifted_wrapper_with_same_protocol_version(
        self,
        session_storage: SessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """Source-byte drift has no effect when the wait protocol remains compatible."""
        session = session_storage.register(
            external_id="wrapper-session",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=test_project["id"],
        )
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        registry = FakeInternalRegistry(
            name="gobby-agents",
            tools=[{"name": "wait_for_output", "description": "Wait for output"}],
        )
        registry_call = AsyncMock(wraps=registry.call)
        server._internal_manager = cast(Any, FakeInternalManager([registry]))

        with patch.object(registry, "call", registry_call), TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/tools/call",
                headers={
                    MCP_WRAPPER_PROTOCOL_VERSION_HEADER: MCP_WRAPPER_PROTOCOL_VERSION,
                    "X-Gobby-MCP-Wrapper-Fingerprint": "digest-before-source-drift",
                    "X-Gobby-Session-Id": session.id,
                    "X-Gobby-Caller-Project-Id": test_project["id"],
                },
                json={
                    "server_name": "gobby-agents",
                    "tool_name": "wait_for_output",
                    "arguments": {"run_id": "run-123", "timeout_seconds": 1},
                },
            )

        assert response.status_code == 200
        assert response.json()["success"] is True
        registry_call.assert_awaited_once()

    def test_call_tool_rejects_incompatible_wrapper_protocol_version(
        self, session_storage: SessionManager
    ) -> None:
        """Wait dispatch refuses wrappers whose explicit protocol version differs."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        registry = FakeInternalRegistry(
            name="gobby-agents",
            tools=[{"name": "wait_for_output", "description": "Wait for output"}],
        )
        registry_call = AsyncMock(wraps=registry.call)
        server._internal_manager = cast(Any, FakeInternalManager([registry]))

        with patch.object(registry, "call", registry_call), TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/tools/call",
                headers={MCP_WRAPPER_PROTOCOL_VERSION_HEADER: "0"},
                json={
                    "server_name": "gobby-agents",
                    "tool_name": "wait_for_output",
                    "arguments": {"run_id": "run-123", "timeout_seconds": 1},
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["error_code"] == MCP_WRAPPER_STALE_ERROR_CODE
        assert data["provided_wrapper_protocol_version"] == "0"
        assert data["expected_wrapper_protocol_version"] == MCP_WRAPPER_PROTOCOL_VERSION
        assert data["restart_required"] is True
        registry_call.assert_not_awaited()

    def test_call_tool_wrapper_without_identity_fails_closed(
        self, session_storage: SessionManager
    ) -> None:
        """Structured wrapper calls still require independent caller identity."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        registry = FakeInternalRegistry(
            name="gobby-agents",
            tools=[{"name": "wait_for_agent", "description": "Wait for an agent"}],
        )
        registry.call = AsyncMock(wraps=registry.call)
        server._internal_manager = FakeInternalManager([registry])

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/tools/call",
                headers={
                    "X-Gobby-Caller-Project-Id": "project-123",
                    MCP_WRAPPER_PROTOCOL_VERSION_HEADER: MCP_WRAPPER_PROTOCOL_VERSION,
                },
                json={
                    "server_name": "gobby-agents",
                    "tool_name": "wait_for_agent",
                    "arguments": {"run_id": "run-123"},
                },
            )

        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "SESSION_REQUIRED"
        assert response.json()["detail"]["terminal_context_seen"] is False
        registry.call.assert_not_awaited()

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
        assert response.json() == {"detail": "Internal server error"}

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

    def test_call_tool_external_success_envelope_is_flattened(
        self, session_storage: SessionManager
    ) -> None:
        """External MCP success envelopes should not be nested under result."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        mcp_manager._configs["external-server"] = FakeServerConfig(name="external-server")
        mcp_manager.call_tool = AsyncMock(return_value={"success": True, "data": [1, 2, 3]})
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
        assert data["data"] == [1, 2, 3]
        assert "result" not in data

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
        assert response.json() == {"detail": "Internal server error"}

    @pytest.mark.parametrize(
        ("path", "payload"),
        [
            (
                "/api/mcp/tools/call",
                {
                    "server_name": "slow-server",
                    "tool_name": "slow_tool",
                    "arguments": {},
                },
            ),
            ("/api/mcp/slow-server/tools/slow_tool", {}),
        ],
    )
    def test_external_tool_timeout_returns_failure_envelope(
        self,
        session_storage: SessionManager,
        path: str,
        payload: dict[str, Any],
    ) -> None:
        """External calls use the configured timeout and return a flat failure envelope."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        config = FakeServerConfig(name="slow-server")
        mcp_manager = MagicMock()
        mcp_manager.server_configs = [config]
        mcp_manager.get_server_config.return_value = config
        mcp_manager.project_id = GLOBAL_PROJECT_ID
        mcp_manager.call_tool = AsyncMock(side_effect=TimeoutError)
        server.mcp_manager = mcp_manager
        server._tools_handler = MagicMock(
            tool_proxy=ToolProxyService(mcp_manager, validate_arguments=False)
        )

        with (
            patch(
                "gobby.servers.routes.mcp.endpoints.execution._mcp_call_timeout",
                return_value=0.01,
            ),
            TestClient(server.app) as client,
        ):
            response = client.post(path, json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["error"] == "Tool call timed out after 0.01 seconds"
        assert data["error_code"] == "CONNECTION_ERROR"
        assert "response_time_ms" in data
        mcp_manager.call_tool.assert_awaited_once_with(
            "slow-server",
            tool_name="slow_tool",
            arguments={},
            session_id=None,
            timeout=0.01,
        )


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

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data.get("scope") == "global"

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

    def test_add_server_rejects_string_enabled(self, session_storage: SessionManager) -> None:
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
                    "enabled": "false",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["error"] == "enabled must be a boolean"
        mcp_manager.add_server.assert_not_called()

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
# update_mcp_server Endpoint Tests
# ============================================================================


class TestUpdateMCPServer:
    """Tests for PUT /mcp/servers/{name} endpoint."""

    def test_update_server_success(self, session_storage: SessionManager) -> None:
        """Updating a server persists the full editable config through the manager."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        mcp_manager.put(FakeServerConfig(name="github", transport="stdio"))
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.put(
                "/api/mcp/servers/github",
                json={
                    "name": "github",
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env": {"GITHUB_TOKEN": "secret"},
                    "headers": {"Authorization": "Bearer token"},
                    "description": "GitHub tools",
                    "scope": "global",
                    "enabled": False,
                    "requires_oauth": True,
                    "oauth_provider": "github",
                    "connect_timeout": 45,
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        updated = mcp_manager._configs["github"]
        assert updated.command == "npx"
        assert updated.args == ["-y", "@modelcontextprotocol/server-github"]
        assert updated.env == {"GITHUB_TOKEN": "secret"}
        assert updated.headers == {"Authorization": "Bearer token"}
        assert updated.description == "GitHub tools"
        assert updated.requires_oauth is True
        assert updated.oauth_provider == "github"
        assert updated.connect_timeout == 45
        assert mcp_manager.last_project_id == GLOBAL_PROJECT_ID


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
        mcp_manager.put(FakeServerConfig(name="test-server"))
        mcp_manager.remove_server = AsyncMock()
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.delete("/api/mcp/servers/test-server")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        mcp_manager.remove_server.assert_awaited_once_with(
            "test-server",
            project_id=GLOBAL_PROJECT_ID,
        )

    def test_remove_server_not_found(self, session_storage: SessionManager) -> None:
        """Test removing non-existent server."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.delete("/api/mcp/servers/nonexistent")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]["error"]


# ============================================================================
# set_mcp_server_enabled Endpoint Tests
# ============================================================================


class TestSetMCPServerEnabled:
    """Tests for PATCH /mcp/servers/{name} endpoint."""

    def test_set_enabled_no_manager(self, client: TestClient) -> None:
        """Test toggling when MCP manager not available."""
        response = client.patch("/api/mcp/servers/test-server", json={"enabled": False})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "MCP manager not available" in data["error"]

    def test_set_enabled_requires_boolean(self, session_storage: SessionManager) -> None:
        """Empty PATCH of an unknown server is 404; non-boolean enabled is 400."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server.mcp_manager = FakeMCPManager()

        with TestClient(server.app) as client:
            response = client.patch("/api/mcp/servers/test-server", json={})

        assert response.status_code == 404

        with TestClient(server.app) as client:
            response = client.patch("/api/mcp/servers/test-server", json={"enabled": "true"})

        assert response.status_code == 400

    def test_disable_then_enable_round_trip(self, session_storage: SessionManager) -> None:
        """Disabling then re-enabling persists through the manager."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        config = FakeServerConfig(name="github", transport="http", enabled=True)
        mcp_manager.put(config)
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            disable = client.patch("/api/mcp/servers/github", json={"enabled": False})
            assert disable.status_code == 200
            assert disable.json()["success"] is True
            assert disable.json()["enabled"] is False
            assert config.enabled is False
            assert mcp_manager.last_project_id == GLOBAL_PROJECT_ID

            enable = client.patch("/api/mcp/servers/github", json={"enabled": True})
            assert enable.status_code == 200
            assert enable.json()["enabled"] is True
            assert config.enabled is True
            assert mcp_manager.last_project_id == GLOBAL_PROJECT_ID

    def test_set_enabled_not_found(self, session_storage: SessionManager) -> None:
        """Test toggling a server that is not configured."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.patch("/api/mcp/servers/nope", json={"enabled": True})

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]["error"]

    @pytest.mark.parametrize(
        "error",
        [
            RuntimeError("connection failed"),
            MCPError("mcp connection failed"),
            CircuitBreakerOpen("github", 3.0),
            KeyError("bad state"),
        ],
    )
    def test_set_enabled_known_manager_errors_return_failure_response(
        self, session_storage: SessionManager, error: Exception
    ) -> None:
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        mcp_manager.put(FakeServerConfig(name="github"))
        mcp_manager.set_server_enabled = AsyncMock(side_effect=error)  # type: ignore[method-assign]
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.patch("/api/mcp/servers/github", json={"enabled": True})

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["error"] == str(error)

    def test_set_enabled_unexpected_manager_error_propagates(
        self, session_storage: SessionManager
    ) -> None:
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        mcp_manager.put(FakeServerConfig(name="github"))
        mcp_manager.set_server_enabled = AsyncMock(  # type: ignore[method-assign]
            side_effect=PermissionError("permission denied")
        )
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.patch("/api/mcp/servers/github", json={"enabled": True})

        assert response.status_code == 403
        data = response.json()["detail"]
        assert data["success"] is False
        assert "permission denied" in data["error"]


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
        assert "other-project" in data["error"]

    def test_import_preview_does_not_broadcast_imported_event(
        self, session_storage: SessionManager
    ) -> None:
        """Preview-only synthesized imports do not broadcast persisted imports."""
        websocket_server = MagicMock()
        websocket_server.broadcast_mcp_event = AsyncMock()
        server = create_http_server(
            port=60887,
            test_mode=True,
            config=DaemonConfig(),
            session_manager=session_storage,
            websocket_server=websocket_server,
        )
        importer = MagicMock()
        importer.import_from_github = AsyncMock(
            return_value={
                "status": "requires_approval",
                "requires_approval": True,
                "config": {"name": "preview", "transport": "stdio"},
                "missing": [],
            }
        )

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": "test-project", "name": "test"},
            ),
            patch("gobby.mcp_proxy.importer.MCPServerImporter", return_value=importer),
        ):
            response = client.post(
                "/api/mcp/servers/import",
                json={"github_url": "https://github.com/example/mcp-server"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "requires_approval"
        websocket_server.broadcast_mcp_event.assert_not_awaited()

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
        server._internal_manager = cast(
            Any,
            FakeInternalManager(
                [
                    FakeInternalRegistry(name="gobby-tasks"),
                ]
            ),
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
                FakeInternalRegistry(
                    name="gobby-tasks",
                    result={
                        "success": True,
                        "tool": "list_tasks",
                        "updated_at": datetime(2026, 7, 3, 12, 35, tzinfo=UTC),
                    },
                ),
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
        assert data["result"] == {
            "tool": "list_tasks",
            "updated_at": "2026-07-03T12:35:00+00:00",
        }

    def test_proxy_rejects_missing_wait_wrapper_protocol_version(
        self, session_storage: SessionManager
    ) -> None:
        """Legacy route rejects wrappers without protocol version headers."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._internal_manager = FakeInternalManager(
            [
                FakeInternalRegistry(
                    name="gobby-agents",
                    tools=[{"name": "wait_for_output", "description": "Wait for output"}],
                ),
            ]
        )

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/gobby-agents/tools/wait_for_output",
                json={"run_id": "run-123", "timeout_seconds": 1},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["error_code"] == MCP_WRAPPER_STALE_ERROR_CODE
        assert data["restart_required"] is True

    def test_proxy_rejects_incompatible_wait_wrapper_protocol_version(
        self, session_storage: SessionManager
    ) -> None:
        """Legacy route rejects wrappers with incompatible protocol versions."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._internal_manager = FakeInternalManager(
            [
                FakeInternalRegistry(
                    name="gobby-agents",
                    tools=[{"name": "wait_for_output", "description": "Wait for output"}],
                ),
            ]
        )

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/gobby-agents/tools/wait_for_output",
                headers={MCP_WRAPPER_PROTOCOL_VERSION_HEADER: "0"},
                json={"run_id": "run-123", "timeout_seconds": 1},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["error_code"] == MCP_WRAPPER_STALE_ERROR_CODE
        assert data["provided_wrapper_protocol_version"] == "0"
        assert data["expected_wrapper_protocol_version"] == MCP_WRAPPER_PROTOCOL_VERSION
        assert data["restart_required"] is True

    def test_proxy_accepts_current_wait_wrapper_protocol_version(
        self,
        session_storage: SessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """Fresh ambient wrappers resolve terminal identity before dispatch."""
        terminal_context = {"parent_pid": 4242, "tmux_pane": "%12"}
        session = session_storage.register(
            external_id="ambient-session",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=test_project["id"],
            terminal_context=terminal_context,
        )
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._internal_manager = FakeInternalManager(
            [
                FakeInternalRegistry(
                    name="gobby-sessions",
                    tools=[{"name": "get_handoff", "description": "Wait for a summary"}],
                ),
            ]
        )

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/gobby-sessions/tools/get_handoff",
                headers={
                    MCP_WRAPPER_PROTOCOL_VERSION_HEADER: MCP_WRAPPER_PROTOCOL_VERSION,
                    TERMINAL_CONTEXT_HEADER: json.dumps(terminal_context),
                    "X-Gobby-Caller-Project-Id": test_project["id"],
                },
                json={"session_id": "sess-123"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["result"] == {"tool": "get_handoff"}
        resolved_session = session_storage.get(session.id)
        assert resolved_session is not None
        assert resolved_session.status == "active"

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
        assert response.json() == {"detail": "Internal server error"}

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

    def test_proxy_external_success_envelope_is_flattened(
        self, session_storage: SessionManager
    ) -> None:
        """Proxy success envelopes should not be nested under result."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mcp_manager = FakeMCPManager()
        mcp_manager._configs["external-server"] = FakeServerConfig(name="external-server")
        mcp_manager.call_tool = AsyncMock(return_value={"success": True, "items": [1, 2, 3]})
        server.mcp_manager = mcp_manager

        with TestClient(server.app) as client:
            response = client.post(
                "/api/mcp/external-server/tools/list_items",
                json={"limit": 10},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["items"] == [1, 2, 3]
        assert "result" not in data

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
        assert response.json() == {"detail": "Internal server error"}


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
        server._internal_manager = cast(
            Any,
            FakeInternalManager(
                [
                    FakeInternalRegistry(name="gobby-tasks"),
                ]
            ),
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
        server._internal_manager = cast(
            InternalRegistryManager,
            FakeInternalManager(
                [
                    FakeInternalRegistry(name="gobby-tasks"),
                ]
            ),
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

    def test_refresh_tools_description_change_reembeds(
        self, session_storage: SessionManager
    ) -> None:
        """A description-only change refreshes its hash and embedding."""
        registry = FakeInternalRegistry(
            name="gobby-tasks",
            tools=[{"name": "list_tasks", "description": "New description"}],
        )
        schema = registry.get_schema("list_tasks")
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server._internal_manager = cast(InternalRegistryManager, FakeInternalManager([registry]))

        mock_mcp_db_manager = MagicMock()
        mock_mcp_db_manager.db = MagicMock()
        mock_mcp_db_manager.get_cached_tools.return_value = []
        server._mcp_db_manager = mock_mcp_db_manager

        semantic_search = MagicMock()
        semantic_search.embed_tool = AsyncMock()
        mock_handler = MagicMock()
        mock_handler._semantic_search = semantic_search
        server._tools_handler = mock_handler

        hash_manager = SchemaHashManager(db=MagicMock())
        stored_hashes = [
            MagicMock(
                tool_name="list_tasks",
                schema_hash=compute_schema_hash(schema, description="Old description"),
            )
        ]

        with (
            TestClient(server.app) as client,
            patch.object(server, "resolve_project_id", return_value="test-project"),
            patch(
                "gobby.mcp_proxy.schema_hash.SchemaHashManager",
                return_value=hash_manager,
            ),
            patch.object(
                hash_manager,
                "get_hashes_for_server",
                return_value=stored_hashes,
            ),
            patch.object(hash_manager, "store_hash") as mock_store_hash,
            patch.object(hash_manager, "cleanup_stale_hashes", return_value=0),
        ):
            response = client.post("/api/mcp/refresh", json={"force": False})

        assert response.status_code == 200
        data = response.json()
        assert data["stats"]["tools_changed"] == 1
        assert data["stats"]["embeddings_generated"] == 1
        semantic_search.embed_tool.assert_awaited_once()
        mock_store_hash.assert_called_once_with(
            server_name="gobby-tasks",
            tool_name="list_tasks",
            project_id=GLOBAL_PROJECT_ID,
            schema_hash=compute_schema_hash(schema, description="New description"),
        )


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
            json=_hook_envelope(source="claude"),
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
            json=_hook_envelope(hook_type="session-start"),
        )
        assert response.status_code == 400
        assert "source" in response.json()["detail"]

    def test_execute_hook_rejects_flat_payload(self, client: TestClient) -> None:
        """Flat ghook payloads are no longer accepted by the route."""
        response = client.post(
            "/api/hooks/execute",
            json={"hook_type": "session-start", "source": "claude", "input_data": {}},
        )
        assert response.status_code == 400
        assert "Unsupported schema_version: None" in response.json()["detail"]

    def test_execute_hook_unsupported_source(self, session_storage: SessionManager) -> None:
        """Test execute hook with unsupported source."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server.app.state.hook_manager = _mock_hook_manager()

        with TestClient(server.app) as client:
            response = client.post(
                "/api/hooks/execute",
                json=_hook_envelope(hook_type="session-start", source="unsupported"),
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
            config=DaemonConfig(),
        )

        with TestClient(server.app) as client:
            if hasattr(server.app.state, "hook_manager"):
                del server.app.state.hook_manager
            response = client.post(
                "/api/hooks/execute",
                json=_hook_envelope(hook_type="session-start", source="claude"),
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
        mock_hook_manager = _mock_hook_manager()
        server.app.state.hook_manager = mock_hook_manager

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.adapters.claude_code.ClaudeCodeAdapter.handle_native",
                return_value={"continue": True},
            ) as mock_handle_native,
        ):
            response = client.post(
                "/api/hooks/execute",
                json=_hook_envelope(hook_type="session-start", source="claude"),
            )

        assert response.status_code == 200
        assert response.json()["continue"] is True
        mock_handle_native.assert_called_once()

    def test_execute_hook_claude_envelope_source(self, session_storage: SessionManager) -> None:
        """Envelope-shaped Claude requests should normalize to the flat adapter payload."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = _mock_hook_manager()
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
                json=_hook_envelope(
                    hook_type="session-start",
                    source="claude",
                    input_data={"session_id": "claude-envelope"},
                    headers={"X-Gobby-Session-Id": "embedded-session"},
                ),
            )

        assert response.status_code == 200
        assert response.json()["continue"] is True
        assert mock_adapter.handle_native.call_args.args[0] == {
            "hook_type": "session-start",
            "source": "claude",
            "input_data": {"session_id": "claude-envelope"},
        }

    def test_execute_hook_rejects_unsupported_source(
        self,
        session_storage: SessionManager,
    ) -> None:
        """Unsupported hook sources are rejected."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = _mock_hook_manager()
        server.app.state.hook_manager = mock_hook_manager

        with TestClient(server.app) as client:
            response = client.post(
                "/api/hooks/execute",
                json=_hook_envelope(hook_type="session-start", source="unsupported"),
            )

        assert response.status_code == 400
        assert "Unsupported source: unsupported" in response.json()["detail"]

    def test_execute_hook_droid_source(self, session_storage: SessionManager) -> None:
        """Test execute hook with Droid source."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = _mock_hook_manager()
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
                json=_hook_envelope(
                    hook_type="PreToolUse",
                    source="droid",
                    input_data={"session_id": "droid-123", "cwd": "/tmp"},
                ),
            )

        assert response.status_code == 200
        assert response.json()["continue"] is True
        MockAdapter.assert_called_once_with(hook_manager=mock_hook_manager)
        assert mock_adapter.handle_native.call_args.args[0] == {
            "hook_type": "PreToolUse",
            "source": "droid",
            "input_data": {"session_id": "droid-123", "cwd": "/tmp"},
        }

    @pytest.mark.parametrize(
        ("hook_type", "hook_response", "expected"),
        [
            (
                "PreToolUse",
                HookResponse(decision="block", reason="tool policy"),
                {
                    "continue": True,
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "tool policy",
                    },
                },
            ),
            (
                "PermissionRequest",
                HookResponse(decision="block", reason="permission policy"),
                {
                    "continue": True,
                    "hookSpecificOutput": {
                        "hookEventName": "PermissionRequest",
                        "decision": {
                            "behavior": "deny",
                            "message": "permission policy",
                            "interrupt": True,
                        },
                    },
                },
            ),
            (
                "Stop",
                HookResponse(decision="block", reason="finish the task"),
                {"continue": True, "decision": "block", "reason": "finish the task"},
            ),
            (
                "TodoCompleted",
                HookResponse(decision="block", reason="dependency incomplete"),
                {
                    "continue": True,
                    "decision": "block",
                    "reason": "dependency incomplete",
                },
            ),
        ],
    )
    def test_execute_hook_qwen_returns_exact_native_response_shapes(
        self,
        session_storage: SessionManager,
        hook_type: str,
        hook_response: HookResponse,
        expected: dict[str, Any],
    ) -> None:
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        hook_manager = _mock_hook_manager()
        hook_manager.handle.return_value = hook_response
        server.app.state.hook_manager = hook_manager
        adapter = QwenAdapter(hook_manager=hook_manager)

        with (
            patch(
                "gobby.adapters.qwen.QwenAdapter",
                return_value=adapter,
            ) as adapter_constructor,
            TestClient(server.app) as client,
        ):
            response = client.post(
                "/api/hooks/execute",
                json=_hook_envelope(
                    hook_type=hook_type,
                    source="qwen",
                    input_data={
                        "session_id": "qwen-native-shape",
                        "phase": "validation",
                    },
                ),
            )

        assert response.status_code == 200
        assert response.json() == expected
        adapter_constructor.assert_called_once_with(hook_manager=hook_manager)

    def test_execute_hook_qwen_stop_evaluation_failure_returns_structured_block(
        self,
        session_storage: SessionManager,
    ) -> None:
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        hook_manager = _mock_hook_manager()
        hook_manager.handle.side_effect = RuntimeError("rule engine unavailable")
        server.app.state.hook_manager = hook_manager
        adapter = QwenAdapter(hook_manager=hook_manager)

        with (
            patch(
                "gobby.adapters.qwen.QwenAdapter",
                return_value=adapter,
            ) as adapter_constructor,
            TestClient(server.app) as client,
        ):
            response = client.post(
                "/api/hooks/execute",
                json=_hook_envelope(
                    hook_type="Stop",
                    source="qwen",
                    critical=True,
                    input_data={"session_id": "qwen-stop-failure"},
                ),
            )

        assert response.status_code == 200
        assert response.json()["continue"] is True
        assert response.json()["decision"] == "block"
        assert "blocking this critical hook for safety" in response.json()["reason"]
        adapter_constructor.assert_called_once_with(hook_manager=hook_manager)

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
        mock_hook_manager = _mock_hook_manager()
        mock_hook_manager.handle.side_effect = RuntimeError("droid adapter failed")
        server.app.state.hook_manager = mock_hook_manager

        with TestClient(server.app) as client:
            response = client.post(
                "/api/hooks/execute",
                json=_hook_envelope(
                    hook_type="PreToolUse",
                    source="droid",
                    input_data={"session_id": "droid-123", "tool_name": "Read"},
                ),
            )

        assert response.status_code == 200
        data = response.json()
        assert data["continue"] is True
        assert "droid adapter failed" in data["systemMessage"]
        assert "hookSpecificOutput" not in data

    def test_execute_hook_non_critical_timeout_returns_graceful_response(
        self,
        session_storage: SessionManager,
    ) -> None:
        """A non-critical adapter timeout returns a graceful response."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            config=DaemonConfig(hooks={"adapter_timeout": 91}),
            session_manager=session_storage,
        )
        server.app.state.hook_manager = _mock_hook_manager()

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new_callable=AsyncMock,
                side_effect=TimeoutError,
            ) as run_adapter_hook,
        ):
            response = client.post(
                "/api/hooks/execute",
                json=_hook_envelope(
                    hook_type="PreToolUse",
                    source="droid",
                    input_data={"session_id": "droid-123", "tool_name": "Read"},
                ),
            )
            run_adapter_hook.assert_awaited_once()
            assert run_adapter_hook.await_args.kwargs["timeout_seconds"] == 91

        assert response.status_code == 200
        data = response.json()
        assert data["continue"] is True
        assert "timed out after 91s" in data["systemMessage"]

    def test_stalled_workflow_dependencies_do_not_starve_control_plane(
        self,
        session_storage: SessionManager,
    ) -> None:
        server = create_http_server(
            port=60887,
            test_mode=True,
            config=DaemonConfig(),
            session_manager=session_storage,
        )
        server.app.state.hook_manager = _mock_hook_manager()
        server.config.hooks.adapter_timeout = 0.5
        runtime = WorkflowEvaluationRuntime(max_workers=2)
        handler = WorkflowHookHandler(timeout=0.05, evaluation_runtime=runtime)
        started_count = 0
        started_lock = threading.Lock()
        all_started = threading.Event()
        release = threading.Event()

        def stalled_dependency() -> None:
            nonlocal started_count
            with started_lock:
                started_count += 1
                if started_count == 2:
                    all_started.set()
            release.wait(timeout=1)

        async def evaluate(
            _event: HookEvent,
            *,
            blocking_deadline: float | None = None,
        ) -> HookResponse:
            del blocking_deadline
            await asyncio.to_thread(stalled_dependency)
            return HookResponse(decision="allow")

        cast(Any, handler)._evaluate_rules = evaluate

        def run_workflow(payload: dict[str, Any], _manager: Any) -> dict[str, bool]:
            session_id = str(payload.get("session_id") or "workflow-stall")
            event = HookEvent(
                event_type=HookEventType.BEFORE_TOOL,
                session_id=session_id,
                source=SessionSource.DROID,
                timestamp=datetime.now(UTC),
                data=payload,
                metadata={"_platform_session_id": session_id},
            )
            handler.evaluate(event)
            return {"continue": True}

        try:
            with (
                TestClient(server.app) as client,
                patch(
                    "gobby.adapters.droid.DroidAdapter.handle_native",
                    side_effect=run_workflow,
                ),
                concurrent.futures.ThreadPoolExecutor(max_workers=2) as requests,
            ):
                hook_futures = [
                    requests.submit(
                        client.post,
                        "/api/hooks/execute",
                        json=_hook_envelope(
                            hook_type=hook_type,
                            source="droid",
                            input_data={
                                "session_id": f"stalled-{index}",
                                "tool_name": "Read",
                            },
                        ),
                    )
                    for index, hook_type in enumerate(("PreToolUse", "UserPromptSubmit"))
                ]
                assert all_started.wait(timeout=0.5)

                control_started = time.perf_counter()
                health_response = client.get("/api/health")
                mcp_response = client.get("/api/mcp/status")
                control_elapsed = time.perf_counter() - control_started

                hook_responses = [future.result(timeout=1) for future in hook_futures]

            assert health_response.status_code == 200
            assert mcp_response.status_code == 200
            assert control_elapsed < 0.5
            assert all(response.status_code == 200 for response in hook_responses)
            assert all(response.json()["continue"] is True for response in hook_responses)
        finally:
            release.set()
            handler.shutdown()

    @pytest.mark.asyncio
    async def test_adapter_executor_bounds_and_releases_hung_evaluation_workers(self) -> None:
        from gobby.servers.routes.mcp import hooks as hook_routes

        worker_limit = hook_routes.HOOK_ADAPTER_MAX_WORKERS
        active_workers = 0
        peak_workers = 0
        worker_lock = threading.Lock()
        never_completed: concurrent.futures.Future[None] = concurrent.futures.Future()

        def bounded_evaluation_wait(*_args: Any, **_kwargs: Any) -> dict[str, bool]:
            nonlocal active_workers, peak_workers
            with worker_lock:
                active_workers += 1
                peak_workers = max(peak_workers, active_workers)
            try:
                never_completed.result(timeout=0.03)
            finally:
                with worker_lock:
                    active_workers -= 1
            return {"continue": True}

        adapter = MagicMock()
        adapter.handle_native.side_effect = bounded_evaluation_wait
        results = await asyncio.gather(
            *(
                hook_routes._run_adapter_hook(
                    adapter,
                    {},
                    MagicMock(),
                    timeout_seconds=0.5,
                )
                for _ in range(worker_limit + 3)
            ),
            return_exceptions=True,
        )

        assert all(isinstance(result, TimeoutError) for result in results)
        assert peak_workers <= worker_limit
        assert active_workers == 0
        assert await asyncio.wait_for(asyncio.to_thread(lambda: True), timeout=0.2)

    @pytest.mark.parametrize(
        ("source", "hook_type", "critical", "adapter_patch"),
        [
            (
                "codex",
                "Stop",
                False,
                "gobby.adapters.codex_impl.hooks_adapter.CodexHooksAdapter",
            ),
            ("claude", "session-start", True, "gobby.adapters.claude_code.ClaudeCodeAdapter"),
        ],
    )
    def test_execute_hook_fail_safe_timeout_blocks(
        self,
        session_storage: SessionManager,
        source: str,
        hook_type: str,
        critical: bool,
        adapter_patch: str,
    ) -> None:
        """Stop and CLI-critical hook timeouts return provider-native blocks."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server.app.state.hook_manager = _mock_hook_manager()

        timeout_error = WorkflowEvaluationTimeout(
            event_type=hook_type,
            session_id="test-stop",
            timeout_seconds=15,
        )
        timeout_error.queue_duration_seconds = 0.125
        timeout_error.execution_duration_seconds = 15.0
        timeout_mock = AsyncMock(side_effect=timeout_error)
        with (
            TestClient(server.app) as client,
            patch(adapter_patch) as MockAdapter,
            patch("gobby.servers.routes.mcp.hooks._run_adapter_hook", new=timeout_mock),
            patch("gobby.servers.routes.mcp.hooks.logger") as mock_logger,
        ):
            mock_adapter = MagicMock()
            mock_adapter.translate_from_hook_response.return_value = {
                "continue": False,
                "decision": "block",
                "reason": "timed out",
            }
            MockAdapter.return_value = mock_adapter

            response = client.post(
                "/api/hooks/execute",
                json=_hook_envelope(
                    hook_type=hook_type,
                    source=source,
                    critical=critical,
                    input_data={"session_id": "test-stop"},
                ),
            )

        assert response.status_code == 200
        assert response.json() == {
            "continue": False,
            "decision": "block",
            "reason": "timed out",
        }
        assert timeout_mock.await_args.kwargs["timeout_seconds"] == 105
        mock_adapter.translate_from_hook_response.assert_called_once()
        hook_response = mock_adapter.translate_from_hook_response.call_args.args[0]
        assert hook_response.decision == "block"
        assert hook_response.reason == (
            "Gobby hook evaluation timed out after 105s; blocking this critical hook for safety. "
            "Try again after the daemon recovers."
        )
        assert hook_response.system_message is None
        timeout_log = next(
            call
            for call in mock_logger.error.call_args_list
            if call.args and call.args[0] == "Critical hook timed out: %s"
        )
        assert timeout_log.kwargs["extra"]["exception_type"] == "WorkflowEvaluationTimeout"
        expected_timeout_fields = {
            "exception_type": "WorkflowEvaluationTimeout",
            "evaluation_event": hook_type,
            "evaluation_session_id": "test-stop",
            "evaluation_timeout_seconds": 15,
            "adapter_queue_duration_seconds": 0.125,
            "adapter_execution_duration_seconds": 15.0,
        }
        assert {
            key: timeout_log.kwargs["extra"][key] for key in expected_timeout_fields
        } == expected_timeout_fields

    @pytest.mark.parametrize("handler_layer", ["value_error", "exception", "outer"])
    @pytest.mark.parametrize(
        ("source", "hook_type", "critical", "should_block"),
        [
            ("codex", "Stop", False, True),
            ("claude", "session-start", True, True),
            ("droid", "PreToolUse", False, False),
        ],
    )
    def test_execute_hook_exception_posture_matches_fail_safe_contract(
        self,
        session_storage: SessionManager,
        handler_layer: str,
        source: str,
        hook_type: str,
        critical: bool,
        should_block: bool,
    ) -> None:
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server.app.state.hook_manager = _mock_hook_manager()
        error: Exception = (
            ValueError("invalid hook state")
            if handler_layer == "value_error"
            else RuntimeError("hook pipeline unavailable")
        )
        failure_patch: Any
        if handler_layer == "outer":
            failure_patch = patch(
                "gobby.servers.routes.mcp.hooks.claim_envelope_processing",
                side_effect=error,
            )
            headers = {"X-Gobby-Envelope-Id": "outer-handler-error"}
        else:
            failure_patch = patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new=AsyncMock(side_effect=error),
            )
            headers = {}

        with (
            TestClient(server.app) as client,
            failure_patch,
            patch("gobby.servers.routes.mcp.hooks.mark_envelope_processed"),
        ):
            response = client.post(
                "/api/hooks/execute",
                headers=headers,
                json=_hook_envelope(
                    hook_type=hook_type,
                    source=source,
                    critical=critical,
                    input_data={"session_id": "error-posture"},
                ),
            )

        assert response.status_code == 200
        data = response.json()
        assert data["continue"] is not should_block
        if should_block:
            reason = data.get("reason") or data.get("stopReason")
            assert "blocking this critical hook for safety" in reason
        else:
            assert "non-fatal" in data["systemMessage"]

    @pytest.mark.parametrize("supplied_run_id", [None, "not-a-uuid"])
    def test_execute_hook_acks_ambiguous_managed_terminal_identity(
        self,
        session_storage: SessionManager,
        supplied_run_id: str | None,
    ) -> None:
        platform_session_id = "d92fc5be-6638-415d-8143-c349293fb35c"
        expected_run_id = "3fbc517c-9e1c-4ea3-9a2f-f21b2035c764"
        fence_sessions = MagicMock()
        fence_sessions.get.return_value = MagicMock(agent_run_id=expected_run_id)
        fence_runs = MagicMock()
        hook_manager = _mock_hook_manager()
        ingress_results: list[Any] = []

        def handle(event: HookEvent) -> HookResponse:
            ingress_results.append(
                validate_managed_agent_hook(
                    event,
                    session_manager=fence_sessions,
                    agent_run_manager=fence_runs,
                    database=MagicMock(),
                    completion_registry=None,
                    registry_loop=None,
                )
            )
            return HookResponse(decision="allow")

        hook_manager.handle.side_effect = handle
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server.app.state.hook_manager = hook_manager
        input_data: dict[str, Any] = {"session_id": "external-child"}
        if supplied_run_id is not None:
            input_data["terminal_context"] = {"gobby_agent_run_id": supplied_run_id}

        with TestClient(server.app) as client:
            response = client.post(
                "/api/hooks/execute",
                headers={"X-Gobby-Session-Id": platform_session_id},
                json=_hook_envelope(
                    hook_type="Stop",
                    source="codex",
                    critical=True,
                    input_data=input_data,
                ),
            )

        assert response.status_code == 200
        assert response.json() == {"continue": True}
        assert len(ingress_results) == 1
        assert ingress_results[0].accepted is False
        assert ingress_results[0].ambiguous is True
        fence_runs.get.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_legacy_identity_less_envelope_is_removed_and_barrier_settles(
        self,
        session_storage: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
        inbox_dir = tmp_path / "hooks" / "inbox"
        inbox_dir.mkdir(parents=True)
        envelope_id = "n-0000000000001-legacy"
        envelope_path = inbox_dir / f"{envelope_id}.json"
        platform_session_id = "d92fc5be-6638-415d-8143-c349293fb35c"
        expected_run_id = "3fbc517c-9e1c-4ea3-9a2f-f21b2035c764"
        envelope_path.write_text(
            json.dumps(
                {
                    **_hook_envelope(
                        hook_type="Stop",
                        source="codex",
                        critical=True,
                        input_data={"session_id": "external-child"},
                    ),
                    "headers": {"X-Gobby-Session-Id": platform_session_id},
                }
            ),
            encoding="utf-8",
        )

        fence_sessions = MagicMock()
        fence_sessions.get.return_value = MagicMock(agent_run_id=expected_run_id)
        fence_runs = MagicMock()
        hook_manager = _mock_hook_manager()

        def handle(event: HookEvent) -> HookResponse:
            result = validate_managed_agent_hook(
                event,
                session_manager=fence_sessions,
                agent_run_manager=fence_runs,
                database=MagicMock(),
                completion_registry=None,
                registry_loop=None,
            )
            assert result.ambiguous is True
            return HookResponse(decision="allow")

        hook_manager.handle.side_effect = handle
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server.app.state.hook_manager = hook_manager

        token = "test-token"
        (tmp_path / "local_cli_token").write_text(token, encoding="utf-8")
        AuthStore(session_storage.db).set_local_api_token_hash(hash_token(token))
        first = await drain_hook_inbox_barrier(
            server.app,
            inbox_dir,
            timeout_seconds=1.0,
        )
        second = await drain_hook_inbox_barrier(
            server.app,
            inbox_dir,
            timeout_seconds=1.0,
        )

        assert first.replayed == 1
        assert first.timed_out is False
        assert second.replayed == 0
        assert second.timed_out is False
        assert envelope_path.exists() is False
        assert is_envelope_processed(envelope_id, processed_dir=inbox_dir / "processed")
        assert hook_manager.handle.call_count == 1
        fence_runs.get.assert_not_called()

    @pytest.mark.parametrize(
        ("error_kind", "envelope_id", "reason"),
        [
            (
                "run_identity",
                "retryable-run-identity",
                "agent_run_identity_pending",
            ),
            (
                "daemon_not_ready",
                "retryable-daemon-not-ready",
                "daemon_not_ready",
            ),
        ],
    )
    def test_execute_hook_releases_claim_for_retryable_error(
        self,
        session_storage: SessionManager,
        error_kind: str,
        envelope_id: str,
        reason: str,
    ) -> None:
        from gobby.hooks.agent_run_ingress import AgentRunIngressRetryableError
        from gobby.hooks.health_gate import DaemonNotReadyError

        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server.app.state.hook_manager = _mock_hook_manager()
        retryable = (
            AgentRunIngressRetryableError(
                session_id="child-1",
                expected_run_id="run-1",
                reason="run is not durable yet",
            )
            if error_kind == "run_identity"
            else DaemonNotReadyError(
                daemon_status="not_running",
                reason="Connection refused",
            )
        )

        with (
            TestClient(server.app) as client,
            patch(
                "gobby.servers.routes.mcp.hooks._run_adapter_hook",
                new=AsyncMock(side_effect=retryable),
            ),
            patch(
                "gobby.servers.routes.mcp.hooks.release_envelope_processing_claim",
                return_value=True,
            ) as release,
            patch(
                "gobby.servers.routes.mcp.hooks.mark_envelope_processed",
            ) as mark_processed,
        ):
            response = client.post(
                "/api/hooks/execute",
                headers={"X-Gobby-Envelope-Id": envelope_id},
                json=_hook_envelope(
                    hook_type="SessionStart",
                    source="codex",
                    critical=True,
                    input_data={"session_id": "child-1"},
                ),
            )

        assert response.status_code == 503
        assert response.json() == {
            "status": "retry",
            "reason": reason,
        }
        release.assert_called_once_with(envelope_id)
        mark_processed.assert_not_called()

    @pytest.mark.parametrize(
        ("source", "hook_type", "adapter_patch"),
        [
            ("claude", "pre-tool-use", "gobby.adapters.claude_code.ClaudeCodeAdapter"),
            ("qwen", "PreToolUse", "gobby.adapters.qwen.QwenAdapter"),
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
        mock_hook_manager = _mock_hook_manager()
        server.app.state.hook_manager = mock_hook_manager

        with (
            TestClient(server.app) as client,
            patch(adapter_patch) as MockAdapter,
            patch(
                "gobby.servers.routes.mcp.hook_hold_open._maybe_hold_open",
                new_callable=AsyncMock,
            ) as mock_hold_open,
        ):
            mock_adapter = MagicMock()
            mock_adapter.handle_native.return_value = {"continue": True}
            MockAdapter.return_value = mock_adapter
            mock_hold_open.return_value = {"decision": "approve"}

            response = client.post(
                "/api/hooks/execute",
                headers={"X-Gobby-Session-Id": WEB_SESSION_ID},
                json=_hook_envelope(
                    hook_type=hook_type,
                    source=source,
                    input_data={"tool_name": "Bash", "arguments": {"command": "pwd"}},
                ),
            )

        assert response.status_code == 200
        assert response.json() == {"decision": "approve"}
        mock_hold_open.assert_awaited_once()
        await_args = mock_hold_open.await_args
        assert await_args is not None
        args = await_args.args
        assert args[1] == WEB_SESSION_ID
        assert args[2] == "PreToolUse"
        assert args[4] == source
        assert await_args.kwargs["server"] is server

    def test_execute_hook_pre_tool_use_returns_adapter_response_without_app_server_state(
        self,
        session_storage: SessionManager,
    ) -> None:
        """Missing app.state.server must not turn hold-open fallback into a hook error."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = _mock_hook_manager()
        server.app.state.hook_manager = mock_hook_manager

        with (
            TestClient(server.app) as client,
            patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as MockAdapter,
        ):
            mock_adapter = MagicMock()
            mock_adapter.handle_native.return_value = {"continue": True, "adapter": "ok"}
            MockAdapter.return_value = mock_adapter

            app_state_server = server.app.state.server
            delattr(server.app.state, "server")
            try:
                response = client.post(
                    "/api/hooks/execute",
                    headers={"X-Gobby-Session-Id": WEB_SESSION_ID},
                    json=_hook_envelope(
                        hook_type="pre-tool-use",
                        source="claude",
                        input_data={
                            "tool_name": "Bash",
                            "arguments": {"command": "pwd"},
                        },
                    ),
                )
            finally:
                server.app.state.server = app_state_server

        assert response.status_code == 200
        assert response.json() == {"continue": True, "adapter": "ok"}

    def test_execute_hook_codex_source(self, session_storage: SessionManager) -> None:
        """Test execute hook with Codex source uses CodexHooksAdapter."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = _mock_hook_manager()
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
                json=_hook_envelope(
                    hook_type="SessionStart",
                    source="codex",
                    input_data={"session_id": "test-123", "cwd": "/tmp"},
                ),
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
        mock_hook_manager = _mock_hook_manager()
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
                json=_hook_envelope(
                    hook_type="SessionStart",
                    source="codex",
                    input_data={"session_id": "test-123", "cwd": "/"},
                ),
            )

        assert response.status_code == 200
        assert response.json()["continue"] is True
        warning.assert_not_called()
        debug.assert_called()

    def test_execute_hook_codex_pre_compact_error_is_graceful(
        self, session_storage: SessionManager
    ) -> None:
        """Codex compact hook errors should return Codex-valid non-fatal output."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = _mock_hook_manager()
        mock_hook_manager.handle.side_effect = RuntimeError("compact failed")
        server.app.state.hook_manager = mock_hook_manager

        with TestClient(server.app) as client:
            response = client.post(
                "/api/hooks/execute",
                json=_hook_envelope(
                    hook_type="PreCompact",
                    source="codex",
                    input_data={"session_id": "test-compact", "cwd": "/tmp"},
                ),
            )

        assert response.status_code == 200
        data = response.json()
        assert data["continue"] is True
        assert "compact failed" in data["systemMessage"]
        assert "hookSpecificOutput" not in data
        assert "decision" not in data
        assert "reason" not in data
        assert "stopReason" not in data
        mock_hook_manager.handle.assert_called_once()

    def test_execute_hook_codex_envelope_source(self, session_storage: SessionManager) -> None:
        """Envelope-shaped Codex requests should normalize before adapter dispatch."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = _mock_hook_manager()
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
                json=_hook_envelope(
                    critical=True,
                    hook_type="SessionStart",
                    source="codex",
                    input_data={"session_id": "test-envelope", "cwd": "/tmp"},
                    headers={"X-Gobby-Session-Id": "embedded-codex"},
                ),
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
        server.app.state.hook_manager = _mock_hook_manager()

        with TestClient(server.app) as client:
            response = client.post(
                "/api/hooks/execute",
                json=_hook_envelope(
                    schema_version=99,
                    hook_type="session-start",
                    source="claude",
                ),
            )

        assert response.status_code == 400
        assert response.json()["detail"] == (
            f"Unsupported schema_version: 99. Supported: {SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION}"
        )

    def test_execute_hook_envelope_requires_source(self, session_storage: SessionManager) -> None:
        """Envelope requests still require source after normalization."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server.app.state.hook_manager = _mock_hook_manager()

        with TestClient(server.app) as client:
            response = client.post(
                "/api/hooks/execute",
                json=_hook_envelope(hook_type="session-start"),
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
        server.app.state.hook_manager = _mock_hook_manager()

        with TestClient(server.app) as client:
            response = client.post(
                "/api/hooks/execute",
                json=_hook_envelope(source="claude"),
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
        mock_hook_manager = _mock_hook_manager()
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
        mock_hook_manager = _mock_hook_manager()
        server.app.state.hook_manager = mock_hook_manager

        with (
            TestClient(server.app) as client,
            patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as MockAdapter,
            patch(
                "gobby.servers.routes.mcp.hook_hold_open._maybe_hold_open",
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
                json=_hook_envelope(
                    hook_type="pre-tool-use",
                    source="claude",
                    input_data={"tool_name": "Bash", "arguments": {"command": "pwd"}},
                    headers={"X-Gobby-Session-Id": "embedded-session"},
                ),
            )

        assert response.status_code == 200
        assert response.json() == {"decision": "approve"}
        mock_hold_open.assert_awaited_once()
        await_args = mock_hold_open.await_args
        assert await_args is not None
        assert await_args.args[1] == "real-session"

    def test_execute_hook_logs_enqueued_at_for_envelope_requests(
        self, session_storage: SessionManager
    ) -> None:
        """Envelope metadata should be surfaced in structured route logging."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        mock_hook_manager = _mock_hook_manager()
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
                json=_hook_envelope(
                    enqueued_at="2026-04-16T12:34:56Z",
                    hook_type="session-start",
                    source="claude",
                ),
            )

        assert response.status_code == 200
        matching_logs = [
            call
            for call in mock_logger.debug.call_args_list
            if call.args[:2] == ("Hook executed: %s", "session-start")
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
        mock_hook_manager = _mock_hook_manager()
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
                json=_hook_envelope(
                    hook_type="SessionStart",
                    source="codex",
                    input_data={"session_id": "test-456", "cwd": "/tmp"},
                ),
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
        mock_hook_manager = _mock_hook_manager()
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
                json=_hook_envelope(
                    hook_type="Stop",
                    source="codex",
                    input_data={"session_id": "test-stop"},
                ),
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
        for hook in (
            "SessionStart",
            "SubagentStart",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "SubagentStop",
            "Stop",
        ):
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
            config=None,
        )

    @pytest.fixture
    def webhooks_client(self, webhooks_server: HTTPServer) -> Iterator[TestClient]:
        """Create test client for webhooks endpoints."""
        with patch("gobby.servers.app_factory.HookManager") as mock_hook_manager:
            mock_hook_manager.return_value.shutdown_async = AsyncMock()
            with TestClient(webhooks_server.app) as client:
                yield client

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

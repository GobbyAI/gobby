"""Tests for the MCP Client Manager."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from gobby.mcp_proxy.manager import (
    ConnectionState,
    HealthState,
    MCPClientManager,
    MCPConnectionHealth,
    MCPError,
    MCPServerConfig,
    _create_transport_connection,
)
from gobby.mcp_proxy.transport_types import SUPPORTED_TRANSPORTS
from gobby.storage.mcp_models import MCPServer

pytestmark = pytest.mark.unit


class TestMCPServerConfig:
    """Tests for MCPServerConfig dataclass."""

    def test_http_config_valid(self) -> None:
        """Test valid HTTP config."""
        config = MCPServerConfig(
            name="test-server",
            transport="http",
            url="http://localhost:8080/mcp",
            enabled=True,
            project_id="test-project-uuid",
        )

        result = config.validate()
        assert result is None
        assert config.name == "test-server"
        assert config.transport == "http"
        assert config.url == "http://localhost:8080/mcp"

    def test_http_config_missing_url_raises(self) -> None:
        """Test HTTP config without URL raises error."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project-uuid",
            transport="http",
            url=None,
        )

        with pytest.raises(ValueError, match="http transport requires 'url' parameter"):
            config.validate()

    def test_stdio_config_valid(self) -> None:
        """Test valid stdio config."""
        config = MCPServerConfig(
            name="stdio-server",
            project_id="test-project-uuid",
            transport="stdio",
            command="npx",
            args=["-y", "@test/server"],
            env={"DEBUG": "true"},
        )

        result = config.validate()
        assert result is None
        assert config.command == "npx"
        assert config.args == ["-y", "@test/server"]
        assert config.env == {"DEBUG": "true"}

    def test_stdio_config_missing_command_raises(self) -> None:
        """Test stdio config without command raises error."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project-uuid",
            transport="stdio",
            command=None,
        )

        with pytest.raises(ValueError, match="stdio transport requires 'command' parameter"):
            config.validate()

    def test_websocket_config_valid(self) -> None:
        """Test valid WebSocket config."""
        config = MCPServerConfig(
            name="ws-server",
            project_id="test-project-uuid",
            transport="websocket",
            url="ws://localhost:8080/mcp",
        )

        result = config.validate()
        assert result is None
        assert config.url == "ws://localhost:8080/mcp"

    def test_unsupported_transport_raises(self) -> None:
        """Test unsupported transport raises error."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project-uuid",
            transport="invalid",
        )

        with pytest.raises(ValueError, match="Unsupported transport"):
            config.validate()

    def test_http_config_with_headers(self) -> None:
        """Test HTTP config with custom headers."""
        config = MCPServerConfig(
            name="api-server",
            project_id="test-project-uuid",
            transport="http",
            url="https://api.example.com/mcp",
            headers={"Authorization": "Bearer token123", "X-API-Key": "secret"},
        )

        config.validate()
        assert config.headers == {"Authorization": "Bearer token123", "X-API-Key": "secret"}

    def test_connect_timeout_default(self) -> None:
        """Test connect_timeout has default value of 30.0."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project-uuid",
            transport="http",
            url="http://localhost:8080/mcp",
        )

        assert config.connect_timeout == 30.0
        config.validate()

    def test_connect_timeout_custom(self) -> None:
        """Test connect_timeout can be customized."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project-uuid",
            transport="http",
            url="http://localhost:8080/mcp",
            connect_timeout=60.0,
        )

        assert config.connect_timeout == 60.0
        config.validate()

    def test_connect_timeout_zero_raises(self) -> None:
        """Test connect_timeout of zero raises error."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project-uuid",
            transport="http",
            url="http://localhost:8080/mcp",
            connect_timeout=0,
        )

        with pytest.raises(ValueError, match="connect_timeout must be a positive number"):
            config.validate()

    def test_connect_timeout_negative_raises(self) -> None:
        """Test negative connect_timeout raises error."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project-uuid",
            transport="http",
            url="http://localhost:8080/mcp",
            connect_timeout=-5.0,
        )

        with pytest.raises(ValueError, match="connect_timeout must be a positive number"):
            config.validate()

    def test_validate_rejects_empty_id(self) -> None:
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project-uuid",
            transport="http",
            url="http://localhost:8080/mcp",
            id="",
        )

        with pytest.raises(ValueError, match="id must be a non-empty string"):
            config.validate()

        config.id = "   "
        with pytest.raises(ValueError, match="id must be a non-empty string"):
            config.validate()

    def test_new_config_has_id_and_template_defaults(self) -> None:
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project-uuid",
            transport="http",
            url="http://localhost:8080/mcp",
        )

        assert config.id
        assert config.template_id is None
        assert config.template is None
        assert config.runtime_hook is None
        assert config.template_values is None
        config.validate()


class TestMCPConnectionHealth:
    """Tests for MCPConnectionHealth tracking."""

    def test_initial_state(self) -> None:
        """Test initial health state."""
        health = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        assert health.health == HealthState.HEALTHY
        assert health.consecutive_failures == 0
        assert health.last_error is None

    def test_record_success(self) -> None:
        """Test recording successful operation."""
        health = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
            consecutive_failures=3,
            health=HealthState.DEGRADED,
        )

        health.record_success(response_time_ms=50.0)

        assert health.consecutive_failures == 0
        assert health.last_error is None
        assert health.health == HealthState.HEALTHY
        assert health.response_time_ms == 50.0
        assert health.last_health_check is not None

    def test_record_failure_degraded(self) -> None:
        """Test health becomes degraded after 3 failures."""
        health = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        # Record 3 failures
        for i in range(3):
            health.record_failure(f"Error {i + 1}")

        assert health.consecutive_failures == 3
        assert health.health == HealthState.DEGRADED
        assert health.last_error == "Error 3"

    def test_record_failure_unhealthy(self) -> None:
        """Test health becomes unhealthy after 5 failures."""
        health = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        # Record 5 failures
        for i in range(5):
            health.record_failure(f"Error {i + 1}")

        assert health.consecutive_failures == 5
        assert health.health == HealthState.UNHEALTHY


class TestCreateTransportConnection:
    """Tests for transport connection factory."""

    def test_create_http_connection(self) -> None:
        """Test creating HTTP transport connection."""
        config = MCPServerConfig(
            name="http-server",
            project_id="test-project-uuid",
            transport="http",
            url="http://localhost:8080/mcp",
        )

        connection = _create_transport_connection(config, stdio_errlog_path="/tmp/mcp-client.log")

        assert connection.config == config
        assert connection.state == ConnectionState.DISCONNECTED
        assert not hasattr(connection, "_stdio_errlog_path")

    def test_create_stdio_connection(self) -> None:
        """Test creating stdio transport connection."""
        config = MCPServerConfig(
            name="stdio-server",
            project_id="test-project-uuid",
            transport="stdio",
            command="npx",
            args=["-y", "@test/server"],
        )

        connection = _create_transport_connection(config, stdio_errlog_path="/tmp/mcp-client.log")

        assert connection.config == config
        assert connection.state == ConnectionState.DISCONNECTED
        assert connection._stdio_errlog_path == "/tmp/mcp-client.log"

    def test_create_websocket_connection(self) -> None:
        """Test creating WebSocket transport connection."""
        config = MCPServerConfig(
            name="ws-server",
            project_id="test-project-uuid",
            transport="websocket",
            url="ws://localhost:8080/mcp",
        )

        connection = _create_transport_connection(config, stdio_errlog_path="/tmp/mcp-client.log")

        assert connection.config == config
        assert connection.state == ConnectionState.DISCONNECTED
        assert not hasattr(connection, "_stdio_errlog_path")

    def test_create_sse_connection(self) -> None:
        """Test creating SSE transport connection."""
        config = MCPServerConfig(
            name="sse-server",
            project_id="test-project-uuid",
            transport="sse",
            url="https://localhost:8080/sse",
        )

        config.validate()
        connection = _create_transport_connection(config)

        assert connection.config == config
        assert connection.state == ConnectionState.DISCONNECTED

    @pytest.mark.parametrize(
        "transport",
        SUPPORTED_TRANSPORTS,
    )
    def test_factory_covers_every_valid_transport(
        self,
        transport: str,
    ) -> None:
        """Every transport accepted by config validation has a factory implementation."""
        options: dict[str, Any]
        if transport == "stdio":
            options = {"command": "server"}
        elif transport == "websocket":
            options = {"url": "wss://localhost/mcp"}
        else:
            options = {"url": "https://localhost/mcp"}

        config = MCPServerConfig(
            name=f"{transport}-server",
            project_id="test-project-uuid",
            transport=transport,
            **options,
        )

        config.validate()

        assert _create_transport_connection(config).config is config

    def test_create_unsupported_transport_raises(self) -> None:
        """Test unsupported transport raises error."""
        config = MCPServerConfig(
            name="invalid-server",
            project_id="test-project-uuid",
            transport="invalid",
        )

        with pytest.raises(ValueError, match="Unsupported transport"):
            _create_transport_connection(config)


class TestMCPClientManagerInit:
    """Tests for MCPClientManager initialization."""

    def test_init_with_configs(self) -> None:
        """Test initialization with server configs."""
        configs = [
            MCPServerConfig(
                name="server1",
                project_id="test-project-uuid",
                transport="http",
                url="http://localhost:8001",
            ),
            MCPServerConfig(
                name="server2",
                project_id="test-project-uuid",
                transport="http",
                url="http://localhost:8002",
            ),
        ]

        manager = MCPClientManager(server_configs=configs)

        assert len(manager.server_configs) == 2
        assert manager.connections == {}
        assert set(manager._configs) == {item.id for item in configs}
        assert set(manager.health) == {item.id for item in configs}

    def test_init_empty_configs(self) -> None:
        """Test initialization with empty configs."""
        manager = MCPClientManager(server_configs=[])

        assert manager.server_configs == []
        assert manager.connections == {}

    def test_init_with_project_context(self) -> None:
        """Test initialization with project context."""
        configs = [
            MCPServerConfig(
                name="server1",
                project_id="test-project-uuid",
                transport="http",
                url="http://localhost:8001",
            )
        ]

        manager = MCPClientManager(
            server_configs=configs,
            external_id="test-cli-key",
            project_path="/path/to/project",
            project_id="project-uuid",
        )

        assert manager.external_id == "test-cli-key"
        assert manager.project_path == "/path/to/project"
        assert manager.project_id == "project-uuid"

    def test_init_stores_stdio_errlog_path(self) -> None:
        """Test initialization with stdio stderr log path."""
        manager = MCPClientManager(
            server_configs=[],
            stdio_errlog_path="/tmp/mcp-client.log",
        )

        assert manager.stdio_errlog_path == "/tmp/mcp-client.log"


class TestMCPClientManagerConnections:
    """Tests for MCPClientManager connection operations."""

    def test_list_connections_empty(self) -> None:
        """Test listing connections when none are connected."""
        manager = MCPClientManager(server_configs=[])

        assert manager.list_connections() == []

    def test_get_client_not_found_raises(self) -> None:
        """Test getting unknown client raises error."""
        manager = MCPClientManager(server_configs=[])

        with pytest.raises(ValueError, match="Unknown MCP server: 'nonexistent'"):
            manager.get_client("nonexistent")

    @pytest.mark.asyncio
    async def test_connect_all_no_enabled_servers(self):
        """Test connect_all with no enabled servers."""
        configs = [
            MCPServerConfig(
                name="disabled-server",
                project_id="test-project-uuid",
                transport="http",
                url="http://localhost:8001",
                enabled=False,
            ),
        ]

        manager = MCPClientManager(server_configs=configs)
        await manager.connect_all()

        assert len(manager.connections) == 0

    @pytest.mark.asyncio
    async def test_connect_server_passes_stdio_errlog_path_to_factory(self) -> None:
        """Test stdio stderr log path is passed to the transport factory."""
        config = MCPServerConfig(
            name="stdio-server",
            project_id="test-project-uuid",
            transport="stdio",
            command="node",
        )
        manager = MCPClientManager(
            server_configs=[config],
            stdio_errlog_path="/tmp/mcp-client.log",
        )
        session = MagicMock()
        connection = MagicMock()
        connection.connect = AsyncMock(return_value=session)

        with patch(
            "gobby.mcp_proxy.manager.create_transport_connection",
            return_value=connection,
        ) as mock_factory:
            result = await manager._connect_server(config)

        assert result is session
        assert manager.connections[config.id] is connection
        assert manager.health[config.id].state == ConnectionState.CONNECTED
        connection.connect.assert_awaited_once()
        mock_factory.assert_called_once_with(
            config,
            stdio_errlog_path="/tmp/mcp-client.log",
        )

    @pytest.mark.asyncio
    async def test_disconnect_all_empty(self):
        """Test disconnect_all when no connections exist."""
        manager = MCPClientManager(server_configs=[])
        manager._tool_schema_cache["stale-server"] = [{"name": "test-tool"}]

        # Should not raise
        await manager.disconnect_all()
        assert manager.connections == {}
        assert manager._tool_schema_cache == {}


class TestMCPClientManagerHealth:
    """Tests for MCPClientManager health monitoring."""

    @pytest.mark.asyncio
    async def test_health_check_all_empty(self):
        """Test health check with no connections."""
        manager = MCPClientManager(server_configs=[])

        health_status = await manager.health_check_all()

        assert health_status == {}

    @pytest.mark.asyncio
    async def test_get_health_report_empty(self):
        """Test health report with no connections."""
        manager = MCPClientManager(server_configs=[])

        report = await manager.get_health_report()

        assert report == {}

    @pytest.mark.asyncio
    async def test_get_health_report_with_tracking(self):
        """Test health report includes tracked data."""
        manager = MCPClientManager(server_configs=[])

        # Manually add health tracking
        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
            health=HealthState.HEALTHY,
            last_health_check=datetime.now(),
            response_time_ms=50.0,
        )

        report = await manager.get_health_report()

        assert "test-server" in report
        assert report["test-server"]["state"] == "connected"
        assert report["test-server"]["health"] == "healthy"
        assert report["test-server"]["name"] == "test-server"
        assert report["test-server"]["response_time_ms"] == 50.0


class TestMCPClientManagerServerOperations:
    """Tests for MCPClientManager add/remove server operations."""

    @pytest.mark.asyncio
    async def test_add_server_duplicate_raises(self):
        """Test adding duplicate server raises error."""
        configs = [
            MCPServerConfig(
                name="server1",
                project_id="test-project-uuid",
                transport="http",
                url="http://localhost:8001",
            )
        ]

        manager = MCPClientManager(server_configs=configs)

        # Mock the connection
        mock_connection = MagicMock()
        mock_connection.is_connected = True
        manager.connections[configs[0].id] = mock_connection

        # Try to add same server
        with pytest.raises(MCPError, match="MCP server 'server1' already exists"):
            await manager.add_server(
                MCPServerConfig(
                    name="server1",
                    project_id="test-project-uuid",
                    transport="http",
                    url="http://localhost:8001",
                )
            )

    @pytest.mark.asyncio
    async def test_remove_server_not_found_raises(self):
        """Test removing unknown server raises error."""
        manager = MCPClientManager(server_configs=[])

        with pytest.raises(ValueError, match="MCP server 'nonexistent'.*not found"):
            await manager.remove_server("nonexistent", project_id="test-project")


class TestMCPError:
    """Tests for MCPError exception."""

    def test_mcp_error_message(self) -> None:
        """Test MCPError stores message."""
        error = MCPError("Test error message")

        assert str(error) == "Test error message"
        assert error.code is None

    def test_mcp_error_with_code(self) -> None:
        """Test MCPError with error code."""
        error = MCPError("JSON-RPC error", code=-32600)

        assert str(error) == "JSON-RPC error"
        assert error.code == -32600


class TestConnectionStateEnum:
    """Tests for ConnectionState enum."""

    def test_connection_states(self) -> None:
        """Test all connection state values."""
        assert ConnectionState.DISCONNECTED.value == "disconnected"
        assert ConnectionState.CONNECTING.value == "connecting"
        assert ConnectionState.CONNECTED.value == "connected"
        assert ConnectionState.FAILED.value == "failed"
        assert ConnectionState.NEEDS_CONFIGURATION.value == "needs_configuration"
        assert ConnectionState.STALE_TEMPLATE.value == "stale_template"
        assert ConnectionState.DISABLED.value == "disabled"


class TestHealthStateEnum:
    """Tests for HealthState enum."""

    def test_health_states(self) -> None:
        """Test all health state values."""
        assert HealthState.HEALTHY.value == "healthy"
        assert HealthState.DEGRADED.value == "degraded"
        assert HealthState.UNHEALTHY.value == "unhealthy"


# ---------------------------------------------------------------------------
# Plan 4.1: id-keyed manager, refresh_server, fail-closed secrets
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _row(**kwargs: Any) -> MCPServer:
    return MCPServer(
        id=str(kwargs.get("id", uuid4())),
        name=kwargs["name"],
        transport=kwargs.get("transport", "http"),
        url=kwargs.get("url", "http://localhost:8001"),
        command=kwargs.get("command"),
        args=kwargs.get("args"),
        env=kwargs.get("env"),
        headers=kwargs.get("headers"),
        enabled=kwargs.get("enabled", True),
        description=kwargs.get("description"),
        requires_oauth=False,
        oauth_provider=None,
        connect_timeout=kwargs.get("connect_timeout", 30.0),
        created_at=_now(),
        updated_at=_now(),
        project_id=kwargs["project_id"],
        template_id=kwargs.get("template_id"),
        template_values=kwargs.get("template_values"),
        runtime_hook=kwargs.get("runtime_hook"),
        template=kwargs.get("template"),
    )


class FakeMCPDb:
    """In-memory MCP DB surface used by 4.1 manager tests."""

    def __init__(self, servers: list[MCPServer] | None = None) -> None:
        self.servers: dict[str, MCPServer] = {row.id: row for row in (servers or [])}
        self.cached_tools: dict[str, list[Any]] = {}
        self.expand_errors: dict[str, dict[str, str]] = {}
        self.db = object()
        self.deleted_ids: set[str] = set()

    def list_all_servers(self, enabled_only: bool = False) -> list[MCPServer]:
        rows = [row for row in self.servers.values() if row.id not in self.deleted_ids]
        if enabled_only:
            rows = [row for row in rows if row.enabled]
        return rows

    def insert_server(self, **kwargs: Any) -> MCPServer | None:
        name = str(kwargs["name"]).lower()
        project_id = kwargs["project_id"]
        for row in self.servers.values():
            if row.name == name and row.project_id == project_id:
                return None
        allowed = {
            "id",
            "transport",
            "url",
            "command",
            "args",
            "env",
            "headers",
            "enabled",
            "description",
            "connect_timeout",
            "project_id",
            "template_id",
            "template_values",
            "runtime_hook",
            "template",
        }
        row = _row(name=name, **{k: v for k, v in kwargs.items() if k in allowed})
        row.name = name
        self.servers[row.id] = row
        return row

    def get_server_by_id(self, server_id: str) -> MCPServer | None:
        if server_id in self.deleted_ids:
            return None
        return self.servers.get(server_id)

    def get_cached_tools(self, server_id: str) -> list[Any]:
        return list(self.cached_tools.get(server_id, []))

    def cache_tools(self, server_id: str, tools: list[dict[str, Any]]) -> int:
        stored = [
            SimpleNamespace(
                name=tool["name"],
                description=tool.get("description", ""),
            )
            for tool in tools
        ]
        self.cached_tools[server_id] = stored
        return len(stored)

    def refresh_template_instances(
        self,
        expand: Any,
        *,
        server_id: str | None = None,
    ) -> dict[str, Any]:
        if server_id is not None and server_id in self.expand_errors:
            return {"refreshed": 0, "errors": {server_id: self.expand_errors[server_id]}}
        target = self.get_server_by_id(server_id) if server_id else None
        servers = [target] if target is not None and target.template_id else []
        errors: dict[str, dict[str, str]] = {}
        refreshed = 0
        for server in servers:
            if server.id in self.expand_errors:
                errors[server.id] = self.expand_errors[server.id]
                continue
            try:
                expanded = expand(SimpleNamespace(definition={}), server)
            except ValueError as exc:
                errors[server.id] = {
                    "name": server.name,
                    "project_id": str(server.project_id),
                    "error": str(exc),
                }
                continue
            for key in (
                "env",
                "args",
                "headers",
                "url",
                "command",
                "transport",
                "connect_timeout",
                "runtime_hook",
            ):
                if key in expanded:
                    setattr(server, key, expanded[key])
            refreshed += 1
        return {"refreshed": refreshed, "errors": errors}

    def update_server(self, name: str, project_id: str, **fields: Any) -> MCPServer | None:
        for row in self.servers.values():
            if row.name == name and row.project_id == project_id:
                for key, value in fields.items():
                    setattr(row, key, value)
                return row
        return None

    def remove_server(self, name: str, project_id: str) -> None:
        for sid, row in list(self.servers.items()):
            if row.name == name and row.project_id == project_id:
                self.deleted_ids.add(sid)
                del self.servers[sid]
                return


class RecordingConnection:
    """Transport double that records the resolved config it was created with."""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.is_connected = False
        self.session: FakeToolSession | None = None
        self.disconnect_calls = 0
        self.connect_calls = 0

    @property
    def resolved_token(self) -> str | None:
        if self.config.env:
            return self.config.env.get("TOKEN")
        if self.config.args:
            return next((item for item in self.config.args if not item.startswith("$")), None)
        return None

    async def connect(self) -> FakeToolSession:
        self.connect_calls += 1
        self.is_connected = True
        self.session = FakeToolSession(self)
        return self.session

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.is_connected = False
        self.session = None


class FakeToolSession:
    def __init__(self, connection: RecordingConnection) -> None:
        self.connection = connection
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return {"token": self.connection.resolved_token, "tool": name}

    async def list_tools(self) -> SimpleNamespace:
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="probe",
                    description="probe",
                    input_schema={"type": "object"},
                )
            ]
        )

    async def read_resource(self, uri: str) -> dict[str, str]:
        return {"uri": uri}


def _patch_transport() -> tuple[
    list[RecordingConnection],
    Callable[[MCPServerConfig, str | None], RecordingConnection],
]:
    created: list[RecordingConnection] = []

    def factory(
        config: MCPServerConfig, stdio_errlog_path: str | None = None
    ) -> RecordingConnection:
        connection = RecordingConnection(config)
        created.append(connection)
        return connection

    return created, factory


def _resolve_with_store(store: dict[str, str]) -> Any:
    def resolve(self: MCPClientManager, config: MCPServerConfig) -> MCPServerConfig:
        from dataclasses import replace

        missing: list[str] = []

        def subst(text: str) -> str:
            if text.startswith("$secret:"):
                name = text.split(":", 1)[1]
                if name not in store:
                    missing.append(name)
                    return text
                return store[name]
            return text

        env = {key: subst(value) for key, value in (config.env or {}).items()}
        args = [subst(value) for value in (config.args or [])]
        headers = {key: subst(value) for key, value in (config.headers or {}).items()}
        if missing:
            names = ", ".join(dict.fromkeys(missing))
            raise MCPError(
                f"Server '{config.name}' needs configuration: missing secret(s) {names}",
                missing_secrets=list(dict.fromkeys(missing)),
            )
        return replace(config, env=env or None, args=args or None, headers=headers or None)

    return resolve


class TestIdKeyedManager:
    def test_same_name_in_two_projects_are_independent_servers(self) -> None:
        project_a = str(uuid4())
        project_b = str(uuid4())
        a = _row(name="github", project_id=project_a, url="http://a.example")
        b = _row(name="github", project_id=project_b, url="http://b.example")
        db = FakeMCPDb([a, b])
        manager = MCPClientManager(mcp_db_manager=db, lazy_connect=True)

        assert set(manager._configs) == {a.id, b.id}
        assert set(manager._lazy_connector.get_all_states()) == {a.id, b.id}
        config_a = manager.get_server_config(a.id)
        config_b = manager.get_server_config(b.id)
        assert config_a is not None
        assert config_b is not None
        assert config_a.url == "http://a.example"
        assert config_b.url == "http://b.example"
        assert manager.get_available_servers(project_id=project_a) == ["github"]
        assert manager.get_available_servers(project_id=project_b) == ["github"]

    def test_load_initial_configs_uses_db_row_ids(self) -> None:
        row = _row(name="context7", project_id=str(uuid4()))
        db = FakeMCPDb([row])
        db.cache_tools(row.id, [{"name": "query-docs", "description": "docs"}])
        manager = MCPClientManager(mcp_db_manager=db)
        config = manager.get_server_config(row.id)
        assert config is not None
        assert config.id == row.id
        assert config.tools == [{"name": "query-docs", "brief": "docs"}]
        assert row.id in manager._configs

    @pytest.mark.asyncio
    async def test_add_server_adopts_insert_server_id(self) -> None:
        db = FakeMCPDb()
        created, factory = _patch_transport()
        config = MCPServerConfig(
            name="linear",
            project_id=str(uuid4()),
            transport="http",
            url="http://localhost:9",
        )
        with patch("gobby.mcp_proxy.manager.create_transport_connection", side_effect=factory):
            manager = MCPClientManager(mcp_db_manager=db, lazy_connect=False)
            result = await manager.add_server(config)

        assert result["success"] is True
        persisted = next(iter(db.servers.values()))
        assert persisted.id in manager._configs
        assert manager._configs[persisted.id].id == persisted.id
        assert created[0].config.id == persisted.id

    @pytest.mark.asyncio
    async def test_add_server_duplicate_insert_registers_nothing(self) -> None:
        project_id = str(uuid4())
        existing = _row(name="linear", project_id=project_id)
        db = FakeMCPDb([existing])
        manager = MCPClientManager(mcp_db_manager=db)
        with pytest.raises(MCPError, match="already exists"):
            await manager.add_server(
                MCPServerConfig(
                    name="linear",
                    project_id=project_id,
                    transport="http",
                    url="http://localhost:9",
                )
            )
        assert list(manager._configs) == [existing.id]


@pytest.mark.asyncio
async def test_refresh_server_rotates_secret_for_selected_instance_only() -> None:
    project_a = str(uuid4())
    project_b = str(uuid4())
    a = _row(
        name="github",
        project_id=project_a,
        env={"TOKEN": "$secret:github_token"},
    )
    b = _row(
        name="github",
        project_id=project_b,
        env={"TOKEN": "$secret:github_token"},
    )
    db = FakeMCPDb([a, b])
    store = {f"{project_a}:github_token": "alpha", f"{project_b}:github_token": "beta"}

    def resolve(self: MCPClientManager, config: MCPServerConfig) -> MCPServerConfig:
        from dataclasses import replace

        key = f"{config.project_id}:github_token"
        return replace(config, env={"TOKEN": store[key]})

    created, factory = _patch_transport()
    with (
        patch("gobby.mcp_proxy.manager.create_transport_connection", side_effect=factory),
        patch.object(MCPClientManager, "_resolve_secrets_in_config", resolve),
    ):
        manager = MCPClientManager(mcp_db_manager=db, lazy_connect=False)
        assert callable(manager.refresh_server)
        await manager.connect_all()
        first_a = manager._connections[a.id]
        first_b = manager._connections[b.id]
        store[f"{project_a}:github_token"] = "rotated-a"
        await manager.refresh_server(a.id)
        after_a = await manager.call_tool(a.id, "probe", {})
        after_b = await manager.call_tool(b.id, "probe", {})

    assert after_a["token"] == "rotated-a"
    assert after_b["token"] == "beta"
    assert manager._connections[a.id] is not first_a
    assert manager._connections[b.id] is first_b
    assert a.id not in manager._tool_schema_cache or manager._tool_schema_cache[a.id]


@pytest.mark.asyncio
async def test_refresh_server_is_linearizable_against_concurrent_calls() -> None:
    project_id = str(uuid4())
    row = _row(
        name="github",
        project_id=project_id,
        env={"TOKEN": "$secret:github_token"},
    )
    db = FakeMCPDb([row])
    store = {"github_token": "before"}
    created, factory = _patch_transport()
    in_flight_entered = asyncio.Event()
    in_flight_release = asyncio.Event()

    class BlockingSession(FakeToolSession):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            in_flight_entered.set()
            await in_flight_release.wait()
            return await super().call_tool(name, arguments)

    class BlockingConnection(RecordingConnection):
        async def connect(self) -> FakeToolSession:
            self.connect_calls += 1
            self.is_connected = True
            self.session = BlockingSession(self)
            return self.session

    blocking: list[RecordingConnection] = []

    def first_factory(
        config: MCPServerConfig, stdio_errlog_path: str | None = None
    ) -> RecordingConnection:
        if not blocking:
            connection = BlockingConnection(config)
            blocking.append(connection)
            return connection
        return factory(config, stdio_errlog_path)

    with (
        patch(
            "gobby.mcp_proxy.manager.create_transport_connection",
            side_effect=first_factory,
        ),
        patch.object(MCPClientManager, "_resolve_secrets_in_config", _resolve_with_store(store)),
    ):
        manager = MCPClientManager(mcp_db_manager=db, lazy_connect=False)
        assert callable(manager.refresh_server)
        await manager.connect_all()
        in_flight = asyncio.create_task(manager.call_tool(row.id, "probe", {}))
        await asyncio.wait_for(in_flight_entered.wait(), timeout=1)
        store["github_token"] = "after"
        refresh_task = asyncio.create_task(manager.refresh_server(row.id))
        await refresh_task
        in_flight_release.set()
        in_flight_result = await in_flight
        after = await manager.call_tool(row.id, "probe", {})

    assert after["token"] == "after"
    assert in_flight_result["token"] in {"before", "after"}

    db.deleted_ids.add(row.id)
    db.servers.pop(row.id, None)
    with pytest.raises(MCPError, match="[Uu]nknown|[Nn]ot found"):
        await manager.refresh_server(row.id)


@pytest.mark.asyncio
async def test_missing_required_secret_fails_closed_on_every_connection_path() -> None:
    project_id = str(uuid4())
    row = _row(
        name="github",
        project_id=project_id,
        env={"TOKEN": "$secret:github_token"},
    )
    db = FakeMCPDb([row])
    store: dict[str, str] = {}
    created, factory = _patch_transport()
    with (
        patch("gobby.mcp_proxy.manager.create_transport_connection", side_effect=factory),
        patch.object(MCPClientManager, "_resolve_secrets_in_config", _resolve_with_store(store)),
    ):
        manager = MCPClientManager(mcp_db_manager=db, lazy_connect=True)
        assert callable(manager.refresh_server)
        health = manager.get_server_health()[row.id]
        assert health["state"] == "needs_configuration"
        assert health["missing_secrets"] == ["github_token"]
        assert "$secret:" not in str(health.get("missing_secrets"))
        assert created == []
        with pytest.raises(MCPError, match="missing secret"):
            await manager.ensure_connected(row.id)
        assert row.id not in manager._connections
        store["github_token"] = "set"
        await manager.refresh_server(row.id)
        assert manager.is_connected(row.id)
        result = await manager.call_tool(row.id, "probe", {})
        assert result["token"] == "set"


@pytest.mark.asyncio
async def test_optional_secret_reexpands_on_all_connection_paths() -> None:
    project_id = str(uuid4())
    template_id = str(uuid4())
    row = _row(
        name="search",
        project_id=project_id,
        template_id=template_id,
        template="brave-search",
        env={"KEY": "plain"},
    )
    db = FakeMCPDb([row])
    optional_present = {"on": False}

    def expand(_template: Any, server: MCPServer) -> dict[str, Any]:
        env = {"KEY": "plain"}
        if optional_present["on"]:
            env["OPTIONAL"] = "$secret:optional_token"
        return {"env": env}

    store = {"optional_token": "opt-value"}
    created, factory = _patch_transport()
    with (
        patch("gobby.mcp_proxy.manager.create_transport_connection", side_effect=factory),
        patch.object(MCPClientManager, "_resolve_secrets_in_config", _resolve_with_store(store)),
    ):
        manager = MCPClientManager(
            mcp_db_manager=db,
            lazy_connect=False,
            template_expand=expand,
        )
        await manager.connect_all()
        assert "OPTIONAL" not in (manager._configs[row.id].env or {})
        optional_present["on"] = True
        await manager.refresh_server(row.id)
        assert manager._configs[row.id].env == {
            "KEY": "plain",
            "OPTIONAL": "$secret:optional_token",
        }
        resolved_env = manager._connections[row.id].config.env
        assert resolved_env is not None
        assert resolved_env["OPTIONAL"] == "opt-value"
        health = manager.get_server_health()[row.id]
        assert health["state"] != "needs_configuration"
        optional_present["on"] = False
        await manager.refresh_server(row.id)
        assert "OPTIONAL" not in (manager._configs[row.id].env or {})
        assert manager.get_server_health()[row.id]["state"] != "needs_configuration"


@pytest.mark.asyncio
async def test_registry_config_keeps_secret_references_after_refresh() -> None:
    project_id = str(uuid4())
    row = _row(
        name="github",
        project_id=project_id,
        env={"TOKEN": "$secret:github_token"},
    )
    db = FakeMCPDb([row])
    store = {"github_token": "live"}
    created, factory = _patch_transport()
    with (
        patch("gobby.mcp_proxy.manager.create_transport_connection", side_effect=factory),
        patch.object(MCPClientManager, "_resolve_secrets_in_config", _resolve_with_store(store)),
    ):
        manager = MCPClientManager(mcp_db_manager=db, lazy_connect=False)
        assert callable(manager.refresh_server)
        await manager.connect_all()
        await manager.refresh_server(row.id)
        assert manager._configs[row.id].env == {"TOKEN": "$secret:github_token"}
        assert manager._connections[row.id].config.env == {"TOKEN": "live"}


@pytest.mark.asyncio
async def test_refresh_server_keeps_last_known_good_on_expansion_error() -> None:
    project_id = str(uuid4())
    good = _row(name="ok", project_id=project_id, url="http://ok.example")
    stale = _row(
        name="stale",
        project_id=project_id,
        template_id=str(uuid4()),
        template="broken",
        url="http://stale.example",
    )
    db = FakeMCPDb([good, stale])
    created, factory = _patch_transport()
    with (
        patch("gobby.mcp_proxy.manager.create_transport_connection", side_effect=factory),
        patch.object(MCPClientManager, "_resolve_secrets_in_config", _resolve_with_store({})),
    ):
        manager = MCPClientManager(mcp_db_manager=db, lazy_connect=False)
        assert callable(manager.refresh_server)
        await manager.connect_all()
        live = manager._connections[stale.id]
        db.expand_errors[stale.id] = {
            "name": "stale",
            "project_id": project_id,
            "error": "Missing required parameter 'api_key'",
        }
        with pytest.raises(MCPError, match="api_key"):
            await manager.refresh_server(stale.id)
        assert manager._connections[stale.id] is live
        assert manager._configs[stale.id].url == "http://stale.example"
        health = manager.get_server_health()[stale.id]
        assert health["state"] == "stale_template"
        assert "api_key" in (health["last_error"] or "")
        assert "$secret:" not in (health["last_error"] or "")
        assert manager.has_server(good.id)
        assert manager.get_server_health()[good.id]["state"] == "connected"

    db2 = FakeMCPDb([good, stale])
    db2.expand_errors[stale.id] = {
        "name": "stale",
        "project_id": project_id,
        "error": "Missing required parameter 'api_key'",
    }
    manager2 = MCPClientManager(mcp_db_manager=db2, lazy_connect=True)
    assert manager2.get_server_health()[stale.id]["state"] == "stale_template"
    assert manager2.has_server(good.id)
    assert stale.id not in manager2._connections


@pytest.mark.asyncio
async def test_refresh_with_deleted_secret_disconnects_old_transport() -> None:
    project_id = str(uuid4())
    row = _row(
        name="github",
        project_id=project_id,
        env={"TOKEN": "$secret:github_token"},
    )
    db = FakeMCPDb([row])
    store = {"github_token": "live"}
    created, factory = _patch_transport()
    with (
        patch("gobby.mcp_proxy.manager.create_transport_connection", side_effect=factory),
        patch.object(MCPClientManager, "_resolve_secrets_in_config", _resolve_with_store(store)),
    ):
        manager = MCPClientManager(mcp_db_manager=db, lazy_connect=False)
        assert callable(manager.refresh_server)
        await manager.connect_all()
        old = manager._connections[row.id]
        assert isinstance(old, RecordingConnection)
        del store["github_token"]
        with pytest.raises(MCPError, match="missing secret"):
            await manager.refresh_server(row.id)
        assert old.disconnect_calls >= 1
        assert row.id not in manager._connections
        assert manager.get_server_health()[row.id]["state"] == "needs_configuration"
        with pytest.raises(MCPError):
            await manager.call_tool(row.id, "probe", {})
        assert row.id not in manager._connections or not manager.is_connected(row.id)


@pytest.mark.asyncio
async def test_refresh_server_never_connects_disabled_instance() -> None:
    project_id = str(uuid4())
    row = _row(
        name="github",
        project_id=project_id,
        enabled=False,
        env={"TOKEN": "$secret:github_token"},
    )
    db = FakeMCPDb([row])
    resolved = {"called": False}

    def resolve(self: MCPClientManager, config: MCPServerConfig) -> MCPServerConfig:
        resolved["called"] = True
        raise AssertionError("disabled refresh must not resolve secrets")

    created, factory = _patch_transport()
    with (
        patch("gobby.mcp_proxy.manager.create_transport_connection", side_effect=factory),
        patch.object(MCPClientManager, "_resolve_secrets_in_config", resolve),
    ):
        manager = MCPClientManager(mcp_db_manager=db, lazy_connect=True)
        await manager.refresh_server(row.id)

    assert created == []
    assert resolved["called"] is False
    assert row.id not in manager._connections
    assert manager.get_server_health()[row.id]["state"] == "disabled"
    disabled = manager.get_server_config(row.id)
    assert disabled is not None
    assert disabled.enabled is False


class TestCacheDiscoveredToolsById:
    def test_cache_discovered_tools_persists_by_server_id(self) -> None:
        row = _row(name="github", project_id=str(uuid4()))
        db = FakeMCPDb([row])
        manager = MCPClientManager(mcp_db_manager=db)
        tools = [
            {
                "name": "create_issue",
                "description": "Create an issue",
                "inputSchema": {"type": "object"},
            }
        ]
        manager.cache_discovered_tools(row.id, tools)
        assert manager._tool_schema_cache[row.id] == tools
        assert db.cached_tools[row.id][0].name == "create_issue"

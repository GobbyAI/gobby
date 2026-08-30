"""Tests for the MCP Client Manager."""

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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
        assert manager.health == {}

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
        assert manager.connections["stdio-server"] is connection
        assert manager.health["stdio-server"].state == ConnectionState.CONNECTED
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
        manager.connections["server1"] = mock_connection

        # Try to add same server
        with pytest.raises(ValueError, match="MCP server 'server1' already exists"):
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


class TestHealthStateEnum:
    """Tests for HealthState enum."""

    def test_health_states(self) -> None:
        """Test all health state values."""
        assert HealthState.HEALTHY.value == "healthy"
        assert HealthState.DEGRADED.value == "degraded"
        assert HealthState.UNHEALTHY.value == "unhealthy"

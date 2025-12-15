"""Tests for the MCP Client Manager."""

from datetime import datetime
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


class TestMCPServerConfig:
    """Tests for MCPServerConfig dataclass."""

    def test_http_config_valid(self):
        """Test valid HTTP config."""
        config = MCPServerConfig(
            name="test-server",
            transport="http",
            url="http://localhost:8080/mcp",
            enabled=True,
            project_id="test-project-uuid",
        )

        # Should not raise
        config.validate()
        assert config.name == "test-server"
        assert config.transport == "http"
        assert config.url == "http://localhost:8080/mcp"

    def test_http_config_missing_url_raises(self):
        """Test HTTP config without URL raises error."""
        config = MCPServerConfig(
            name="test-server",
            transport="http",
            url=None,
        )

        with pytest.raises(ValueError, match="http transport requires 'url' parameter"):
            config.validate()

    def test_stdio_config_valid(self):
        """Test valid stdio config."""
        config = MCPServerConfig(
            name="stdio-server",
            transport="stdio",
            command="npx",
            args=["-y", "@test/server"],
            env={"DEBUG": "true"},
        )

        # Should not raise
        config.validate()
        assert config.command == "npx"
        assert config.args == ["-y", "@test/server"]

    def test_stdio_config_missing_command_raises(self):
        """Test stdio config without command raises error."""
        config = MCPServerConfig(
            name="test-server",
            transport="stdio",
            command=None,
        )

        with pytest.raises(ValueError, match="stdio transport requires 'command' parameter"):
            config.validate()

    def test_websocket_config_valid(self):
        """Test valid WebSocket config."""
        config = MCPServerConfig(
            name="ws-server",
            transport="websocket",
            url="ws://localhost:8080/mcp",
        )

        # Should not raise
        config.validate()

    def test_unsupported_transport_raises(self):
        """Test unsupported transport raises error."""
        config = MCPServerConfig(
            name="test-server",
            transport="invalid",
        )

        with pytest.raises(ValueError, match="Unsupported transport"):
            config.validate()

    def test_http_config_with_headers(self):
        """Test HTTP config with custom headers."""
        config = MCPServerConfig(
            name="api-server",
            transport="http",
            url="https://api.example.com/mcp",
            headers={"Authorization": "Bearer token123", "X-API-Key": "secret"},
        )

        config.validate()
        assert config.headers == {"Authorization": "Bearer token123", "X-API-Key": "secret"}


class TestMCPConnectionHealth:
    """Tests for MCPConnectionHealth tracking."""

    def test_initial_state(self):
        """Test initial health state."""
        health = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        assert health.health == HealthState.HEALTHY
        assert health.consecutive_failures == 0
        assert health.last_error is None

    def test_record_success(self):
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

    def test_record_failure_degraded(self):
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

    def test_record_failure_unhealthy(self):
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

    def test_create_http_connection(self):
        """Test creating HTTP transport connection."""
        config = MCPServerConfig(
            name="http-server",
            transport="http",
            url="http://localhost:8080/mcp",
        )

        connection = _create_transport_connection(config)

        assert connection.config == config
        assert connection.state == ConnectionState.DISCONNECTED

    def test_create_stdio_connection(self):
        """Test creating stdio transport connection."""
        config = MCPServerConfig(
            name="stdio-server",
            transport="stdio",
            command="npx",
            args=["-y", "@test/server"],
        )

        connection = _create_transport_connection(config)

        assert connection.config == config
        assert connection.state == ConnectionState.DISCONNECTED

    def test_create_websocket_connection(self):
        """Test creating WebSocket transport connection."""
        config = MCPServerConfig(
            name="ws-server",
            transport="websocket",
            url="ws://localhost:8080/mcp",
        )

        connection = _create_transport_connection(config)

        assert connection.config == config
        assert connection.state == ConnectionState.DISCONNECTED

    def test_create_unsupported_transport_raises(self):
        """Test unsupported transport raises error."""
        config = MCPServerConfig(
            name="invalid-server",
            transport="invalid",
        )

        with pytest.raises(ValueError, match="Unsupported transport"):
            _create_transport_connection(config)


class TestMCPClientManagerInit:
    """Tests for MCPClientManager initialization."""

    def test_init_with_configs(self):
        """Test initialization with server configs."""
        configs = [
            MCPServerConfig(name="server1", transport="http", url="http://localhost:8001"),
            MCPServerConfig(name="server2", transport="http", url="http://localhost:8002"),
        ]

        manager = MCPClientManager(server_configs=configs)

        assert len(manager.server_configs) == 2
        assert manager.connections == {}
        assert manager.health == {}

    def test_init_empty_configs(self):
        """Test initialization with empty configs."""
        manager = MCPClientManager(server_configs=[])

        assert manager.server_configs == []
        assert manager.connections == {}

    def test_init_with_project_context(self):
        """Test initialization with project context."""
        configs = [MCPServerConfig(name="server1", transport="http", url="http://localhost:8001")]

        manager = MCPClientManager(
            server_configs=configs,
            external_id="test-cli-key",
            project_path="/path/to/project",
            project_id="project-uuid",
        )

        assert manager.external_id == "test-cli-key"
        assert manager.project_path == "/path/to/project"
        assert manager.project_id == "project-uuid"


class TestMCPClientManagerConnections:
    """Tests for MCPClientManager connection operations."""

    def test_list_connections_empty(self):
        """Test listing connections when none are connected."""
        manager = MCPClientManager(server_configs=[])

        assert manager.list_connections() == []

    def test_get_client_not_found_raises(self):
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
                transport="http",
                url="http://localhost:8001",
                enabled=False,
            ),
        ]

        manager = MCPClientManager(server_configs=configs)
        await manager.connect_all()

        assert len(manager.connections) == 0

    @pytest.mark.asyncio
    async def test_disconnect_all_empty(self):
        """Test disconnect_all when no connections exist."""
        manager = MCPClientManager(server_configs=[])

        # Should not raise
        await manager.disconnect_all()
        assert manager.connections == {}


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
        configs = [MCPServerConfig(name="server1", transport="http", url="http://localhost:8001")]

        manager = MCPClientManager(server_configs=configs)

        # Mock the connection
        mock_connection = MagicMock()
        mock_connection.is_connected = True
        manager.connections["server1"] = mock_connection

        # Try to add same server
        with pytest.raises(ValueError, match="MCP server 'server1' already exists"):
            await manager.add_server(
                MCPServerConfig(name="server1", transport="http", url="http://localhost:8001")
            )

    @pytest.mark.asyncio
    async def test_remove_server_not_found_raises(self):
        """Test removing unknown server raises error."""
        manager = MCPClientManager(server_configs=[])

        with pytest.raises(ValueError, match="MCP server 'nonexistent'.*not found"):
            await manager.remove_server("nonexistent", project_id="test-project")


class TestMCPError:
    """Tests for MCPError exception."""

    def test_mcp_error_message(self):
        """Test MCPError stores message."""
        error = MCPError("Test error message")

        assert str(error) == "Test error message"
        assert error.code is None

    def test_mcp_error_with_code(self):
        """Test MCPError with error code."""
        error = MCPError("JSON-RPC error", code=-32600)

        assert str(error) == "JSON-RPC error"
        assert error.code == -32600


class TestConnectionStateEnum:
    """Tests for ConnectionState enum."""

    def test_connection_states(self):
        """Test all connection state values."""
        assert ConnectionState.DISCONNECTED.value == "disconnected"
        assert ConnectionState.CONNECTING.value == "connecting"
        assert ConnectionState.CONNECTED.value == "connected"
        assert ConnectionState.FAILED.value == "failed"


class TestHealthStateEnum:
    """Tests for HealthState enum."""

    def test_health_states(self):
        """Test all health state values."""
        assert HealthState.HEALTHY.value == "healthy"
        assert HealthState.DEGRADED.value == "degraded"
        assert HealthState.UNHEALTHY.value == "unhealthy"

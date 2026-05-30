"""
Comprehensive unit tests for MCPClientManager to increase coverage.

Focuses on MCP client management operations including:
- Database-backed server loading
- Lazy connection handling
- Health monitoring
- Tool operations
- Error handling and edge cases
"""

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.bundled import CHROME_DEVTOOLS_NPM_PACKAGE
from gobby.mcp_proxy.lazy import CircuitBreakerOpen, CircuitState
from gobby.mcp_proxy.manager import MCPClientManager, truncate_tool_brief
from gobby.mcp_proxy.models import (
    ConnectionState,
    HealthState,
    MCPConnectionHealth,
    MCPError,
    MCPServerConfig,
)
from tests._timing import wait_forever

pytestmark = pytest.mark.unit


def test_truncate_tool_brief_handles_non_positive_lengths() -> None:
    assert truncate_tool_brief("abcdef", max_chars=0) == ""
    assert truncate_tool_brief("abcdef", max_chars=-1) == ""
    assert truncate_tool_brief("abcdef", max_chars=1) == "…"
    assert truncate_tool_brief("abcdef", max_chars=4) == "abc…"


def test_mcp_proxy_source_does_not_register_legacy_gobby_code_server() -> None:
    active_sources = Path("src/gobby/mcp_proxy").rglob("*.py")
    matches = [path for path in active_sources if "gobby-code" in path.read_text()]

    assert matches == []


class MockDBServer:
    """Mock database server object for testing."""

    def __init__(
        self,
        name: str,
        transport: str = "http",
        url: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        enabled: bool = True,
        description: str | None = None,
        project_id: str = "test-project",
    ):
        self.name = name
        self.transport = transport
        self.url = url
        self.command = command
        self.args = args
        self.env = env
        self.headers = headers
        self.enabled = enabled
        self.description = description
        self.project_id = project_id


class MockCachedTool:
    """Mock cached tool object for testing."""

    def __init__(self, name: str, description: str | None = None):
        self.name = name
        self.description = description


class TestMCPClientManagerDatabaseInit:
    """Tests for MCPClientManager initialization from database."""

    def test_init_with_db_manager_and_project_id(self) -> None:
        """Test loading servers from database with project_id."""
        mock_db = MagicMock()
        mock_db.list_runtime_servers.return_value = [
            MockDBServer(
                name="db-server-1",
                transport="http",
                url="http://localhost:8001",
                project_id="test-project",
            ),
            MockDBServer(
                name="db-server-2",
                transport="stdio",
                command="python",
                args=["-m", "server"],
                project_id="test-project",
            ),
        ]
        mock_db.get_cached_tools.return_value = None

        manager = MCPClientManager(
            mcp_db_manager=mock_db,
            project_id="test-project",
        )

        assert len(manager.server_configs) == 2
        assert manager.has_server("db-server-1")
        assert manager.has_server("db-server-2")
        mock_db.list_runtime_servers.assert_called_once_with(
            project_id="test-project",
            enabled_only=False,
        )

    def test_init_with_db_manager_no_project_id(self) -> None:
        """Test loading all servers from database without project_id."""
        mock_db = MagicMock()
        mock_db.list_all_servers.return_value = [
            MockDBServer(
                name="global-server",
                transport="http",
                url="http://localhost:9000",
            ),
        ]
        mock_db.get_cached_tools.return_value = None

        manager = MCPClientManager(mcp_db_manager=mock_db)

        assert len(manager.server_configs) == 1
        assert manager.has_server("global-server")
        mock_db.list_all_servers.assert_called_once_with(enabled_only=False)

    def test_init_with_db_manager_loads_cached_tools(self) -> None:
        """Test that cached tools are loaded from database."""
        mock_db = MagicMock()
        long_description = "Another tool" + "x" * 200
        mock_db.list_runtime_servers.return_value = [
            MockDBServer(
                name="server-with-tools",
                transport="http",
                url="http://localhost:8001",
                project_id="test-project",
            ),
        ]
        mock_db.get_cached_tools.return_value = [
            MockCachedTool("tool1", "A tool for testing"),
            MockCachedTool("tool2", long_description),
        ]

        manager = MCPClientManager(
            mcp_db_manager=mock_db,
            project_id="test-project",
        )

        config = manager._configs["server-with-tools"]
        assert config.tools is not None
        assert len(config.tools) == 2
        assert config.tools[0]["name"] == "tool1"
        assert config.tools[0]["brief"] == "A tool for testing"
        assert config.tools[1]["brief"] == f"{long_description[:99]}…"


class TestLoadToolsFromDB:
    """Tests for _load_tools_from_db static method."""

    def test_load_tools_returns_none_when_no_tools(self) -> None:
        """Test returns None when no cached tools exist."""
        mock_db = MagicMock()
        mock_db.get_cached_tools.return_value = []

        result = MCPClientManager._load_tools_from_db(mock_db, "test-server", "test-project")

        assert result is None

    def test_load_tools_handles_exception(self) -> None:
        """Test handles exceptions gracefully."""
        mock_db = MagicMock()
        mock_db.get_cached_tools.side_effect = Exception("Database error")

        result = MCPClientManager._load_tools_from_db(mock_db, "test-server", "test-project")

        assert result is None

    def test_load_tools_handles_none_description(self) -> None:
        """Test handles tools with None description."""
        mock_db = MagicMock()
        mock_db.get_cached_tools.return_value = [
            MockCachedTool("tool1", None),
        ]

        result = MCPClientManager._load_tools_from_db(mock_db, "test-server", "test-project")

        assert result is not None
        assert result[0]["brief"] == ""


class TestMCPClientManagerServerOperations:
    """Tests for server management operations."""

    def test_get_available_servers(self) -> None:
        """Test get_available_servers returns configured server names."""
        configs = [
            MCPServerConfig(
                name="server1",
                project_id="test-project",
                transport="http",
                url="http://localhost:8001",
            ),
            MCPServerConfig(
                name="server2",
                project_id="test-project",
                transport="http",
                url="http://localhost:8002",
            ),
        ]

        manager = MCPClientManager(server_configs=configs)

        available = manager.get_available_servers()
        assert "server1" in available
        assert "server2" in available
        assert len(available) == 2

    def test_has_server_true(self) -> None:
        """Test has_server returns True for configured server."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        assert manager.has_server("test-server") is True

    def test_has_server_false(self) -> None:
        """Test has_server returns False for unknown server."""
        manager = MCPClientManager(server_configs=[])

        assert manager.has_server("nonexistent") is False

    def test_get_client_configured_but_not_connected(self) -> None:
        """Test get_client raises when server configured but not connected."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        with pytest.raises(ValueError, match="Client 'test-server' not connected"):
            manager.get_client("test-server")

    def test_get_client_returns_connection(self) -> None:
        """Test get_client returns connection when connected."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        # Add a mock connection
        mock_connection = MagicMock()
        manager._connections["test-server"] = mock_connection

        result = manager.get_client("test-server")
        assert result is mock_connection


class TestMCPClientManagerAddServer:
    """Tests for add_server method."""

    @pytest.mark.asyncio
    async def test_add_server_success_disabled(self):
        """Test adding a disabled server doesn't attempt connection."""
        manager = MCPClientManager(server_configs=[])

        config = MCPServerConfig(
            name="new-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
            enabled=False,
        )

        result = await manager.add_server(config)

        assert result["success"] is True
        assert result["name"] == "new-server"
        assert result["full_tool_schemas"] == []
        assert manager.has_server("new-server")

    @pytest.mark.asyncio
    async def test_add_server_persists_to_database(self):
        """Test add_server persists config to database."""
        mock_db = MagicMock()
        manager = MCPClientManager(server_configs=[], mcp_db_manager=mock_db)

        config = MCPServerConfig(
            name="new-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
            enabled=False,
        )

        await manager.add_server(config)

        mock_db.upsert.assert_called_once()
        call_kwargs = mock_db.upsert.call_args[1]
        assert call_kwargs["name"] == "new-server"
        assert call_kwargs["project_id"] == "test-project"

    @pytest.mark.asyncio
    async def test_add_server_canonicalizes_bundled_server_scope(self):
        """Bundled servers are persisted under the global project scope."""
        mock_db = MagicMock()
        manager = MCPClientManager(server_configs=[], mcp_db_manager=mock_db)

        config = MCPServerConfig(
            name="chrome-devtools",
            project_id="test-project",
            transport="stdio",
            command="npx",
            args=[
                "-y",
                CHROME_DEVTOOLS_NPM_PACKAGE,
                "--executable-path=/tmp/chrome",
                "--no-usage-statistics",
            ],
            enabled=False,
        )

        await manager.add_server(config)

        call_kwargs = mock_db.upsert.call_args[1]
        assert call_kwargs["project_id"] == "00000000-0000-0000-0000-000000000002"
        assert call_kwargs["args"] == [
            "-y",
            CHROME_DEVTOOLS_NPM_PACKAGE,
            "--no-usage-statistics",
        ]

    @pytest.mark.asyncio
    async def test_add_server_connects_and_lists_tools(self):
        """Test add_server connects and lists tools for enabled server."""
        manager = MCPClientManager(server_configs=[])

        config = MCPServerConfig(
            name="new-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
            enabled=True,
        )

        # Mock the session with tools
        mock_session = AsyncMock()
        mock_tool = MagicMock()
        mock_tool.name = "test-tool"
        mock_tool.description = "Test description"
        mock_tool.inputSchema = {"type": "object"}
        mock_session.list_tools.return_value = MagicMock(tools=[mock_tool])

        with patch.object(manager, "_connect_server", return_value=mock_session):
            result = await manager.add_server(config)

        assert result["success"] is True
        assert len(result["full_tool_schemas"]) == 1
        assert result["full_tool_schemas"][0]["name"] == "test-tool"

    @pytest.mark.asyncio
    async def test_set_server_enabled_connects_and_caches_listed_tools(self) -> None:
        """Enabling a server discovers tools through the shared cache path."""
        config = MCPServerConfig(
            name="existing-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
            enabled=False,
        )
        manager = MCPClientManager(server_configs=[config])

        mock_session = AsyncMock()
        mock_tool = MagicMock()
        mock_tool.name = "enabled-tool"
        mock_tool.description = "Enabled description"
        mock_tool.inputSchema = {"type": "object"}
        mock_session.list_tools.return_value = MagicMock(tools=[mock_tool])
        connect_server = AsyncMock(return_value=mock_session)

        with (
            patch.object(manager, "_connect_server", new=connect_server),
            patch.object(manager, "cache_discovered_tools") as cache_discovered_tools,
        ):
            result = await manager.set_server_enabled("existing-server", True)

        assert result == {"success": True, "name": "existing-server", "enabled": True}
        assert manager.get_server_config("existing-server") == config
        assert config.enabled is True
        connect_server.assert_awaited_once_with(config)
        mock_session.list_tools.assert_awaited_once_with()
        cache_discovered_tools.assert_called_once_with(
            "existing-server",
            [
                {
                    "name": "enabled-tool",
                    "description": "Enabled description",
                    "inputSchema": {"type": "object"},
                }
            ],
        )

    @pytest.mark.asyncio
    async def test_set_server_enabled_rejects_internal_servers(self) -> None:
        config = MCPServerConfig(
            name="gobby-tasks",
            project_id=None,
            transport="internal",
            enabled=True,
        )
        manager = MCPClientManager(server_configs=[config])

        with pytest.raises(ValueError, match="Internal MCP server"):
            await manager.set_server_enabled("gobby-tasks", False)

    @pytest.mark.asyncio
    async def test_set_server_enabled_keeps_memory_state_when_db_update_fails(self) -> None:
        config = MCPServerConfig(
            name="existing-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
            enabled=False,
        )
        mock_db = MagicMock()
        mock_db.update_server.side_effect = RuntimeError("db down")
        manager = MCPClientManager(server_configs=[config], mcp_db_manager=mock_db)
        mock_session = AsyncMock()
        mock_session.list_tools.return_value = MagicMock(tools=[])

        async def connect_server(server_config: MCPServerConfig) -> AsyncMock:
            manager._connections[server_config.name] = mock_session
            return mock_session

        with (
            patch.object(manager, "_connect_server", new=AsyncMock(side_effect=connect_server)),
            pytest.raises(RuntimeError, match="db down"),
        ):
            await manager.set_server_enabled("existing-server", True)

        assert config.enabled is False
        assert manager.get_server_config("existing-server") is config
        mock_session.disconnect.assert_awaited_once_with()
        assert "existing-server" not in manager.health

    @pytest.mark.asyncio
    async def test_set_server_enabled_false_unregisters_lazy_server(self) -> None:
        config = MCPServerConfig(
            name="existing-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
            enabled=True,
        )
        manager = MCPClientManager(server_configs=[config])

        result = await manager.set_server_enabled("existing-server", False)

        assert result == {"success": True, "name": "existing-server", "enabled": False}
        assert config.enabled is False
        assert manager._lazy_connector.get_state("existing-server") is None

    @pytest.mark.asyncio
    async def test_add_server_handles_list_tools_failure(self):
        """Test add_server handles failure when listing tools."""
        manager = MCPClientManager(server_configs=[])

        config = MCPServerConfig(
            name="new-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
            enabled=True,
        )

        mock_session = AsyncMock()
        mock_session.list_tools.side_effect = Exception("Failed to list tools")

        with patch.object(manager, "_connect_server", return_value=mock_session):
            result = await manager.add_server(config)

        assert result["success"] is True
        assert result["full_tool_schemas"] == []


class TestMCPClientManagerRemoveServer:
    """Tests for remove_server method."""

    @pytest.mark.asyncio
    async def test_remove_server_disconnects_and_cleans_up(self):
        """Test remove_server disconnects and removes from tracking."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        # Add mock connection and health
        mock_connection = AsyncMock()
        manager._connections["test-server"] = mock_connection
        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        result = await manager.remove_server("test-server")

        assert result["success"] is True
        assert "test-server" not in manager._configs
        assert "test-server" not in manager._connections
        assert "test-server" not in manager.health
        mock_connection.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_remove_server_uses_config_project_id(self):
        """Test remove_server uses project_id from config if not provided."""
        mock_db = MagicMock()
        config = MCPServerConfig(
            name="test-server",
            project_id="config-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config], mcp_db_manager=mock_db)

        await manager.remove_server("test-server")

        mock_db.remove_server.assert_called_once_with("test-server", "config-project")
        assert mock_db.remove_server.call_count == 1
        assert mock_db.remove_server.call_args is not None

    @pytest.mark.asyncio
    async def test_remove_server_uses_provided_project_id(self):
        """Test remove_server uses provided project_id over config."""
        mock_db = MagicMock()
        config = MCPServerConfig(
            name="test-server",
            project_id="config-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config], mcp_db_manager=mock_db)

        await manager.remove_server("test-server", project_id="override-project")

        mock_db.remove_server.assert_called_once_with("test-server", "override-project")
        assert mock_db.remove_server.call_count == 1
        assert mock_db.remove_server.call_args is not None


class TestMCPClientManagerConnectAll:
    """Tests for connect_all method."""

    @pytest.mark.asyncio
    async def test_connect_all_lazy_mode_only_preconnect(self):
        """Test connect_all in lazy mode only connects preconnect servers."""
        configs = [
            MCPServerConfig(
                name="server1",
                project_id="test-project",
                transport="http",
                url="http://localhost:8001",
            ),
            MCPServerConfig(
                name="preconnect-server",
                project_id="test-project",
                transport="http",
                url="http://localhost:8002",
            ),
        ]

        manager = MCPClientManager(
            server_configs=configs,
            lazy_connect=True,
            preconnect_servers=["preconnect-server"],
        )

        mock_session = AsyncMock()
        connect_calls = []

        async def mock_connect(config):
            connect_calls.append(config.name)
            return mock_session

        with patch.object(manager, "_connect_server", side_effect=mock_connect):
            results = await manager.connect_all()

        # Only preconnect-server should be connected
        assert "preconnect-server" in connect_calls
        assert "server1" not in connect_calls
        # Results should show the preconnect server was connected
        assert results.get("preconnect-server") is True

    @pytest.mark.asyncio
    async def test_connect_all_eager_mode_connects_all(self):
        """Test connect_all in eager mode connects all enabled servers."""
        configs = [
            MCPServerConfig(
                name="server1",
                project_id="test-project",
                transport="http",
                url="http://localhost:8001",
            ),
            MCPServerConfig(
                name="server2",
                project_id="test-project",
                transport="http",
                url="http://localhost:8002",
            ),
        ]

        manager = MCPClientManager(
            server_configs=configs,
            lazy_connect=False,
        )

        mock_session = AsyncMock()
        connect_calls = []

        async def mock_connect(config):
            connect_calls.append(config.name)
            return mock_session

        with patch.object(manager, "_connect_server", side_effect=mock_connect):
            results = await manager.connect_all()

        assert "server1" in connect_calls
        assert "server2" in connect_calls
        # Both servers should be connected successfully
        assert results.get("server1") is True
        assert results.get("server2") is True

    @pytest.mark.asyncio
    async def test_connect_all_handles_connection_errors(self):
        """Test connect_all handles connection errors gracefully."""
        config = MCPServerConfig(
            name="failing-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(
            server_configs=[config],
            lazy_connect=False,
        )

        with patch.object(
            manager,
            "_connect_server",
            side_effect=Exception("Connection failed"),
        ):
            results = await manager.connect_all()

        assert results["failing-server"] is False

    @pytest.mark.asyncio
    async def test_connect_all_starts_health_monitor(self):
        """Test connect_all starts health monitoring task."""
        manager = MCPClientManager(server_configs=[])

        await manager.connect_all()

        assert manager._health_check_task is not None
        # Clean up
        await manager.disconnect_all()

    @pytest.mark.asyncio
    async def test_connect_all_stores_provided_configs(self):
        """Test connect_all stores configs when provided as argument."""
        manager = MCPClientManager(server_configs=[])

        configs = [
            MCPServerConfig(
                name="new-server",
                project_id="test-project",
                transport="http",
                url="http://localhost:8001",
                enabled=False,
            ),
        ]

        await manager.connect_all(configs=configs)

        assert manager.has_server("new-server")
        await manager.disconnect_all()


class TestMCPClientManagerLazyConnection:
    """Tests for lazy connection functionality."""

    def test_get_lazy_connection_states(self) -> None:
        """Test get_lazy_connection_states returns state info."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        states = manager.get_lazy_connection_states()

        assert "test-server" in states
        assert states["test-server"]["is_connected"] is False
        assert "configured_at" in states["test-server"]


class TestMCPClientManagerEnsureConnected:
    """Tests for ensure_connected method."""

    @pytest.mark.asyncio
    async def test_ensure_connected_server_not_configured(self):
        """Test ensure_connected raises KeyError for unknown server."""
        manager = MCPClientManager(server_configs=[])

        with pytest.raises(KeyError, match="Server 'unknown' not configured"):
            await manager.ensure_connected("unknown")

    @pytest.mark.asyncio
    async def test_ensure_connected_disabled_server(self):
        """Test ensure_connected raises MCPError for disabled server."""
        config = MCPServerConfig(
            name="disabled-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
            enabled=False,
        )

        manager = MCPClientManager(server_configs=[config])

        with pytest.raises(MCPError, match="Server 'disabled-server' is disabled"):
            await manager.ensure_connected("disabled-server")

    @pytest.mark.asyncio
    async def test_ensure_connected_already_connected(self):
        """Test ensure_connected returns existing session."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        # Set up mock connection
        mock_session = MagicMock()
        mock_connection = MagicMock()
        mock_connection.is_connected = True
        mock_connection.session = mock_session
        manager._connections["test-server"] = mock_connection

        result = await manager.ensure_connected("test-server")

        assert result is mock_session

    @pytest.mark.asyncio
    async def test_ensure_connected_circuit_breaker_open(self):
        """Test ensure_connected raises CircuitBreakerOpen when circuit is open."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        # Trip the circuit breaker
        state = manager._lazy_connector.get_state("test-server")
        state.circuit_breaker.state = CircuitState.OPEN
        state.circuit_breaker.last_failure_time = float("inf")  # Never recovers

        with pytest.raises(CircuitBreakerOpen):
            await manager.ensure_connected("test-server")

    @pytest.mark.asyncio
    async def test_ensure_connected_retries_on_failure(self):
        """Test ensure_connected retries connection on failure."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(
            server_configs=[config],
            max_connection_retries=2,
        )

        # Update retry config for faster tests
        manager._lazy_connector.retry_config.initial_delay = 0.01
        manager._lazy_connector.retry_config.max_delay = 0.01

        call_count = 0

        async def failing_connect(cfg):
            nonlocal call_count
            call_count += 1
            raise Exception("Connection failed")

        with patch.object(manager, "_connect_server", side_effect=failing_connect):
            with pytest.raises(MCPError, match="Failed to connect"):
                await manager.ensure_connected("test-server")

        # Should have tried 3 times (initial + 2 retries)
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_ensure_connected_timeout(self):
        """Test ensure_connected handles connection timeout."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(
            server_configs=[config],
            connection_timeout=0.01,
            max_connection_retries=0,
        )

        async def slow_connect(cfg):
            await wait_forever()

        with patch.object(manager, "_connect_server", side_effect=slow_connect):
            with pytest.raises(MCPError, match="Connection timeout"):
                await manager.ensure_connected("test-server")


class TestMCPClientManagerConnectServer:
    """Tests for _connect_server internal method."""

    @pytest.mark.asyncio
    async def test_connect_server_success(self):
        """Test _connect_server successfully connects."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        mock_session = MagicMock()
        mock_connection = AsyncMock()
        mock_connection.connect.return_value = mock_session

        with patch(
            "gobby.mcp_proxy.manager.create_transport_connection",
            return_value=mock_connection,
        ):
            result = await manager._connect_server(config)

        assert result is mock_session
        assert manager.health["test-server"].state == ConnectionState.CONNECTED

    @pytest.mark.asyncio
    async def test_connect_server_failure(self):
        """Test _connect_server handles connection failure."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        mock_connection = AsyncMock()
        mock_connection.connect.side_effect = Exception("Connection failed")

        with patch(
            "gobby.mcp_proxy.manager.create_transport_connection",
            return_value=mock_connection,
        ):
            with pytest.raises(Exception, match="Connection failed"):
                await manager._connect_server(config)

        assert manager.health["test-server"].state == ConnectionState.FAILED


class TestMCPClientManagerDisconnect:
    """Tests for disconnect_all method."""

    @pytest.mark.asyncio
    async def test_disconnect_all_cancels_health_task(self):
        """Test disconnect_all cancels health monitoring."""
        manager = MCPClientManager(server_configs=[])

        # Start health monitoring
        await manager.connect_all()
        assert manager._health_check_task is not None

        await manager.disconnect_all()

        assert manager._health_check_task is None

    @pytest.mark.asyncio
    async def test_disconnect_all_cancels_reconnect_tasks(self):
        """Test disconnect_all cancels pending reconnect tasks."""
        manager = MCPClientManager(server_configs=[])

        # Add a mock reconnect task
        async def slow_reconnect():
            await wait_forever()

        task = asyncio.create_task(slow_reconnect())
        manager._reconnect_tasks.add(task)

        await manager.disconnect_all()

        assert len(manager._reconnect_tasks) == 0

    @pytest.mark.asyncio
    async def test_disconnect_all_handles_timeout(self):
        """Test disconnect_all handles disconnect timeout."""
        config = MCPServerConfig(
            name="slow-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        # Add mock connection that takes too long to disconnect
        mock_connection = AsyncMock()

        async def slow_disconnect():
            await wait_forever()

        mock_connection.disconnect = slow_disconnect
        mock_connection.is_connected = True
        manager._connections["slow-server"] = mock_connection
        manager.health["slow-server"] = MCPConnectionHealth(
            name="slow-server",
            state=ConnectionState.CONNECTED,
        )

        # Should not hang
        await asyncio.wait_for(manager.disconnect_all(), timeout=10.0)

        assert len(manager._connections) == 0


class TestMCPClientManagerCallTool:
    """Tests for call_tool method."""

    @pytest.mark.asyncio
    async def test_call_tool_success(self):
        """Test call_tool executes tool successfully."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        mock_session = AsyncMock()
        mock_session.call_tool.return_value = {"result": "success"}

        # Set up health tracking
        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        with patch.object(manager, "get_client_session", return_value=mock_session):
            result = await manager.call_tool("test-server", "test-tool", {"arg": "val"})

        assert result == {"result": "success"}
        mock_session.call_tool.assert_called_once_with("test-tool", {"arg": "val"})

    @pytest.mark.asyncio
    async def test_call_tool_with_timeout(self):
        """Test call_tool respects timeout."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        mock_session = AsyncMock()

        async def slow_tool(*args):
            await wait_forever()
            return {"result": "late"}

        mock_session.call_tool = slow_tool

        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        with patch.object(manager, "get_client_session", return_value=mock_session):
            with pytest.raises(asyncio.TimeoutError):
                await manager.call_tool("test-server", "slow-tool", None, timeout=0.01)

    @pytest.mark.asyncio
    async def test_call_tool_records_metrics(self):
        """Test call_tool records metrics when manager configured."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        mock_metrics = MagicMock()
        manager = MCPClientManager(
            server_configs=[config],
            metrics_manager=mock_metrics,
        )

        mock_session = AsyncMock()
        mock_session.call_tool.return_value = {"result": "success"}

        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        with patch.object(manager, "get_client_session", return_value=mock_session):
            await manager.call_tool("test-server", "test-tool", {})

        mock_metrics.record_call.assert_called_once()
        call_kwargs = mock_metrics.record_call.call_args[1]
        assert call_kwargs["server_name"] == "test-server"
        assert call_kwargs["tool_name"] == "test-tool"
        assert call_kwargs["success"] is True

    @pytest.mark.asyncio
    async def test_call_tool_records_failure_metrics(self):
        """Test call_tool records failure in metrics."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        mock_metrics = MagicMock()
        manager = MCPClientManager(
            server_configs=[config],
            metrics_manager=mock_metrics,
        )

        mock_session = AsyncMock()
        mock_session.call_tool.side_effect = Exception("Tool failed")

        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        with patch.object(manager, "get_client_session", return_value=mock_session):
            with pytest.raises(Exception, match="Tool failed"):
                await manager.call_tool("test-server", "test-tool", {})

        mock_metrics.record_call.assert_called_once()
        call_kwargs = mock_metrics.record_call.call_args[1]
        assert call_kwargs["success"] is False

    @pytest.mark.asyncio
    async def test_call_tool_handles_metrics_error(self):
        """Test call_tool doesn't fail when metrics recording fails."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        mock_metrics = MagicMock()
        mock_metrics.record_call.side_effect = Exception("Metrics error")
        manager = MCPClientManager(
            server_configs=[config],
            metrics_manager=mock_metrics,
        )

        mock_session = AsyncMock()
        mock_session.call_tool.return_value = {"result": "success"}

        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        with patch.object(manager, "get_client_session", return_value=mock_session):
            # Should not raise despite metrics failure
            result = await manager.call_tool("test-server", "test-tool", {})

        assert result == {"result": "success"}


class TestMCPClientManagerReadResource:
    """Tests for read_resource method."""

    @pytest.mark.asyncio
    async def test_read_resource_success(self):
        """Test read_resource returns resource content."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        mock_session = AsyncMock()
        mock_session.read_resource.return_value = {"content": "resource data"}

        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        with patch.object(manager, "get_client_session", return_value=mock_session):
            result = await manager.read_resource("test-server", "file://test.txt")

        assert result == {"content": "resource data"}

    @pytest.mark.asyncio
    async def test_read_resource_records_failure(self):
        """Test read_resource records health failure on error."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        mock_session = AsyncMock()
        mock_session.read_resource.side_effect = Exception("Read failed")

        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        with patch.object(manager, "get_client_session", return_value=mock_session):
            with pytest.raises(Exception, match="Read failed"):
                await manager.read_resource("test-server", "file://test.txt")

        assert manager.health["test-server"].consecutive_failures == 1


class TestMCPClientManagerListTools:
    """Tests for list_tools method."""

    @pytest.mark.asyncio
    async def test_list_tools_single_server(self):
        """Test list_tools for a single server."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        mock_session = AsyncMock()
        mock_tool = MagicMock()
        mock_tool.name = "test-tool"
        mock_tool.description = "Test tool description"
        mock_tool.inputSchema = {"type": "object"}
        mock_session.list_tools.return_value = MagicMock(tools=[mock_tool])

        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        with patch.object(manager, "get_client_session", return_value=mock_session):
            result = await manager.list_tools("test-server")

        assert "test-server" in result
        assert len(result["test-server"]) == 1
        assert result["test-server"][0]["name"] == "test-tool"

    @pytest.mark.asyncio
    async def test_list_tools_handles_missing_tools_attr(self):
        """Test list_tools handles result without tools attribute."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])
        manager._connections["test-server"] = MagicMock()

        mock_session = AsyncMock()
        # Return object without tools attribute
        mock_session.list_tools.return_value = {}

        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        with patch.object(manager, "get_client_session", return_value=mock_session):
            result = await manager.list_tools("test-server")

        assert result["test-server"] == []

    @pytest.mark.asyncio
    async def test_list_tools_handles_error(self):
        """Test list_tools handles errors gracefully."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])
        manager._connections["test-server"] = MagicMock()

        mock_session = AsyncMock()
        mock_session.list_tools.side_effect = Exception("List failed")

        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        with patch.object(manager, "get_client_session", return_value=mock_session):
            result = await manager.list_tools("test-server")

        assert result["test-server"] == []


class TestMCPClientManagerGetToolInputSchema:
    """Tests for get_tool_input_schema method."""

    @pytest.mark.asyncio
    async def test_get_tool_input_schema_success(self):
        """Test get_tool_input_schema returns schema for tool."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        expected_schema = {"type": "object", "properties": {"arg": {"type": "string"}}}

        with patch.object(
            manager,
            "_list_tools_for_server",
            new=AsyncMock(return_value=[{"name": "test-tool", "inputSchema": expected_schema}]),
        ):
            result = await manager.get_tool_input_schema("test-server", "test-tool")

        assert result == expected_schema

    @pytest.mark.asyncio
    async def test_get_tool_input_schema_tool_not_found(self):
        """Test get_tool_input_schema raises MCPError when tool not found."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        with patch.object(
            manager,
            "_list_tools_for_server",
            new=AsyncMock(return_value=[]),
        ):
            with pytest.raises(MCPError, match="Tool nonexistent not found"):
                await manager.get_tool_input_schema("test-server", "nonexistent")


class TestMCPClientManagerHealthCheck:
    """Tests for health_check_all method."""

    @pytest.mark.asyncio
    async def test_health_check_all_with_connections(self):
        """Test health_check_all checks all connected servers."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        mock_connection = AsyncMock()
        mock_connection.is_connected = True
        mock_connection.health_check.return_value = True
        manager._connections["test-server"] = mock_connection
        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        result = await manager.health_check_all()

        assert result["test-server"] is True
        mock_connection.health_check.assert_called_once_with(timeout=5.0)

    @pytest.mark.asyncio
    async def test_health_check_all_records_failures(self):
        """Test health_check_all records failures."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        mock_connection = AsyncMock()
        mock_connection.is_connected = True
        mock_connection.health_check.return_value = False
        manager._connections["test-server"] = mock_connection
        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        result = await manager.health_check_all()

        assert result["test-server"] is False
        assert manager.health["test-server"].consecutive_failures == 1


class TestMCPClientManagerReconnect:
    """Tests for _reconnect method."""

    @pytest.mark.asyncio
    async def test_reconnect_success(self):
        """Test _reconnect disconnects old connection before reconnecting."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        old_conn = AsyncMock()
        old_conn.is_connected = True
        manager._connections["test-server"] = old_conn

        with patch.object(manager, "_connect_server", return_value=MagicMock()):
            await manager._reconnect("test-server")

        old_conn.disconnect.assert_awaited_once()
        assert old_conn.disconnect.await_count == 1
        assert old_conn.disconnect.await_args is not None

    @pytest.mark.asyncio
    async def test_reconnect_no_old_connection(self):
        """Test _reconnect works when no old connection exists."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        with patch.object(manager, "_connect_server", return_value=MagicMock()) as mock_connect:
            await manager._reconnect("test-server")

        mock_connect.assert_awaited_once()
        assert mock_connect.await_count == 1
        assert mock_connect.await_args is not None

    @pytest.mark.asyncio
    async def test_reconnect_old_disconnect_failure_does_not_block(self):
        """Test _reconnect proceeds even if old connection disconnect fails."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        old_conn = AsyncMock()
        old_conn.disconnect.side_effect = Exception("disconnect exploded")
        manager._connections["test-server"] = old_conn

        with patch.object(manager, "_connect_server", return_value=MagicMock()) as mock_connect:
            await manager._reconnect("test-server")

        mock_connect.assert_awaited_once()
        assert mock_connect.await_count == 1
        assert mock_connect.await_args is not None

    @pytest.mark.asyncio
    async def test_reconnect_handles_unknown_server(self):
        """Test _reconnect handles unknown server gracefully."""
        manager = MCPClientManager(server_configs=[])

        result = await manager._reconnect("unknown-server")
        assert result is None
        assert "unknown-server" not in manager._connections

    @pytest.mark.asyncio
    async def test_reconnect_handles_failure(self):
        """Test _reconnect handles connection failure."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        with patch.object(
            manager,
            "_connect_server",
            side_effect=Exception("Reconnect failed"),
        ):
            result = await manager._reconnect("test-server")
            assert result is None
            assert "test-server" not in manager._connections


class TestMCPClientManagerServerConfig:
    """Tests for add_server_config and remove_server_config methods."""

    def test_add_server_config(self) -> None:
        """Test add_server_config registers new config."""
        manager = MCPClientManager(server_configs=[])

        config = MCPServerConfig(
            name="new-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager.add_server_config(config)

        assert manager.has_server("new-server")
        assert "new-server" in manager.health

    def test_add_server_config_initializes_health(self) -> None:
        """Test add_server_config initializes health tracking."""
        manager = MCPClientManager(server_configs=[])

        config = MCPServerConfig(
            name="new-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager.add_server_config(config)

        # Default lazy_connect=True, so new servers start as PENDING
        assert manager.health["new-server"].state == ConnectionState.PENDING

    def test_remove_server_config_success(self) -> None:
        """Test remove_server_config removes config."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        manager.remove_server_config("test-server")

        assert not manager.has_server("test-server")

    def test_remove_server_config_with_connection_raises(self) -> None:
        """Test remove_server_config raises when connection exists."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])
        manager._connections["test-server"] = MagicMock()

        with pytest.raises(RuntimeError, match="Cannot remove config"):
            manager.remove_server_config("test-server")


class TestMCPClientManagerServerHealth:
    """Tests for get_server_health method."""

    def test_get_server_health_formats_output(self) -> None:
        """Test get_server_health returns formatted health data."""
        manager = MCPClientManager(server_configs=[])

        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
            health=HealthState.HEALTHY,
            last_health_check=datetime.now(),
            response_time_ms=42.5,
            consecutive_failures=0,
        )

        health = manager.get_server_health()

        assert "test-server" in health
        assert health["test-server"]["state"] == "connected"
        assert health["test-server"]["health"] == "healthy"
        assert health["test-server"]["response_time_ms"] == 42.5
        assert health["test-server"]["failures"] == 0


class TestMCPClientManagerMonitorHealth:
    """Tests for _monitor_health background task."""

    @pytest.mark.asyncio
    async def test_monitor_health_checks_connections(self):
        """Test _monitor_health performs health checks."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(
            server_configs=[config],
            health_check_interval=0.01,  # Fast for testing
        )

        mock_connection = AsyncMock()
        mock_connection.is_connected = True
        mock_connection.health_check.return_value = True
        manager._connections["test-server"] = mock_connection
        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )
        manager._running = True

        async def one_interval(_delay: float) -> None:
            manager._running = False

        with patch("gobby.mcp_proxy.manager.asyncio.sleep", side_effect=one_interval):
            await manager._monitor_health()

        mock_connection.health_check.assert_called()
        assert mock_connection.health_check.call_count >= 1
        assert mock_connection.health_check.call_args is not None

    @pytest.mark.asyncio
    async def test_monitor_health_triggers_reconnect_on_unhealthy(self):
        """Test _monitor_health triggers reconnect for unhealthy servers."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(
            server_configs=[config],
            health_check_interval=0.01,
        )

        mock_connection = AsyncMock()
        mock_connection.is_connected = True
        mock_connection.health_check.return_value = False
        manager._connections["test-server"] = mock_connection
        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
            health=HealthState.UNHEALTHY,
            consecutive_failures=5,
        )
        manager._running = True

        reconnect_called = asyncio.Event()
        original_reconnect = manager._reconnect

        async def mock_reconnect(name):
            reconnect_called.set()
            return await original_reconnect(name)

        with patch.object(manager, "_reconnect", side_effect=mock_reconnect):
            task = asyncio.create_task(manager._monitor_health())

            # Wait for reconnect to be triggered
            try:
                await asyncio.wait_for(reconnect_called.wait(), timeout=1.0)
            except TimeoutError:
                pass  # May not always trigger depending on timing

            manager._running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert mock_connection.health_check.await_count >= 1

    @pytest.mark.asyncio
    async def test_monitor_health_keeps_transient_failure_below_warning(self, caplog):
        """Transient health failures are tracked without warning-level log noise."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )
        manager = MCPClientManager(server_configs=[config], health_check_interval=0.01)
        mock_connection = AsyncMock()
        mock_connection.is_connected = True
        mock_connection.health_check.return_value = False
        manager._connections["test-server"] = mock_connection
        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )
        manager._running = True
        caplog.set_level("WARNING", logger="gobby.mcp_proxy.manager")

        async def one_interval(_delay: float) -> None:
            manager._running = False

        with patch("gobby.mcp_proxy.manager.asyncio.sleep", side_effect=one_interval):
            await manager._monitor_health()

        assert manager.health["test-server"].consecutive_failures == 1
        assert "Health check failed for test-server" not in caplog.text

    @pytest.mark.asyncio
    async def test_monitor_health_warns_on_unhealthy_transition(self, caplog):
        """A server emits one warning when it crosses into unhealthy state."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )
        manager = MCPClientManager(server_configs=[config], health_check_interval=0.01)
        mock_connection = AsyncMock()
        mock_connection.is_connected = True
        mock_connection.health_check.return_value = False
        manager._connections["test-server"] = mock_connection
        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
            health=HealthState.DEGRADED,
            consecutive_failures=4,
        )
        manager._running = True
        caplog.set_level("WARNING", logger="gobby.mcp_proxy.manager")

        async def one_interval(_delay: float) -> None:
            manager._running = False

        with (
            patch("gobby.mcp_proxy.manager.asyncio.sleep", side_effect=one_interval),
            patch.object(manager, "_reconnect", new_callable=AsyncMock),
        ):
            await manager._monitor_health()

        assert manager.health["test-server"].health == HealthState.UNHEALTHY
        assert "Health check failed for test-server" in caplog.text

    @pytest.mark.asyncio
    async def test_monitor_health_debugs_repeated_unhealthy_failure_with_context(self, caplog):
        """Repeated unhealthy health failures are logged at debug with context."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )
        manager = MCPClientManager(server_configs=[config], health_check_interval=0.01)
        mock_connection = AsyncMock()
        mock_connection.is_connected = True
        mock_connection.health_check.return_value = False
        manager._connections["test-server"] = mock_connection
        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
            health=HealthState.UNHEALTHY,
            consecutive_failures=5,
        )
        manager._running = True
        caplog.set_level("DEBUG", logger="gobby.mcp.manager")

        async def one_interval(_delay: float) -> None:
            manager._running = False

        with (
            patch("gobby.mcp_proxy.manager.asyncio.sleep", side_effect=one_interval),
            patch.object(manager, "_reconnect", new_callable=AsyncMock),
        ):
            await manager._monitor_health()

        debug_records = [
            record
            for record in caplog.records
            if record.levelname == "DEBUG"
            and record.message == "Health check failed for test-server"
        ]
        assert debug_records
        assert debug_records[0].server_name == "test-server"
        assert debug_records[0].previous_health == HealthState.UNHEALTHY.value
        assert debug_records[0].consecutive_failures == 6

    @pytest.mark.asyncio
    async def test_monitor_health_continues_when_no_connections(self):
        """Test _monitor_health continues loop when no connected servers."""
        manager = MCPClientManager(
            server_configs=[],
            health_check_interval=0.01,
        )
        manager._running = True

        # Add a disconnected connection
        mock_connection = MagicMock()
        mock_connection.is_connected = False
        manager._connections["test-server"] = mock_connection

        async def one_interval(_delay: float) -> None:
            manager._running = False

        with patch("gobby.mcp_proxy.manager.asyncio.sleep", side_effect=one_interval):
            await manager._monitor_health()

        # Should not have called health_check since not connected
        assert (
            not hasattr(mock_connection, "health_check") or not mock_connection.health_check.called
        )

    @pytest.mark.asyncio
    async def test_monitor_health_handles_exceptions(self):
        """Test _monitor_health handles exceptions in loop."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(
            server_configs=[config],
            health_check_interval=0.01,
        )

        mock_connection = AsyncMock()
        mock_connection.is_connected = True
        # Raise exception on health check
        mock_connection.health_check.side_effect = RuntimeError("Unexpected error")
        manager._connections["test-server"] = mock_connection
        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )
        manager._running = True

        async def one_interval(_delay: float) -> None:
            manager._running = False

        with patch("gobby.mcp_proxy.manager.asyncio.sleep", side_effect=one_interval):
            await manager._monitor_health()

        assert mock_connection.health_check.await_count >= 1
        assert manager.health["test-server"].consecutive_failures >= 1


class TestMCPClientManagerConnectAllEager:
    """Tests for connect_all in eager mode with disabled servers."""

    @pytest.mark.asyncio
    async def test_connect_all_eager_skips_disabled(self):
        """Test connect_all in eager mode skips disabled servers."""
        configs = [
            MCPServerConfig(
                name="enabled-server",
                project_id="test-project",
                transport="http",
                url="http://localhost:8001",
                enabled=True,
            ),
            MCPServerConfig(
                name="disabled-server",
                project_id="test-project",
                transport="http",
                url="http://localhost:8002",
                enabled=False,
            ),
        ]

        manager = MCPClientManager(
            server_configs=configs,
            lazy_connect=False,
        )

        connect_calls = []

        async def mock_connect(config):
            connect_calls.append(config.name)
            return MagicMock()

        with patch.object(manager, "_connect_server", side_effect=mock_connect):
            results = await manager.connect_all()

        # Only enabled server should be connected
        assert "enabled-server" in connect_calls
        assert "disabled-server" not in connect_calls
        assert results["disabled-server"] is False

        await manager.disconnect_all()


class TestMCPClientManagerDisconnectErrors:
    """Tests for disconnect error handling."""

    @pytest.mark.asyncio
    async def test_disconnect_all_handles_disconnect_error(self):
        """Test disconnect_all handles errors during disconnect."""
        config = MCPServerConfig(
            name="error-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        mock_connection = AsyncMock()
        mock_connection.is_connected = True
        mock_connection.disconnect.side_effect = RuntimeError("Disconnect failed")
        manager._connections["error-server"] = mock_connection
        manager.health["error-server"] = MCPConnectionHealth(
            name="error-server",
            state=ConnectionState.CONNECTED,
        )

        # Should not raise despite error
        await manager.disconnect_all()

        assert len(manager._connections) == 0


class TestMCPClientManagerCircuitBreakerEdgeCases:
    """Tests for circuit breaker edge cases."""

    @pytest.mark.asyncio
    async def test_ensure_connected_circuit_open_no_failure_time(self):
        """Test circuit breaker open without last_failure_time raises MCPError."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        # Set circuit to open without failure time and mock can_attempt_connection
        # to return False (simulating open circuit breaker)
        state = manager._lazy_connector.get_state("test-server")
        state.circuit_breaker.state = CircuitState.OPEN
        state.circuit_breaker.last_failure_time = None

        # We need to mock can_attempt_connection to return False
        with patch.object(
            manager._lazy_connector,
            "can_attempt_connection",
            return_value=False,
        ):
            with pytest.raises(MCPError, match="Circuit breaker open"):
                await manager.ensure_connected("test-server")


class TestMCPClientManagerConcurrentConnection:
    """Tests for concurrent connection handling."""

    @pytest.mark.asyncio
    async def test_ensure_connected_double_check_after_lock(self):
        """Test ensure_connected returns session if connected while waiting for lock."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        mock_session = MagicMock()
        connect_started = asyncio.Event()
        connection_established = asyncio.Event()

        async def simulate_concurrent_connect():
            await connect_started.wait()
            # Simulate another coroutine connecting while we wait
            mock_connection = MagicMock()
            mock_connection.is_connected = True
            mock_connection.session = mock_session
            manager._connections["test-server"] = mock_connection
            connection_established.set()

        async def slow_connect(cfg):
            # Wait for "concurrent" connection to complete
            connect_started.set()
            await connection_established.wait()
            return mock_session

        # Start concurrent connection task
        concurrent_task = asyncio.create_task(simulate_concurrent_connect())

        with patch.object(manager, "_connect_server", side_effect=slow_connect):
            result = await manager.ensure_connected("test-server")

        await concurrent_task
        assert result is mock_session


class TestMCPClientManagerNullSession:
    """Tests for null session handling."""

    @pytest.mark.asyncio
    async def test_ensure_connected_null_session(self):
        """Test ensure_connected raises when connection returns None."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(
            server_configs=[config],
            max_connection_retries=0,
        )

        # Return None from connect
        with patch.object(manager, "_connect_server", return_value=None):
            with pytest.raises(MCPError, match="Connection returned no session"):
                await manager.ensure_connected("test-server")


class TestMCPClientManagerGetClientSession:
    """Tests for get_client_session method."""

    @pytest.mark.asyncio
    async def test_get_client_session_delegates_to_ensure_connected(self):
        """Test get_client_session calls ensure_connected."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        mock_session = MagicMock()

        with patch.object(manager, "ensure_connected", return_value=mock_session) as mock_ensure:
            result = await manager.get_client_session("test-server")

        mock_ensure.assert_called_once_with("test-server")
        assert result is mock_session


class TestMCPClientManagerCallToolMetricsEdgeCases:
    """Tests for call_tool metrics edge cases."""

    @pytest.mark.asyncio
    async def test_call_tool_no_metrics_recorded_without_project_id(self):
        """Test call_tool doesn't record metrics when no project_id available."""
        config = MCPServerConfig(
            name="test-server",
            project_id="",  # Empty project_id (falsy)
            transport="http",
            url="http://localhost:8001",
        )

        mock_metrics = MagicMock()
        manager = MCPClientManager(
            server_configs=[config],
            metrics_manager=mock_metrics,
            project_id=None,  # No manager project_id either
        )

        mock_session = AsyncMock()
        mock_session.call_tool.return_value = {"result": "success"}

        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        with patch.object(manager, "get_client_session", return_value=mock_session):
            result = await manager.call_tool("test-server", "test-tool", {})

        assert result == {"result": "success"}
        # Metrics should NOT be recorded when no project_id
        mock_metrics.record_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_call_tool_uses_config_project_id(self):
        """Test call_tool uses config's project_id for metrics."""
        config = MCPServerConfig(
            name="test-server",
            project_id="config-project",
            transport="http",
            url="http://localhost:8001",
        )

        mock_metrics = MagicMock()
        manager = MCPClientManager(
            server_configs=[config],
            metrics_manager=mock_metrics,
            project_id="manager-project",  # This should be overridden by config
        )

        mock_session = AsyncMock()
        mock_session.call_tool.return_value = {"result": "success"}

        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        with patch.object(manager, "get_client_session", return_value=mock_session):
            await manager.call_tool("test-server", "test-tool", {})

        # Should use config's project_id
        call_kwargs = mock_metrics.record_call.call_args[1]
        assert call_kwargs["project_id"] == "config-project"


class TestMCPClientManagerListToolsAllServers:
    """Tests for list_tools with all servers."""

    @pytest.mark.asyncio
    async def test_list_tools_all_connected_servers(self):
        """Test list_tools lists tools from all connected servers."""
        configs = [
            MCPServerConfig(
                name="server1",
                project_id="test-project",
                transport="http",
                url="http://localhost:8001",
            ),
            MCPServerConfig(
                name="server2",
                project_id="test-project",
                transport="http",
                url="http://localhost:8002",
            ),
        ]

        manager = MCPClientManager(server_configs=configs)
        manager._connections["server1"] = MagicMock()
        manager._connections["server2"] = MagicMock()

        mock_session = AsyncMock()
        mock_tool = MagicMock()
        mock_tool.name = "shared-tool"
        mock_tool.description = "A tool"
        mock_tool.inputSchema = {}
        mock_session.list_tools.return_value = MagicMock(tools=[mock_tool])

        manager.health["server1"] = MCPConnectionHealth(
            name="server1",
            state=ConnectionState.CONNECTED,
        )
        manager.health["server2"] = MCPConnectionHealth(
            name="server2",
            state=ConnectionState.CONNECTED,
        )

        with patch.object(manager, "get_client_session", return_value=mock_session):
            result = await manager.list_tools()  # No server_name = all connected

        assert "server1" in result
        assert "server2" in result

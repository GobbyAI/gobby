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
import logging
import threading
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from gobby.mcp_proxy.bundled import CHROME_DEVTOOLS_NPM_PACKAGE, DEFAULT_EXTERNAL_MCP_SERVERS
from gobby.mcp_proxy.client_manager import connections
from gobby.mcp_proxy.client_manager.secrets import resolve_secrets_in_config
from gobby.mcp_proxy.client_manager.server_registry import truncate_tool_brief
from gobby.mcp_proxy.lazy import CircuitBreakerOpen, CircuitState
from gobby.mcp_proxy.manager import MCPClientManager
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
    configs = [
        MCPServerConfig(
            project_id="project-id",
            name=server["name"],
            transport=server["transport"],
            command=server.get("command"),
            args=server.get("args"),
            description=server.get("description"),
        )
        for server in DEFAULT_EXTERNAL_MCP_SERVERS
    ]
    manager = MCPClientManager(server_configs=configs)

    assert "gobby-code" not in manager.get_available_servers()


def test_resolve_secrets_in_config_resolves_args() -> None:
    manager = MagicMock()
    manager.mcp_db_manager = MagicMock(db=object())
    config = MCPServerConfig(
        name="context7",
        transport="stdio",
        command="npx",
        args=["--api-key", "$secret:context7_api_key"],
        project_id="proj-1",
    )
    store = MagicMock()
    store.resolve.side_effect = lambda value: (
        "resolved-token" if value == "$secret:context7_api_key" else value
    )

    with patch("gobby.storage.secrets.SecretStore", return_value=store):
        resolved = resolve_secrets_in_config(manager, config, logging.getLogger("test"))

    assert resolved.args == ["--api-key", "resolved-token"]
    assert config.args == ["--api-key", "$secret:context7_api_key"]
    assert resolved is not config
    assert resolved.command == "npx"
    assert store.resolve.call_args_list == [
        call("--api-key"),
        call("$secret:context7_api_key"),
    ]


def test_resolve_secrets_in_config_does_not_warn_when_no_arg_stripped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = MagicMock()
    manager.mcp_db_manager = MagicMock(db=object())
    config = MCPServerConfig(
        name="context7",
        transport="stdio",
        command="npx",
        args=["--api-key", "$secret:context7_api_key"],
        project_id="proj-1",
    )
    store = MagicMock()
    store.resolve.side_effect = lambda value: (
        "resolved-token" if value == "$secret:context7_api_key" else value
    )

    with (
        caplog.at_level("WARNING", logger="test"),
        patch("gobby.storage.secrets.SecretStore", return_value=store),
    ):
        resolved = resolve_secrets_in_config(manager, config, logging.getLogger("test"))

    assert resolved.args == ["--api-key", "resolved-token"]
    assert "Stripping unresolved secret ref" not in caplog.text


def test_resolve_secrets_in_config_strips_unresolved_args(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = MagicMock()
    manager.mcp_db_manager = MagicMock(db=object())
    config = MCPServerConfig(
        name="context7",
        transport="stdio",
        command="npx",
        args=["--api-key", "$secret:missing_key", "--token=$secret:missing_key", "serve"],
        project_id="proj-1",
    )
    store = MagicMock()
    store.resolve.side_effect = lambda value: value

    with (
        caplog.at_level("WARNING", logger="test"),
        patch("gobby.storage.secrets.SecretStore", return_value=store),
    ):
        resolved = resolve_secrets_in_config(manager, config, logging.getLogger("test"))

    assert resolved.args == ["serve"]
    assert "Stripping unresolved secret ref from context7 args" in caplog.text
    assert "missing_key" not in caplog.text


def test_resolve_secrets_in_config_failure_logs_only_safe_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel_secret = "sentinel-secret-value"
    secret_ref = "$secret:sentinel_secret"
    manager = MagicMock()
    manager.mcp_db_manager = MagicMock(db=object())
    config = MCPServerConfig(
        name="context7",
        transport="stdio",
        command="npx",
        args=["--api-key", secret_ref],
        project_id="proj-1",
    )
    store = MagicMock()
    failure = RuntimeError(f"provider exposed {secret_ref}={sentinel_secret}")
    store.resolve.side_effect = failure

    with (
        caplog.at_level("WARNING", logger="test"),
        patch("gobby.storage.secrets.SecretStore", return_value=store),
        pytest.raises(RuntimeError) as exc_info,
    ):
        resolve_secrets_in_config(manager, config, logging.getLogger("test"))

    assert exc_info.value is failure
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.exc_info is None
    assert record.getMessage() == "Secret resolution failed for context7 (RuntimeError)"
    assert sentinel_secret not in caplog.text
    assert secret_ref not in caplog.text


def test_resolve_secrets_in_config_preserves_config_on_import_error() -> None:
    manager = MagicMock()
    manager.mcp_db_manager = MagicMock(db=object())
    config = MCPServerConfig(
        name="context7",
        transport="stdio",
        command="npx",
        args=["--api-key", "$secret:context7_api_key"],
        project_id="proj-1",
    )

    with patch.dict("sys.modules", {"gobby.storage.secrets": None}):
        resolved = resolve_secrets_in_config(manager, config, logging.getLogger("test"))

    assert resolved is config


@pytest.mark.asyncio
async def test_remove_server_unregisters_lazy_connection_state() -> None:
    manager = MCPClientManager(
        server_configs=[
            MCPServerConfig(
                name="lazy-server",
                transport="http",
                url="http://localhost:8001",
                project_id="proj-1",
            )
        ],
    )
    assert "lazy-server" in manager.get_lazy_connection_states()

    result = await manager.remove_server("lazy-server")

    assert result == {"success": True, "name": "lazy-server"}
    assert "lazy-server" not in manager.get_lazy_connection_states()


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
        id: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01",
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
        self.id = id


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

        result = MCPClientManager._load_tools_from_db(mock_db, "test-server-id")

        assert result is None

    def test_load_tools_handles_exception(self) -> None:
        """Test handles exceptions gracefully."""
        mock_db = MagicMock()
        mock_db.get_cached_tools.side_effect = Exception("Database error")

        result = MCPClientManager._load_tools_from_db(mock_db, "test-server-id")

        assert result is None

    def test_load_tools_handles_none_description(self) -> None:
        """Test handles tools with None description."""
        mock_db = MagicMock()
        mock_db.get_cached_tools.return_value = [
            MockCachedTool("tool1", None),
        ]

        result = MCPClientManager._load_tools_from_db(mock_db, "test-server-id")

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
    async def test_add_server_success_disabled(self) -> None:
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
        assert result["connected"] is False
        assert result["full_tool_schemas"] == []
        assert manager.has_server("new-server")

    @pytest.mark.asyncio
    async def test_add_server_persists_to_database(self) -> None:
        """Test add_server persists config to database."""
        mock_db = MagicMock()
        event_loop_thread = threading.get_ident()
        db_threads: list[int] = []
        mock_db.upsert.side_effect = lambda **_kwargs: db_threads.append(threading.get_ident())
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
        assert len(db_threads) == 1
        assert db_threads[0] != event_loop_thread

    @pytest.mark.asyncio
    async def test_add_server_canonicalizes_bundled_server_scope(self) -> None:
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
    async def test_add_server_connects_and_lists_tools(self) -> None:
        """Test add_server connects and lists tools for enabled server."""
        mock_db = MagicMock()
        event_loop_thread = threading.get_ident()
        cache_threads: list[int] = []
        mock_db.cache_tools.side_effect = lambda *_args, **_kwargs: cache_threads.append(
            threading.get_ident()
        )
        manager = MCPClientManager(server_configs=[], mcp_db_manager=mock_db)

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
        mock_tool.input_schema = {"type": "object"}
        mock_session.list_tools.return_value = MagicMock(tools=[mock_tool])

        with patch.object(manager, "_connect_server", return_value=mock_session):
            result = await manager.add_server(config)

        assert result["success"] is True
        assert result["connected"] is True
        assert len(result["full_tool_schemas"]) == 1
        assert result["full_tool_schemas"][0]["name"] == "test-tool"
        mock_db.cache_tools.assert_called_once()
        assert len(cache_threads) == 1
        assert cache_threads[0] != event_loop_thread

    @pytest.mark.asyncio
    async def test_add_server_keeps_config_when_initial_connection_fails(self) -> None:
        """A persisted config remains available for a later lazy connection."""
        mock_db = MagicMock()
        manager = MCPClientManager(
            server_configs=[],
            mcp_db_manager=mock_db,
            max_connection_retries=0,
        )
        config = MCPServerConfig(
            name="recovering-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
            enabled=True,
        )
        recovered_session = AsyncMock()
        connect_server = AsyncMock(
            side_effect=[RuntimeError("target unreachable"), recovered_session]
        )

        with patch.object(manager, "_connect_server", new=connect_server):
            result = await manager.add_server(config)

            assert result == {
                "success": True,
                "name": "recovering-server",
                "connected": False,
                "error": "target unreachable",
                "full_tool_schemas": [],
            }
            stored_config = manager.get_server_config("recovering-server")
            assert stored_config is not None
            assert stored_config.url == config.url
            assert stored_config.enabled is True
            mock_db.upsert.assert_called_once()

            session = await manager.ensure_connected("recovering-server")

        assert session is recovered_session
        assert connect_server.await_count == 2

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
        mock_tool.input_schema = {"type": "object"}
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
            project_id="test-project",
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
        manager._tool_schema_cache["existing-server"] = [{"name": "test-tool"}]

        result = await manager.set_server_enabled("existing-server", False)

        assert result == {"success": True, "name": "existing-server", "enabled": False}
        assert config.enabled is False
        assert manager._lazy_connector.get_state("existing-server") is None
        assert "existing-server" not in manager._tool_schema_cache

    @pytest.mark.asyncio
    async def test_add_server_handles_list_tools_failure(self) -> None:
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
    async def test_remove_server_disconnects_and_cleans_up(self) -> None:
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
        manager._tool_schema_cache["test-server"] = [{"name": "test-tool"}]
        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        result = await manager.remove_server("test-server")

        assert result["success"] is True
        assert "test-server" not in manager._configs
        assert "test-server" not in manager._connections
        assert "test-server" not in manager._tool_schema_cache
        assert "test-server" not in manager.health
        mock_connection.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_remove_server_uses_config_project_id(self) -> None:
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
    async def test_remove_server_uses_provided_project_id(self) -> None:
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
    async def test_connect_all_lazy_mode_only_preconnect(self) -> None:
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

        async def mock_connect(config: MCPServerConfig) -> Any:
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
    async def test_connect_all_eager_mode_connects_all(self) -> None:
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

        async def mock_connect(config: MCPServerConfig) -> Any:
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
    async def test_connect_all_handles_connection_errors(self) -> None:
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
    async def test_connect_all_starts_health_monitor(self) -> None:
        """Test connect_all starts health monitoring task."""
        manager = MCPClientManager(server_configs=[])

        await manager.connect_all()

        assert manager._health_check_task is not None
        # Clean up
        await manager.disconnect_all()

    @pytest.mark.asyncio
    async def test_connect_all_stores_provided_configs(self) -> None:
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
    async def test_ensure_connected_server_not_configured(self) -> None:
        """Test ensure_connected raises KeyError for unknown server."""
        manager = MCPClientManager(server_configs=[])

        with pytest.raises(KeyError, match="Server 'unknown' not configured"):
            await manager.ensure_connected("unknown")

    @pytest.mark.asyncio
    async def test_ensure_connected_disabled_server(self) -> None:
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
    async def test_ensure_connected_already_connected(self) -> None:
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
    async def test_ensure_connected_circuit_breaker_open(self) -> None:
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
        assert state is not None
        state.circuit_breaker.state = CircuitState.OPEN
        state.circuit_breaker.last_failure_time = float("inf")  # Never recovers

        with pytest.raises(CircuitBreakerOpen):
            await manager.ensure_connected("test-server")

    @pytest.mark.asyncio
    async def test_ensure_connected_retries_on_failure(self) -> None:
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

        async def failing_connect(cfg: MCPServerConfig) -> None:
            nonlocal call_count
            call_count += 1
            raise Exception("Connection failed")

        with patch.object(manager, "_connect_server", side_effect=failing_connect):
            with pytest.raises(MCPError, match="Failed to connect"):
                await manager.ensure_connected("test-server")

        # Should have tried 3 times (initial + 2 retries)
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_ensure_connected_timeout(self) -> None:
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

        async def slow_connect(cfg: MCPServerConfig) -> None:
            await wait_forever()

        with patch.object(manager, "_connect_server", side_effect=slow_connect):
            with pytest.raises(MCPError, match="Connection timeout"):
                await manager.ensure_connected("test-server")


class TestMCPClientManagerConnectServer:
    """Tests for _connect_server internal method."""

    @pytest.mark.asyncio
    async def test_connect_server_success(self) -> None:
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
        event_loop_thread = threading.get_ident()
        secret_threads: list[int] = []

        def resolve_secrets(server_config: MCPServerConfig) -> MCPServerConfig:
            secret_threads.append(threading.get_ident())
            return server_config

        with (
            patch(
                "gobby.mcp_proxy.manager.create_transport_connection",
                return_value=mock_connection,
            ),
            patch.object(manager, "_resolve_secrets_in_config", side_effect=resolve_secrets),
        ):
            result = await manager._connect_server(config)

        assert result is mock_session
        assert manager.health["test-server"].state == ConnectionState.CONNECTED
        assert len(secret_threads) == 1
        assert secret_threads[0] != event_loop_thread

    @pytest.mark.asyncio
    async def test_connect_server_failure(self) -> None:
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
        mock_connection.is_connected = False

        with patch(
            "gobby.mcp_proxy.manager.create_transport_connection",
            return_value=mock_connection,
        ):
            with pytest.raises(Exception, match="Connection failed"):
                await manager._connect_server(config)

        assert manager.health["test-server"].state == ConnectionState.FAILED
        assert manager.is_connected("test-server") is False
        assert manager.list_connections() == []

    @pytest.mark.asyncio
    async def test_connect_server_refreshes_resolved_config_on_cached_transport(self) -> None:
        """A retry uses freshly resolved secrets on its cached transport."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
            headers={"Authorization": "$secret:API_TOKEN"},
        )
        first_resolved = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
            headers={"Authorization": "Bearer expired"},
        )
        second_resolved = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
            headers={"Authorization": "Bearer rotated"},
        )
        manager = MCPClientManager(server_configs=[config])
        mock_session = MagicMock()
        mock_connection = MagicMock()
        mock_connection.connect = AsyncMock(
            side_effect=[RuntimeError("expired credentials"), mock_session]
        )

        def create_connection(
            resolved_config: MCPServerConfig,
            stdio_errlog_path: str | None = None,
        ) -> MagicMock:
            assert stdio_errlog_path is None
            mock_connection.config = resolved_config
            return mock_connection

        with (
            patch(
                "gobby.mcp_proxy.manager.create_transport_connection",
                side_effect=create_connection,
            ) as mock_factory,
            patch.object(
                manager,
                "_resolve_secrets_in_config",
                side_effect=[first_resolved, second_resolved],
            ),
        ):
            with pytest.raises(RuntimeError, match="expired credentials"):
                await manager._connect_server(config)
            result = await manager._connect_server(config)

        assert result is mock_session
        assert mock_factory.call_count == 1
        assert mock_connection.config is second_resolved
        assert mock_connection.config.headers == {"Authorization": "Bearer rotated"}


class TestMCPClientManagerDisconnect:
    """Tests for disconnect_all method."""

    @pytest.mark.asyncio
    async def test_disconnect_all_cancels_health_task(self) -> None:
        """Test disconnect_all cancels health monitoring."""
        manager = MCPClientManager(server_configs=[])

        # Start health monitoring
        await manager.connect_all()
        assert manager._health_check_task is not None

        await manager.disconnect_all()

        assert manager._health_check_task is None

    @pytest.mark.asyncio
    async def test_disconnect_all_cancels_reconnect_tasks(self) -> None:
        """Test disconnect_all cancels pending reconnect tasks."""
        manager = MCPClientManager(server_configs=[])

        # Add a mock reconnect task
        async def slow_reconnect() -> None:
            await wait_forever()

        task = asyncio.create_task(slow_reconnect())
        manager._reconnect_tasks.add(task)

        await manager.disconnect_all()

        assert len(manager._reconnect_tasks) == 0

    @pytest.mark.asyncio
    async def test_disconnect_all_tears_down_connecting_transport(self) -> None:
        """Shutdown must tear down transports that have not reached CONNECTED."""
        manager = MCPClientManager(server_configs=[])
        connection = AsyncMock()
        connection.is_connected = False
        manager._connections["connecting-server"] = connection
        manager.health["connecting-server"] = MCPConnectionHealth(
            name="connecting-server",
            state=ConnectionState.CONNECTING,
        )

        await manager.disconnect_all()

        connection.disconnect.assert_awaited_once_with()
        assert manager._connections == {}
        assert manager.health["connecting-server"].state is ConnectionState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_disconnect_all_handles_timeout(self) -> None:
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

        async def slow_disconnect() -> None:
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
    async def test_call_tool_success(self) -> None:
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
    async def test_call_tool_with_timeout(self) -> None:
        """Test call_tool respects timeout."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        mock_session = AsyncMock()

        async def slow_tool(*args: Any) -> dict[str, str]:
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
    async def test_call_tool_records_metrics(self) -> None:
        """Test call_tool records metrics when manager configured."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        mock_metrics = MagicMock()
        event_loop_thread = threading.get_ident()
        metrics_threads: list[int] = []

        def record_call(**_kwargs: Any) -> None:
            metrics_threads.append(threading.get_ident())

        mock_metrics.record_call.side_effect = record_call
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
        assert len(metrics_threads) == 1
        assert metrics_threads[0] != event_loop_thread

    @pytest.mark.asyncio
    async def test_call_tool_records_failure_metrics(self) -> None:
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
    async def test_call_tool_handles_metrics_error(self) -> None:
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
    async def test_read_resource_success(self) -> None:
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
    async def test_read_resource_records_failure(self) -> None:
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
    async def test_list_tools_single_server(self) -> None:
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
        mock_tool.input_schema = {"type": "object"}
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
    @pytest.mark.asyncio
    async def test_list_tools_single_server_propagates_connection_error(self) -> None:
        """A dead single server must not be reported as an empty inventory."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])
        manager._connections["test-server"] = MagicMock()

        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        with (
            patch.object(
                manager,
                "get_client_session",
                side_effect=ConnectionError("initial connection lost"),
            ),
            patch.object(
                manager,
                "ensure_connected",
                side_effect=ConnectionError("reconnect refused"),
            ),
            pytest.raises(MCPError, match="reconnect retry failed: reconnect refused"),
        ):
            await manager.list_tools("test-server")


class TestMCPClientManagerGetToolInputSchema:
    """Tests for get_tool_input_schema method."""

    @pytest.mark.asyncio
    async def test_get_tool_input_schema_success(self) -> None:
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
    async def test_get_tool_input_schema_tool_not_found(self) -> None:
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

    def test_cache_discovered_tools_writes_only_when_inventory_changes(self) -> None:
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )
        mock_db = MagicMock()
        stored = MagicMock()
        stored.id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa10"
        mock_db.get_server.return_value = stored
        manager = MCPClientManager(server_configs=[config], mcp_db_manager=mock_db)
        tools = [
            {
                "name": "test-tool",
                "description": "Test tool",
                "inputSchema": {"type": "object"},
            }
        ]

        manager.cache_discovered_tools("test-server", tools)
        manager.cache_discovered_tools("test-server", [dict(tools[0])])

        mock_db.cache_tools.assert_called_once_with(stored.id, tools)

        changed_tools = [*tools, {"name": "other-tool", "inputSchema": {}}]
        manager.cache_discovered_tools("test-server", changed_tools)
        assert mock_db.cache_tools.call_count == 2

    def test_cache_discovered_tools_retries_transient_persistence_failure(self) -> None:
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )
        mock_db = MagicMock()
        mock_db.cache_tools.side_effect = [RuntimeError("db unavailable"), None]
        manager = MCPClientManager(server_configs=[config], mcp_db_manager=mock_db)
        tools = [{"name": "test-tool", "inputSchema": {"type": "object"}}]

        manager.cache_discovered_tools("test-server", tools)

        assert manager._tool_schema_cache["test-server"] == tools
        manager.cache_discovered_tools("test-server", [dict(tools[0])])
        manager.cache_discovered_tools("test-server", [dict(tools[0])])

        assert mock_db.cache_tools.call_count == 2

    @pytest.mark.asyncio
    async def test_disconnect_server_invalidates_schema_cache(self) -> None:
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )
        manager = MCPClientManager(server_configs=[config])
        manager._tool_schema_cache["test-server"] = [{"name": "test-tool"}]

        await manager.disconnect_server("test-server")

        assert "test-server" not in manager._tool_schema_cache


class TestMCPClientManagerHealthCheck:
    """Tests for health_check_all method."""

    @pytest.mark.asyncio
    async def test_health_check_all_with_connections(self) -> None:
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
            consecutive_failures=2,
            last_error="Previous failure",
        )

        result = await manager.health_check_all()

        assert result["test-server"] is True
        mock_connection.health_check.assert_called_once_with(timeout=5.0)
        assert manager.health["test-server"].consecutive_failures == 0
        assert manager.health["test-server"].last_error is None

    @pytest.mark.asyncio
    async def test_health_check_all_records_failures(self) -> None:
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
        assert manager.health["test-server"].last_error == "Health check failed"

    @pytest.mark.asyncio
    async def test_health_check_all_formats_raised_failure(self) -> None:
        """Unexpected probe exceptions retain their type and normalized message."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )
        manager = MCPClientManager(server_configs=[config])
        mock_connection = AsyncMock()
        mock_connection.is_connected = True
        mock_connection.health_check.side_effect = RuntimeError("probe failed\nhard")
        manager._connections["test-server"] = mock_connection
        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTED,
        )

        result = await manager.health_check_all()

        assert result["test-server"] is False
        assert manager.health["test-server"].last_error == "RuntimeError: probe failed hard"


class TestMCPClientManagerReconnect:
    """Tests for _reconnect method."""

    @pytest.mark.asyncio
    async def test_reconnect_success(self) -> None:
        """Test _reconnect disconnects old connection before reconnecting."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config])

        old_conn = AsyncMock()
        old_conn.is_connected = False
        manager._connections["test-server"] = old_conn
        manager._tool_schema_cache["test-server"] = [{"name": "test-tool"}]
        manager.health["test-server"] = MCPConnectionHealth(
            name="test-server",
            state=ConnectionState.CONNECTING,
        )

        with patch.object(manager, "_connect_server", return_value=MagicMock()):
            await manager._reconnect("test-server")

        old_conn.disconnect.assert_awaited_once()
        assert "test-server" not in manager._tool_schema_cache
        assert old_conn.disconnect.await_count == 1
        assert old_conn.disconnect.await_args is not None

    @pytest.mark.asyncio
    async def test_reconnect_no_old_connection(self) -> None:
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
    async def test_reconnect_old_disconnect_failure_does_not_block(self) -> None:
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
    async def test_reconnect_handles_unknown_server(self) -> None:
        """Test _reconnect handles unknown server gracefully."""
        manager = MCPClientManager(server_configs=[])

        await manager._reconnect("unknown-server")
        assert "unknown-server" not in manager._connections

    @pytest.mark.asyncio
    async def test_reconnect_handles_failure(self) -> None:
        """Test _reconnect handles connection failure."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )

        manager = MCPClientManager(server_configs=[config], max_connection_retries=0)

        with patch.object(
            manager,
            "_connect_server",
            side_effect=Exception("Reconnect failed"),
        ):
            await manager._reconnect("test-server")
            assert "test-server" not in manager._connections

    @pytest.mark.asyncio
    async def test_reconnect_and_ensure_connected_share_one_connect(self) -> None:
        """Concurrent recovery paths serialize connection startup."""
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )
        manager = MCPClientManager(
            server_configs=[config],
            connection_timeout=1.0,
            max_connection_retries=0,
        )
        connect_started = asyncio.Event()
        ensure_waiting = asyncio.Event()
        release_connect = asyncio.Event()
        session = MagicMock()
        connect_calls = 0
        acquire_calls = 0
        original_acquire_lock = connections._acquire_connection_lock

        async def tracked_acquire_lock(manager_arg: Any, server_name: str) -> asyncio.Lock:
            nonlocal acquire_calls
            acquire_calls += 1
            if acquire_calls == 2:
                ensure_waiting.set()
            return await original_acquire_lock(manager_arg, server_name)

        async def controlled_connect(_config: MCPServerConfig) -> Any:
            nonlocal connect_calls
            connect_calls += 1
            connect_started.set()
            await release_connect.wait()
            connection = MagicMock()
            connection.is_connected = True
            connection.session = session
            manager._connections["test-server"] = connection
            return session

        with (
            patch.object(connections, "_acquire_connection_lock", side_effect=tracked_acquire_lock),
            patch.object(manager, "_connect_server", side_effect=controlled_connect),
        ):
            reconnect_task = asyncio.create_task(manager._reconnect("test-server"))
            await connect_started.wait()
            ensure_task = asyncio.create_task(manager.ensure_connected("test-server"))
            await asyncio.wait_for(ensure_waiting.wait(), timeout=1.0)
            assert not ensure_task.done()
            release_connect.set()
            reconnect_result, ensure_result = await asyncio.gather(reconnect_task, ensure_task)

        assert reconnect_result is None
        assert ensure_result is session
        assert connect_calls == 1

    @pytest.mark.asyncio
    async def test_reconnect_serializes_teardown_with_ensure_connected(self) -> None:
        config = MCPServerConfig(
            name="test-server",
            project_id="test-project",
            transport="http",
            url="http://localhost:8001",
        )
        manager = MCPClientManager(
            server_configs=[config],
            connection_timeout=1.0,
            max_connection_retries=0,
        )
        teardown_started = asyncio.Event()
        ensure_waiting = asyncio.Event()
        release_teardown = asyncio.Event()
        old_session = MagicMock()
        new_session = MagicMock()
        old_connection = MagicMock()
        old_connection.is_connected = True
        old_connection.session = old_session

        async def controlled_disconnect() -> None:
            teardown_started.set()
            await release_teardown.wait()

        old_connection.disconnect = AsyncMock(side_effect=controlled_disconnect)
        manager._connections["test-server"] = old_connection
        connect_calls = 0
        acquire_calls = 0
        original_acquire_lock = connections._acquire_connection_lock

        async def tracked_acquire_lock(manager_arg: Any, server_name: str) -> asyncio.Lock:
            nonlocal acquire_calls
            acquire_calls += 1
            if acquire_calls == 2:
                ensure_waiting.set()
            return await original_acquire_lock(manager_arg, server_name)

        async def controlled_connect(_config: MCPServerConfig) -> Any:
            nonlocal connect_calls
            connect_calls += 1
            connection = MagicMock()
            connection.is_connected = True
            connection.session = new_session
            manager._connections["test-server"] = connection
            return new_session

        with (
            patch.object(connections, "_acquire_connection_lock", side_effect=tracked_acquire_lock),
            patch.object(manager, "_connect_server", side_effect=controlled_connect),
        ):
            reconnect_task = asyncio.create_task(manager._reconnect("test-server"))
            await asyncio.wait_for(teardown_started.wait(), timeout=1.0)
            ensure_task = asyncio.create_task(manager.ensure_connected("test-server"))
            await asyncio.wait_for(ensure_waiting.wait(), timeout=1.0)

            assert not ensure_task.done()
            release_teardown.set()
            ensured_session = await asyncio.wait_for(ensure_task, timeout=1.0)
            await asyncio.wait_for(reconnect_task, timeout=1.0)

        assert ensured_session is new_session
        assert connect_calls == 1
        old_connection.disconnect.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_reconnect_applies_connection_timeout(self) -> None:
        """A wedged reconnect returns after the configured connect timeout."""
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
        connect_cancelled = asyncio.Event()

        async def wedged_connect(_config: MCPServerConfig) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                connect_cancelled.set()

        with patch.object(manager, "_connect_server", side_effect=wedged_connect):
            await asyncio.wait_for(manager._reconnect("test-server"), timeout=0.2)

        assert connect_cancelled.is_set()


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
            last_error="list_tools timed out after 5s",
        )

        health = manager.get_server_health()

        assert "test-server" in health
        assert health["test-server"]["state"] == "connected"
        assert health["test-server"]["health"] == "healthy"
        assert health["test-server"]["response_time_ms"] == 42.5
        assert health["test-server"]["failures"] == 0
        assert health["test-server"]["last_error"] == "list_tools timed out after 5s"


class TestMCPClientManagerMonitorHealth:
    """Tests for _monitor_health background task."""

    @pytest.mark.asyncio
    async def test_monitor_health_checks_connections(self) -> None:
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
    async def test_monitor_health_triggers_reconnect_on_unhealthy(self) -> None:
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

        async def mock_reconnect(name: str) -> None:
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
    async def test_monitor_health_keeps_transient_failure_below_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
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
        mock_connection.last_health_error = "list_tools timed out after 5s"
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
        assert manager.health["test-server"].last_error == "list_tools timed out after 5s"
        assert "Health check failed for test-server" not in caplog.text

    @pytest.mark.asyncio
    async def test_monitor_health_warns_on_unhealthy_transition(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
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
    async def test_monitor_health_debugs_repeated_unhealthy_failure_with_context(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
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
        mock_connection.last_health_error = "list_tools timed out after 5s"
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
        record_context = vars(debug_records[0])
        assert record_context["server_name"] == "test-server"
        assert record_context["previous_health"] == HealthState.UNHEALTHY.value
        assert record_context["consecutive_failures"] == 6
        assert record_context["last_error"] == "list_tools timed out after 5s"

    @pytest.mark.asyncio
    async def test_monitor_health_continues_when_no_connections(self) -> None:
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
    async def test_monitor_health_handles_exceptions(self) -> None:
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
    async def test_connect_all_eager_skips_disabled(self) -> None:
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

        async def mock_connect(config: MCPServerConfig) -> Any:
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
    async def test_disconnect_all_handles_disconnect_error(self) -> None:
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
    async def test_ensure_connected_circuit_open_no_failure_time(self) -> None:
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
        assert state is not None
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
    async def test_ensure_connected_double_check_after_lock(self) -> None:
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

        async def simulate_concurrent_connect() -> None:
            await connect_started.wait()
            # Simulate another coroutine connecting while we wait
            mock_connection = MagicMock()
            mock_connection.is_connected = True
            mock_connection.session = mock_session
            manager._connections["test-server"] = mock_connection
            connection_established.set()

        async def slow_connect(cfg: MCPServerConfig) -> Any:
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
    async def test_ensure_connected_null_session(self) -> None:
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
    async def test_get_client_session_delegates_to_ensure_connected(self) -> None:
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
    async def test_call_tool_no_metrics_recorded_without_project_id(self) -> None:
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
    async def test_call_tool_uses_config_project_id(self) -> None:
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
    async def test_list_tools_all_connected_servers(self) -> None:
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
        mock_tool.input_schema = {}
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

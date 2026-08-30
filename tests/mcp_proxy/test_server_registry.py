"""Tests for MCP client manager server registry helpers."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.client_manager import server_registry
from gobby.mcp_proxy.models import MCPServerConfig

pytestmark = pytest.mark.unit


class _LazyConnector:
    def __init__(self) -> None:
        self.registered: list[str] = []
        self.unregistered: list[str] = []
        self._locks: dict[str, asyncio.Lock] = {}

    def register_server(self, name: str) -> None:
        self.registered.append(name)

    def unregister_server(self, name: str) -> None:
        self.unregistered.append(name)

    def get_connection_lock(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())


class _Manager:
    def __init__(self) -> None:
        config = MCPServerConfig(
            name="custom",
            transport="stdio",
            command="npx",
            enabled=True,
            project_id="existing-project",
        )
        self._configs = {config.id: config}
        self._connections: dict[str, object] = {}
        self._tool_schema_cache: dict[str, list[dict[str, object]]] = {}
        self.health: dict[str, object] = {config.id: object()}
        self._lazy_connector = _LazyConnector()
        self.mcp_db_manager: MagicMock | None = None
        self.connection_timeout = 30.0

    @property
    def custom_id(self) -> str:
        return next(iter(self._configs))


@pytest.mark.asyncio
async def test_update_server_does_not_mutate_input_config() -> None:
    manager = _Manager()
    server_id = manager.custom_id
    manager._tool_schema_cache[server_id] = [{"name": "old-tool"}]
    caller_config = MCPServerConfig(
        name="custom",
        transport="stdio",
        command="npx",
        args=["--stdio"],
        enabled=False,
        project_id="",
    )

    result = await server_registry.update_server(
        manager,
        server_id,
        caller_config,
        project_id="route-project",
    )

    assert result == {"success": True, "name": "custom", "id": server_id}
    assert caller_config.enabled is False
    assert caller_config.project_id == ""
    updated = manager._configs[server_id]
    assert updated is not caller_config
    assert updated.enabled is True
    assert updated.project_id == "route-project"
    assert server_id not in manager._tool_schema_cache


@pytest.mark.asyncio
async def test_remove_server_keeps_runtime_state_when_persistence_fails() -> None:
    manager = _Manager()
    server_id = manager.custom_id
    original_config = manager._configs[server_id]
    connection = AsyncMock()
    manager._connections[server_id] = connection
    manager._tool_schema_cache[server_id] = [{"name": "old-tool"}]
    manager._lazy_connector.register_server(server_id)
    db_manager = MagicMock()
    db_manager.remove_server.side_effect = RuntimeError("db unavailable")
    manager.mcp_db_manager = db_manager

    with pytest.raises(RuntimeError, match="db unavailable"):
        await server_registry.remove_server(manager, server_id)

    assert manager._configs[server_id] is original_config
    assert manager._connections[server_id] is connection
    assert manager._tool_schema_cache[server_id] == [{"name": "old-tool"}]
    assert server_id in manager.health
    assert manager._lazy_connector.registered == [server_id]
    assert manager._lazy_connector.unregistered == []
    connection.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_server_keeps_runtime_state_when_persistence_fails() -> None:
    manager = _Manager()
    server_id = manager.custom_id
    original_config = manager._configs[server_id]
    connection = AsyncMock()
    manager._connections[server_id] = connection
    manager._tool_schema_cache[server_id] = [{"name": "old-tool"}]
    manager._lazy_connector.register_server(server_id)
    db_manager = MagicMock()
    db_manager.update_server.side_effect = RuntimeError("db unavailable")
    manager.mcp_db_manager = db_manager
    replacement = MCPServerConfig(
        name="custom",
        transport="stdio",
        command="uvx",
        project_id="existing-project",
    )

    with pytest.raises(RuntimeError, match="db unavailable"):
        await server_registry.update_server(manager, server_id, replacement)

    assert manager._configs[server_id] is original_config
    assert manager._connections[server_id] is connection
    assert manager._tool_schema_cache[server_id] == [{"name": "old-tool"}]
    assert server_id in manager.health
    assert manager._lazy_connector.registered == [server_id]
    assert manager._lazy_connector.unregistered == []
    connection.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_server_finalizes_runtime_state_when_disconnect_fails() -> None:
    manager = _Manager()
    server_id = manager.custom_id
    connection = AsyncMock()
    connection.disconnect.side_effect = RuntimeError("disconnect failed")
    manager._connections[server_id] = connection
    manager._tool_schema_cache[server_id] = [{"name": "old-tool"}]
    manager._lazy_connector.register_server(server_id)
    db_manager = MagicMock()
    manager.mcp_db_manager = db_manager

    with pytest.raises(RuntimeError, match="disconnect failed"):
        await server_registry.remove_server(manager, server_id)

    db_manager.remove_server.assert_called_once_with("custom", "existing-project")
    assert server_id not in manager._configs
    assert server_id not in manager._connections
    assert server_id not in manager._tool_schema_cache
    assert server_id not in manager.health
    assert manager._lazy_connector.unregistered == [server_id]


@pytest.mark.asyncio
async def test_update_server_finalizes_runtime_state_when_disconnect_fails() -> None:
    manager = _Manager()
    server_id = manager.custom_id
    connection = AsyncMock()
    connection.disconnect.side_effect = RuntimeError("disconnect failed")
    manager._connections[server_id] = connection
    manager._tool_schema_cache[server_id] = [{"name": "old-tool"}]
    manager._lazy_connector.register_server(server_id)
    db_manager = MagicMock()
    manager.mcp_db_manager = db_manager
    replacement = MCPServerConfig(
        name="custom",
        transport="stdio",
        command="uvx",
        project_id="existing-project",
    )

    with pytest.raises(RuntimeError, match="disconnect failed"):
        await server_registry.update_server(manager, server_id, replacement)

    db_manager.update_server.assert_called_once()
    assert manager._configs[server_id].command == "uvx"
    assert manager._configs[server_id].enabled is True
    assert server_id not in manager._connections
    assert server_id not in manager._tool_schema_cache
    assert server_id not in manager.health
    assert manager._lazy_connector.unregistered == [server_id]
    assert manager._lazy_connector.registered == [server_id, server_id]


@pytest.mark.asyncio
async def test_disable_server_finalizes_runtime_state_when_disconnect_fails() -> None:
    manager = _Manager()
    server_id = manager.custom_id
    connection = AsyncMock()
    connection.disconnect.side_effect = RuntimeError("disconnect failed")
    manager._connections[server_id] = connection
    manager._tool_schema_cache[server_id] = [{"name": "old-tool"}]
    manager._lazy_connector.register_server(server_id)
    db_manager = MagicMock()
    manager.mcp_db_manager = db_manager

    with pytest.raises(RuntimeError, match="disconnect failed"):
        await server_registry.set_server_enabled(manager, server_id, False)

    db_manager.update_server.assert_called_once_with("custom", "existing-project", enabled=False)
    assert manager._configs[server_id].enabled is False
    assert server_id not in manager._connections
    assert server_id not in manager._tool_schema_cache
    assert server_id not in manager.health
    assert manager._lazy_connector.unregistered == [server_id]

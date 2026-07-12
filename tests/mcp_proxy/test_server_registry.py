"""Tests for MCP client manager server registry helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.client_manager import server_registry
from gobby.mcp_proxy.models import MCPServerConfig

pytestmark = pytest.mark.unit


class _LazyConnector:
    def __init__(self) -> None:
        self.registered: list[str] = []
        self.unregistered: list[str] = []

    def register_server(self, name: str) -> None:
        self.registered.append(name)

    def unregister_server(self, name: str) -> None:
        self.unregistered.append(name)


class _Manager:
    def __init__(self) -> None:
        self._configs = {
            "custom": MCPServerConfig(
                name="custom",
                transport="stdio",
                command="npx",
                enabled=True,
                project_id="existing-project",
            )
        }
        self._connections: dict[str, object] = {}
        self._tool_schema_cache: dict[str, list[dict[str, object]]] = {}
        self.health: dict[str, object] = {"custom": object()}
        self._lazy_connector = _LazyConnector()
        self.mcp_db_manager = None


@pytest.mark.asyncio
async def test_update_server_does_not_mutate_input_config() -> None:
    manager = _Manager()
    manager._tool_schema_cache["custom"] = [{"name": "old-tool"}]
    caller_config = MCPServerConfig(
        name="custom",
        transport="stdio",
        command="npx",
        args=["--stdio"],
        enabled=False,
        project_id=None,
    )

    result = await server_registry.update_server(
        manager,
        "custom",
        caller_config,
        project_id="route-project",
    )

    assert result == {"success": True, "name": "custom"}
    assert caller_config.enabled is False
    assert caller_config.project_id is None
    updated = manager._configs["custom"]
    assert updated is not caller_config
    assert updated.enabled is True
    assert updated.project_id == "route-project"
    assert "custom" not in manager._tool_schema_cache


@pytest.mark.asyncio
async def test_remove_server_keeps_runtime_state_when_persistence_fails() -> None:
    manager = _Manager()
    original_config = manager._configs["custom"]
    connection = AsyncMock()
    manager._connections["custom"] = connection
    manager._tool_schema_cache["custom"] = [{"name": "old-tool"}]
    manager._lazy_connector.register_server("custom")
    db_manager = MagicMock()
    db_manager.remove_server.side_effect = RuntimeError("db unavailable")
    manager.mcp_db_manager = db_manager

    with pytest.raises(RuntimeError, match="db unavailable"):
        await server_registry.remove_server(manager, "custom")

    assert manager._configs["custom"] is original_config
    assert manager._connections["custom"] is connection
    assert manager._tool_schema_cache["custom"] == [{"name": "old-tool"}]
    assert "custom" in manager.health
    assert manager._lazy_connector.registered == ["custom"]
    assert manager._lazy_connector.unregistered == []
    connection.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_server_keeps_runtime_state_when_persistence_fails() -> None:
    manager = _Manager()
    original_config = manager._configs["custom"]
    connection = AsyncMock()
    manager._connections["custom"] = connection
    manager._tool_schema_cache["custom"] = [{"name": "old-tool"}]
    manager._lazy_connector.register_server("custom")
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
        await server_registry.update_server(manager, "custom", replacement)

    assert manager._configs["custom"] is original_config
    assert manager._connections["custom"] is connection
    assert manager._tool_schema_cache["custom"] == [{"name": "old-tool"}]
    assert "custom" in manager.health
    assert manager._lazy_connector.registered == ["custom"]
    assert manager._lazy_connector.unregistered == []
    connection.disconnect.assert_not_awaited()

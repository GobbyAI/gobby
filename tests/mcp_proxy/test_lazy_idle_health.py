"""On-demand MCP transports do not receive background traffic while idle."""

import time
from unittest.mock import AsyncMock, patch

import pytest

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import MCPServerConfig

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_idle_lazy_server_gets_no_requests_for_three_intervals_then_reconnects() -> None:
    lazy = MCPServerConfig(
        name="remote", project_id="test", transport="http", url="https://example.test/mcp"
    )
    eager = MCPServerConfig(
        name="local", project_id="test", transport="http", url="http://127.0.0.1:1234/mcp"
    )
    manager = MCPClientManager(
        server_configs=[lazy, eager],
        preconnect_servers=["local"],
        health_check_interval=60,
        max_connection_retries=0,
    )
    old = AsyncMock()
    old.is_connected = True
    old.session = AsyncMock()
    old.health_check.side_effect = old.session.list_tools
    eager_connection = AsyncMock()
    eager_connection.is_connected = True
    eager_connection.health_check.return_value = True
    manager._connections[lazy.id] = old
    manager._connections[eager.id] = eager_connection
    manager._lazy_connector.mark_connected(lazy.id)
    manager._running = True
    ticks = 0

    async def advance(_delay: float) -> None:
        nonlocal ticks
        ticks += 1
        state = manager._lazy_connector.get_state(lazy.id)
        assert state is not None
        state.last_used_at = time.monotonic() - ticks * 60
        if ticks == 3:
            old.health_check.assert_not_awaited()
            old.session.list_tools.assert_not_awaited()
            old.disconnect.assert_not_awaited()
        if ticks == 5:
            manager._running = False

    with patch("gobby.mcp_proxy.manager.asyncio.sleep", side_effect=advance):
        await manager._monitor_health()

    assert ticks == 5
    old.health_check.assert_not_awaited()
    old.session.list_tools.assert_not_awaited()
    old.disconnect.assert_awaited_once()
    assert eager_connection.health_check.await_count == 5
    assert lazy.id not in manager._connections
    assert manager.get_server_health()[lazy.id]["state"] == "disconnected"

    session = AsyncMock()
    session.call_tool.return_value = "called"
    replacement = AsyncMock()
    replacement.is_connected = True
    replacement.session = session

    async def reconnect(_config: MCPServerConfig) -> AsyncMock:
        manager._connections[lazy.id] = replacement
        return session

    with patch.object(manager, "_connect_server", side_effect=reconnect) as connect:
        assert await manager.call_tool(lazy.id, "run") == "called"
    connect.assert_awaited_once()
    session.call_tool.assert_awaited_once_with("run", {})


@pytest.mark.asyncio
async def test_active_lazy_request_is_not_disconnected_when_idle_clock_expires() -> None:
    config = MCPServerConfig(
        name="remote", project_id="test", transport="http", url="https://example.test/mcp"
    )
    manager = MCPClientManager(server_configs=[config], health_check_interval=60)
    connection = AsyncMock()
    connection.is_connected = True
    manager._connections[config.id] = connection
    manager._lazy_connector.mark_connected(config.id)
    manager._lazy_connector.start_request(config.id)
    state = manager._lazy_connector.get_state(config.id)
    assert state is not None
    state.last_used_at = time.monotonic() - 600
    manager._running = True

    async def one_interval(_delay: float) -> None:
        manager._running = False

    with patch("gobby.mcp_proxy.manager.asyncio.sleep", side_effect=one_interval):
        await manager._monitor_health()

    connection.health_check.assert_not_awaited()
    connection.disconnect.assert_not_awaited()
    manager._lazy_connector.finish_request(config.id)
    assert state.active_requests == 0


@pytest.mark.asyncio
async def test_explicit_health_check_is_allowed_and_refreshes_idle_time() -> None:
    config = MCPServerConfig(
        name="remote", project_id="test", transport="http", url="https://example.test/mcp"
    )
    manager = MCPClientManager(server_configs=[config])
    connection = AsyncMock()
    connection.is_connected = True
    connection.health_check.return_value = True
    manager._connections[config.id] = connection
    manager._lazy_connector.mark_connected(config.id)
    state = manager._lazy_connector.get_state(config.id)
    assert state is not None
    state.last_used_at = time.monotonic() - 600

    assert await manager.health_check_all() == {config.id: True}

    connection.health_check.assert_awaited_once()
    assert state.active_requests == 0
    assert state.last_used_at is not None
    assert time.monotonic() - state.last_used_at < 1

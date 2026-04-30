"""Cancellation cleanup tests for MCPClientManager.disconnect_all()."""

from __future__ import annotations

import asyncio

import pytest

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import ConnectionState, MCPConnectionHealth, MCPServerConfig


class BlockingConnection:
    """Connection test double that blocks until its disconnect task is cancelled."""

    is_connected = True

    def __init__(self) -> None:
        self.disconnect_started = asyncio.Event()
        self.disconnect_cancelled = False

    async def disconnect(self) -> None:
        self.disconnect_started.set()
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            self.disconnect_cancelled = True
            raise


@pytest.mark.asyncio
async def test_disconnect_all_cleans_state_when_cancelled_during_disconnect() -> None:
    config = MCPServerConfig(
        name="slow-server",
        project_id="test-project",
        transport="http",
        url="http://localhost:8001",
    )
    manager = MCPClientManager(server_configs=[config])

    connection = BlockingConnection()
    manager._connections["slow-server"] = connection
    manager.health["slow-server"] = MCPConnectionHealth(
        name="slow-server",
        state=ConnectionState.CONNECTED,
    )
    manager._lazy_connector.mark_connected("slow-server")

    async def slow_reconnect() -> None:
        await asyncio.sleep(100)

    reconnect_task = asyncio.create_task(slow_reconnect())
    manager._reconnect_tasks.add(reconnect_task)

    disconnect_task = asyncio.create_task(manager.disconnect_all())
    await asyncio.wait_for(connection.disconnect_started.wait(), timeout=1.0)

    disconnect_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await disconnect_task

    lazy_state = manager._lazy_connector.get_state("slow-server")

    assert connection.disconnect_cancelled is True
    assert manager._connections == {}
    assert manager._reconnect_tasks == set()
    assert reconnect_task.done()
    assert manager.health["slow-server"].state is ConnectionState.DISCONNECTED
    assert lazy_state is not None
    assert lazy_state.connected_at is None

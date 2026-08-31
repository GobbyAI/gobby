"""Cancellation cleanup tests for MCPClientManager.disconnect_all()."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import ConnectionState, MCPConnectionHealth, MCPServerConfig
from gobby.mcp_proxy.transports.base import BaseTransportConnection
from tests._timing import wait_forever


class BlockingConnection:
    """Connection test double that blocks until its disconnect task is cancelled."""

    is_connected = True

    def __init__(self) -> None:
        self.disconnect_started = asyncio.Event()
        self.disconnect_cancelled = False

    async def disconnect(self) -> None:
        self.disconnect_started.set()
        try:
            await wait_forever()
        except asyncio.CancelledError:
            self.disconnect_cancelled = True
            raise


@pytest.mark.asyncio
async def test_disconnect_all_closes_transport_in_caller_task() -> None:
    """Task-affine transport contexts must be closed by their caller task."""
    manager = MCPClientManager(server_configs=[])
    caller_task = asyncio.current_task()
    observed_task: asyncio.Task[object] | None = None

    class TaskRecordingConnection:
        is_connected = True

        async def disconnect(self) -> None:
            nonlocal observed_task
            observed_task = asyncio.current_task()

    connection = TaskRecordingConnection()
    manager._connections["stdio-server"] = connection

    await manager.disconnect_all()

    assert observed_task is caller_task


@pytest.mark.asyncio
async def test_disconnect_all_has_one_overall_shutdown_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.mcp_proxy import connection_cleanup

    manager = MCPClientManager(server_configs=[])
    first = BlockingConnection()
    second = BlockingConnection()
    manager._connections.update(
        {
            "first": cast(BaseTransportConnection, first),
            "second": cast(BaseTransportConnection, second),
        }
    )
    monkeypatch.setattr(connection_cleanup, "_DISCONNECT_ALL_TIMEOUT_SECONDS", 0.01)

    await asyncio.wait_for(manager.disconnect_all(), timeout=0.5)

    assert first.disconnect_cancelled is True
    assert second.disconnect_started.is_set() is False
    assert manager._connections == {}


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
    manager._connections[config.id] = connection
    manager.health[config.id] = MCPConnectionHealth(
        name="slow-server",
        state=ConnectionState.CONNECTED,
        project_id=config.project_id,
    )
    manager._lazy_connector.mark_connected(config.id)

    async def slow_reconnect() -> None:
        await wait_forever()

    reconnect_task = asyncio.create_task(slow_reconnect())
    manager._reconnect_tasks.add(reconnect_task)

    disconnect_task = asyncio.create_task(manager.disconnect_all())
    await asyncio.wait_for(connection.disconnect_started.wait(), timeout=1.0)

    disconnect_task.cancel()
    await disconnect_task

    lazy_state = manager._lazy_connector.get_state(config.id)

    assert connection.disconnect_cancelled is True
    assert manager._connections == {}
    assert manager._reconnect_tasks == set()
    assert reconnect_task.done()
    assert manager.health[config.id].state is ConnectionState.DISCONNECTED
    assert lazy_state is not None
    assert lazy_state.connected_at is None


@pytest.mark.asyncio
async def test_refresh_cancelled_during_old_disconnect_finishes_teardown() -> None:
    config = MCPServerConfig(
        name="slow-server",
        project_id="test-project",
        transport="http",
        url="http://localhost:8001",
    )
    manager = MCPClientManager(server_configs=[config])

    class TrackedBlockingConnection(BlockingConnection):
        def __init__(self) -> None:
            super().__init__()
            self.disconnect_finished = asyncio.Event()

        async def disconnect(self) -> None:
            try:
                await super().disconnect()
            finally:
                self.disconnect_finished.set()

    connection = TrackedBlockingConnection()
    manager._connections[config.id] = cast(BaseTransportConnection, connection)
    manager.health[config.id] = MCPConnectionHealth(
        name="slow-server",
        state=ConnectionState.CONNECTED,
    )
    manager._lazy_connector.mark_connected(config.id)

    refresh_task = asyncio.create_task(manager.refresh_server(config.id))
    await asyncio.wait_for(connection.disconnect_started.wait(), timeout=1.0)
    refresh_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await refresh_task

    assert connection.disconnect_cancelled is True
    assert connection.disconnect_finished.is_set()
    assert config.id not in manager._connections
    lock = manager._lazy_connector.get_connection_lock(config.id)
    assert not lock.locked()

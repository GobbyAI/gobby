"""Connection cleanup helpers for MCP client manager."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import MutableMapping
from typing import Any

from gobby.mcp_proxy.models import ConnectionState


def describe_exception(exc: BaseException) -> str:
    """Return a useful error string even for exceptions with empty messages."""
    message = str(exc).strip()
    return message or type(exc).__name__


def clear_connection_state(
    name: str,
    connections: MutableMapping[str, Any],
    health: MutableMapping[str, Any],
    lazy_connector: Any,
) -> Any | None:
    """Remove cached connection state and mark lazy/health state disconnected."""
    connection = connections.pop(name, None)
    if name in health:
        health[name].state = ConnectionState.DISCONNECTED
    lazy_state = lazy_connector.get_state(name)
    if lazy_state is not None:
        lazy_state.connected_at = None
    return connection


async def disconnect_connection(
    name: str,
    connection: Any,
    logger: logging.Logger,
    *,
    timeout: float = 5.0,
) -> None:
    """Best-effort disconnect for one MCP transport connection."""
    try:
        await asyncio.wait_for(connection.disconnect(), timeout=timeout)
    except TimeoutError:
        logger.warning("Connection disconnect timed out for %s", name)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Error disconnecting %s: %s", name, describe_exception(exc))


async def discard_connection(
    name: str,
    connections: MutableMapping[str, Any],
    health: MutableMapping[str, Any],
    lazy_connector: Any,
    logger: logging.Logger,
) -> None:
    """Drop a cached connection and best-effort disconnect the old transport."""
    connection = clear_connection_state(name, connections, health, lazy_connector)
    if connection is not None and getattr(connection, "is_connected", False):
        await disconnect_connection(name, connection, logger)


async def finalize_disconnect_all(
    *,
    connections: MutableMapping[str, Any],
    health: MutableMapping[str, Any],
    lazy_connector: Any,
    reconnect_tasks: set[asyncio.Task[None]],
    health_check_task: asyncio.Task[None] | None,
    logger: logging.Logger,
) -> None:
    """Disconnect all manager connections and finalize state under cancellation."""
    reconnect_snapshot = list(reconnect_tasks)
    disconnect_tasks: list[asyncio.Task[None]] = []

    try:
        if health_check_task:
            health_check_task.cancel()
            await asyncio.gather(health_check_task, return_exceptions=True)

        for task in reconnect_snapshot:
            task.cancel()
        if reconnect_snapshot:
            await asyncio.gather(*reconnect_snapshot, return_exceptions=True)
        reconnect_tasks.clear()

        for name, connection in list(connections.items()):
            if getattr(connection, "is_connected", False):
                disconnect_tasks.append(
                    asyncio.create_task(disconnect_connection(name, connection, logger))
                )
            if name in health:
                health[name].state = ConnectionState.DISCONNECTED

        if disconnect_tasks:
            await asyncio.gather(*disconnect_tasks, return_exceptions=True)
    except asyncio.CancelledError:
        logger.debug("MCP disconnect cleanup cancelled; finalizing state cleanup")
    finally:
        cleanup_tasks: list[asyncio.Task[None]] = []
        all_reconnect_tasks = [*reconnect_snapshot, *reconnect_tasks]

        if health_check_task and not health_check_task.done():
            health_check_task.cancel()
            cleanup_tasks.append(health_check_task)

        for task in all_reconnect_tasks:
            task.cancel()
            if task not in cleanup_tasks:
                cleanup_tasks.append(task)

        for task in disconnect_tasks:
            if not task.done():
                task.cancel()
            if task not in cleanup_tasks:
                cleanup_tasks.append(task)

        reconnect_tasks.clear()

        for name in list(connections):
            clear_connection_state(name, connections, health, lazy_connector)

        if cleanup_tasks:
            try:
                await asyncio.gather(*cleanup_tasks, return_exceptions=True)
            except asyncio.CancelledError:
                logger.debug("Cancelled while awaiting MCP disconnect cleanup tasks")

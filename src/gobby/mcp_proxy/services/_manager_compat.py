"""Compatibility helpers for MCP manager-like objects used by services."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any


def manager_has_method(mcp_manager: Any, method_name: str) -> bool:
    return callable(getattr(mcp_manager, method_name, None))


async def manager_is_connected(mcp_manager: Any, server_id: str) -> bool:
    is_connected = getattr(mcp_manager, "is_connected", None)
    if callable(is_connected):
        result = is_connected(server_id)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)
    if isinstance(is_connected, bool):
        return is_connected

    connections = getattr(mcp_manager, "connections", None)
    return isinstance(connections, dict) and server_id in connections


async def disconnect_manager_server(mcp_manager: Any, server_id: str) -> None:
    if manager_has_method(mcp_manager, "disconnect_server"):
        await mcp_manager.disconnect_server(server_id)
        return

    connections = getattr(mcp_manager, "connections", None)
    connection = connections.pop(server_id, None) if isinstance(connections, dict) else None
    if connection is not None and getattr(connection, "is_connected", False):
        await connection.disconnect()


def manager_server_configs(mcp_manager: Any) -> list[tuple[str, Any]]:
    configs = getattr(mcp_manager, "server_configs", None)
    if isinstance(configs, list | tuple):
        server_configs: list[tuple[str, Any]] = []
        for config in configs:
            name = getattr(config, "name", None)
            if isinstance(name, str):
                server_configs.append((name, config))
        return server_configs

    legacy_configs = getattr(mcp_manager, "_configs", None)
    if isinstance(legacy_configs, Mapping):
        return [(name, config) for name, config in legacy_configs.items() if isinstance(name, str)]

    return []

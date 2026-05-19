"""Compatibility helpers for MCP manager-like objects used by services."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def manager_has_method(mcp_manager: Any, method_name: str) -> bool:
    return callable(getattr(mcp_manager, method_name, None))


def manager_is_connected(mcp_manager: Any, name: str) -> bool:
    if manager_has_method(mcp_manager, "is_connected"):
        return bool(mcp_manager.is_connected(name))

    connections = getattr(mcp_manager, "connections", None)
    return isinstance(connections, dict) and name in connections


async def disconnect_manager_server(mcp_manager: Any, name: str) -> None:
    if manager_has_method(mcp_manager, "disconnect_server"):
        await mcp_manager.disconnect_server(name)
        return

    connections = getattr(mcp_manager, "connections", None)
    connection = connections.pop(name, None) if isinstance(connections, dict) else None
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
        return [
            (name, config)
            for name, config in legacy_configs.items()
            if isinstance(name, str)
        ]

    return []

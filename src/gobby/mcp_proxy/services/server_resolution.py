"""Server-name resolution helpers for the tool proxy service."""

import fnmatch
import logging
from typing import Any, cast

from gobby.mcp_proxy.services._manager_compat import (
    manager_is_connected,
    manager_server_configs,
)

logger = logging.getLogger("gobby.mcp.server")


def resolve_server_name(service: Any, server_name: str) -> str:
    """Auto-redirect known server name aliases to the correct server."""
    return cast("str", service._SERVER_SUGGESTIONS.get(server_name, server_name))


def get_server_suggestion(service: Any, server_name: str) -> str | None:
    """Get a suggestion for a possibly misspelled server name."""
    return cast("str | None", service._SERVER_SUGGESTIONS.get(server_name))


def is_proxy_namespace(service: Any, server_name: str) -> bool:
    """Check if the server name is the proxy namespace rather than a real server."""
    return cast("bool", server_name == service._PROXY_NAMESPACE)


def resolve_server_for_tool(service: Any, tool_name: str) -> str | None:
    """Resolve the actual server name for a tool when given the proxy namespace."""
    resolved = cast("str | None", service.find_tool_server(tool_name))
    if resolved:
        logger.warning(f"Auto-resolved server_name='gobby' → '{resolved}' for tool '{tool_name}'")
    else:
        logger.warning(f"server_name='gobby' used but tool '{tool_name}' not found on any server")
    return resolved


def find_tool_server(service: Any, tool_name: str) -> str | None:
    """Find which server owns a tool by searching all available servers."""
    if service._internal_manager:
        server = cast("str | None", service._internal_manager.find_tool_server(tool_name))
        if server:
            return server

    for server_name, config in manager_server_configs(service._mcp_manager):
        if config.tools:
            for tool in config.tools:
                tool_name_in_config = (
                    tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)
                )
                if tool_name_in_config == tool_name:
                    return server_name

    return None


async def list_servers(service: Any, name_filter: str | None = None) -> dict[str, Any]:
    """List all available MCP servers, including internal registries."""
    server_list: list[dict[str, Any]] = []
    connected = 0
    if service._internal_manager:
        for reg in service._internal_manager.get_all_registries():
            server_list.append({"name": reg.name, "state": "connected", "transport": "internal"})
            connected += 1
    for config in service._mcp_manager.server_configs:
        health = service._mcp_manager.health.get(config.name)
        state = health.state.value if health else "unknown"
        is_conn = await manager_is_connected(service._mcp_manager, config.name)
        if is_conn:
            connected += 1
        entry: dict[str, Any] = {
            "name": config.name,
            "state": state,
            "transport": config.transport,
        }
        if not config.enabled:
            entry["enabled"] = False
        server_list.append(entry)

    if name_filter:
        server_list = [s for s in server_list if fnmatch.fnmatch(s["name"], name_filter)]
        connected = sum(1 for s in server_list if s.get("state") == "connected")

    return {
        "success": True,
        "servers": server_list,
        "total": len(server_list),
        "connected": connected,
    }

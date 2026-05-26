"""Tool inventory helpers for MCP client manager."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from mcp import ClientSession

from gobby.mcp_proxy.connection_cleanup import describe_exception, discard_connection
from gobby.mcp_proxy.models import MCPError

from .server_registry import truncate_tool_brief


class _ToolInventoryManager(Protocol):
    _connections: dict[str, Any]
    _configs: dict[str, Any]
    _lazy_connector: Any
    health: dict[str, Any]
    mcp_db_manager: Any | None

    def cache_discovered_tools(self, server_name: str, tools: list[dict[str, Any]]) -> None: ...

    async def ensure_connected(self, server_name: str) -> ClientSession: ...

    async def get_client_session(self, server_name: str) -> ClientSession: ...

    async def get_tool_info(self, server_name: str, tool_name: str) -> dict[str, Any]: ...

    async def _list_tools_for_server(self, server_name: str) -> list[dict[str, Any]]: ...

    async def _list_tools_from_session(self, session: ClientSession) -> list[dict[str, Any]]: ...

    async def _retry_list_tools_after_failure(
        self,
        server_name: str,
        initial_error: Exception,
    ) -> list[dict[str, Any]]: ...


def _validated_input_schema(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


async def list_tools(
    manager: _ToolInventoryManager,
    server_name: str | None,
    logger: logging.Logger,
) -> dict[str, list[dict[str, Any]]]:
    """List tools from one server or all active connections."""
    results: dict[str, list[dict[str, Any]]] = {}
    if server_name:
        try:
            return {server_name: await manager._list_tools_for_server(server_name)}
        except Exception as exc:
            logger.warning("Failed to list tools for %s: %s", server_name, exc)
            return {server_name: []}

    for name in list(manager._connections.keys()):
        try:
            results[name] = await manager._list_tools_for_server(name)
        except Exception as exc:
            logger.warning("Failed to list tools for %s: %s", name, exc)
            results[name] = []

    return results


async def list_tools_for_server(
    manager: _ToolInventoryManager,
    server_name: str,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    """List tools for one server and retry after stale-session failures."""
    try:
        session = await manager.get_client_session(server_name)
        tool_list = await manager._list_tools_from_session(session)
    except Exception as initial_error:
        error_message = describe_exception(initial_error)
        logger.warning("Failed to list tools for %s: %s", server_name, error_message)
        if server_name in manager.health:
            manager.health[server_name].record_failure(error_message)
        return await manager._retry_list_tools_after_failure(server_name, initial_error)

    if server_name in manager.health:
        manager.health[server_name].record_success()
    manager.cache_discovered_tools(server_name, tool_list)
    return tool_list


async def retry_list_tools_after_failure(
    manager: _ToolInventoryManager,
    server_name: str,
    initial_error: Exception,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    """Discard stale connection state and retry tool listing once."""
    await discard_connection(
        server_name,
        manager._connections,
        manager.health,
        manager._lazy_connector,
        logger,
    )
    try:
        session = await manager.ensure_connected(server_name)
        tool_list = await manager._list_tools_from_session(session)
    except Exception as retry_error:
        retry_message = describe_exception(retry_error)
        if server_name in manager.health:
            manager.health[server_name].record_failure(retry_message)
        raise MCPError(
            f"Failed to list tools for server '{server_name}': "
            f"initial listing failed: {describe_exception(initial_error)}; "
            f"reconnect retry failed: {retry_message}"
        ) from retry_error

    if server_name in manager.health:
        manager.health[server_name].record_success()
    manager.cache_discovered_tools(server_name, tool_list)
    return tool_list


async def list_tools_from_session(session: ClientSession) -> list[dict[str, Any]]:
    """Convert MCP SDK list_tools result to response dictionaries."""
    tools = await session.list_tools()
    if not hasattr(tools, "tools"):
        return []
    return [
        {
            "name": tool.name,
            "description": getattr(tool, "description", "") or "",
            "inputSchema": _validated_input_schema(getattr(tool, "inputSchema", {})),
        }
        for tool in tools.tools
    ]


def cache_discovered_tools(
    manager: _ToolInventoryManager,
    server_name: str,
    tools: list[dict[str, Any]],
) -> None:
    """Cache discovered full tool schemas and update config summaries."""
    config = manager._configs.get(server_name)
    if not config or not manager.mcp_db_manager or not config.project_id:
        return

    try:
        manager.mcp_db_manager.cache_tools(server_name, tools, project_id=config.project_id)
        config.tools = [
            {"name": tool["name"], "brief": truncate_tool_brief(tool.get("description"))}
            for tool in tools
        ]
    except Exception as exc:
        logging.getLogger("gobby.mcp.manager").debug(
            "Failed to cache tools for %s: %s",
            server_name,
            exc,
        )


async def get_tool_input_schema(
    manager: _ToolInventoryManager,
    server_name: str,
    tool_name: str,
) -> dict[str, Any]:
    """Return the input schema for one tool."""
    tool_info = await manager.get_tool_info(server_name, tool_name)
    return _validated_input_schema(tool_info.get("inputSchema", {}))


async def get_tool_info(
    manager: _ToolInventoryManager,
    server_name: str,
    tool_name: str,
) -> dict[str, Any]:
    """Return full tool info for one tool by filtering list_tools output."""
    server_tools = await manager._list_tools_for_server(server_name)

    for tool in server_tools:
        if not isinstance(tool, dict):
            continue
        found_name = tool.get("name")
        if found_name == tool_name:
            result: dict[str, Any] = {"name": found_name}
            if "description" in tool and tool["description"]:
                result["description"] = tool["description"]
            if "inputSchema" in tool:
                result["inputSchema"] = _validated_input_schema(tool["inputSchema"])
            return result

    raise MCPError(f"Tool {tool_name} not found on server {server_name}")

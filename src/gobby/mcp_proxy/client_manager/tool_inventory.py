"""Tool inventory helpers for MCP client manager."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from mcp import ClientSession

from gobby.mcp_proxy.client_manager.server_registry import truncate_tool_brief, visible_configs
from gobby.mcp_proxy.connection_cleanup import describe_exception, discard_connection
from gobby.mcp_proxy.models import MCPError


class _ToolInventoryManager(Protocol):
    _connections: dict[str, Any]
    _configs: dict[str, Any]
    _lazy_connector: Any
    _tool_schema_cache: dict[str, list[dict[str, Any]]]
    _tool_cache_dirty: set[str]
    health: dict[str, Any]
    mcp_db_manager: Any | None

    def cache_discovered_tools(self, server_id: str, tools: list[dict[str, Any]]) -> None: ...

    def has_server(self, server_id: str) -> bool: ...

    async def ensure_connected(self, server_id: str) -> ClientSession: ...

    async def get_client_session(self, server_id: str) -> ClientSession: ...

    async def get_tool_info(self, server_id: str, tool_name: str) -> dict[str, Any]: ...

    async def _list_tools_for_server(self, server_id: str) -> list[dict[str, Any]]: ...

    async def _list_tools_from_session(self, session: ClientSession) -> list[dict[str, Any]]: ...

    async def _retry_list_tools_after_failure(
        self,
        server_id: str,
        initial_error: Exception,
    ) -> list[dict[str, Any]]: ...


def _validated_input_schema(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


async def list_tools(
    manager: _ToolInventoryManager,
    server_id: str | None,
    logger: logging.Logger,
    *,
    project_id: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """List tools for one server or the caller-visible project inventory."""
    if project_id is not None:
        results: dict[str, list[dict[str, Any]]] = {}
        for config in visible_configs(manager, project_id):
            cached = manager._tool_schema_cache.get(config.id)
            if cached is not None:
                results[config.name] = cached
                continue
            try:
                results[config.name] = await manager._list_tools_for_server(config.id)
            except Exception as exc:
                logger.warning("Failed to list tools for %s: %s", config.name, exc)
                results[config.name] = []
        return results

    if server_id:
        stored = manager._configs.get(server_id)
        name = stored.name if stored is not None else server_id
        return {name: await manager._list_tools_for_server(server_id)}

    results = {}
    for current_id in list(manager._connections.keys()):
        stored = manager._configs.get(current_id)
        name = stored.name if stored is not None else current_id
        try:
            results[name] = await manager._list_tools_for_server(current_id)
        except Exception as exc:
            logger.warning("Failed to list tools for %s: %s", name, exc)
            results[name] = []
    return results


async def list_tools_for_server(
    manager: _ToolInventoryManager,
    server_id: str,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    """List tools for one server and retry after stale-session failures."""
    if not manager.has_server(server_id):
        raise KeyError(f"Server '{server_id}' not configured")
    config = manager._configs.get(server_id)
    label = config.name if config is not None else server_id
    try:
        session = await manager.get_client_session(server_id)
        tool_list = await manager._list_tools_from_session(session)
    except Exception as initial_error:
        error_message = describe_exception(initial_error)
        logger.warning("Failed to list tools for %s: %s", label, error_message)
        if server_id in manager.health:
            manager.health[server_id].record_failure(error_message)
        return await manager._retry_list_tools_after_failure(server_id, initial_error)

    if server_id in manager.health:
        manager.health[server_id].record_success()
    await asyncio.to_thread(manager.cache_discovered_tools, server_id, tool_list)
    return tool_list


async def retry_list_tools_after_failure(
    manager: _ToolInventoryManager,
    server_id: str,
    initial_error: Exception,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    """Discard stale connection state and retry tool listing once."""
    used = manager._connections.get(server_id)
    await discard_connection(
        server_id,
        manager._connections,
        manager.health,
        manager._lazy_connector,
        logger,
        tool_schema_cache=manager._tool_schema_cache,
        expected=used,
    )
    config = manager._configs.get(server_id)
    label = config.name if config is not None else server_id
    try:
        session = await manager.ensure_connected(server_id)
        tool_list = await manager._list_tools_from_session(session)
    except Exception as retry_error:
        retry_message = describe_exception(retry_error)
        if server_id in manager.health:
            manager.health[server_id].record_failure(retry_message)
        raise MCPError(
            f"Failed to list tools for server '{label}': "
            f"initial listing failed: {describe_exception(initial_error)}; "
            f"reconnect retry failed: {retry_message}"
        ) from retry_error

    if server_id in manager.health:
        manager.health[server_id].record_success()
    await asyncio.to_thread(manager.cache_discovered_tools, server_id, tool_list)
    return tool_list


async def list_tools_from_session(session: ClientSession) -> list[dict[str, Any]]:
    """Convert MCP SDK list_tools result to response dictionaries."""
    tools = await session.list_tools()
    return [
        {
            "name": tool.name,
            "description": tool.description or "",
            "inputSchema": _validated_input_schema(tool.input_schema),
        }
        for tool in tools.tools
    ]


def cache_discovered_tools(
    manager: _ToolInventoryManager,
    server_id: str,
    tools: list[dict[str, Any]],
) -> None:
    """Cache discovered full tool schemas and update config summaries."""
    if (
        manager._tool_schema_cache.get(server_id) == tools
        and server_id not in manager._tool_cache_dirty
    ):
        return
    manager._tool_schema_cache[server_id] = tools

    config = manager._configs.get(server_id)
    if not config:
        return

    try:
        config.tools = [
            {"name": tool["name"], "brief": truncate_tool_brief(tool.get("description"))}
            for tool in tools
        ]
        if manager.mcp_db_manager:
            manager.mcp_db_manager.cache_tools(server_id, tools)
        manager._tool_cache_dirty.discard(server_id)
    except Exception as exc:
        manager._tool_cache_dirty.add(server_id)
        logging.getLogger("gobby.mcp.manager").debug(
            "Failed to cache tools for %s: %s",
            server_id,
            exc,
        )


async def get_tool_input_schema(
    manager: _ToolInventoryManager,
    server_id: str,
    tool_name: str,
) -> dict[str, Any]:
    """Return the input schema for one tool."""
    tool_info = await manager.get_tool_info(server_id, tool_name)
    return _validated_input_schema(tool_info.get("inputSchema", {}))


async def get_tool_info(
    manager: _ToolInventoryManager,
    server_id: str,
    tool_name: str,
) -> dict[str, Any]:
    """Return full tool info for one tool by filtering list_tools output."""
    server_tools = manager._tool_schema_cache.get(server_id)
    if server_tools is None:
        server_tools = await manager._list_tools_for_server(server_id)
        manager._tool_schema_cache[server_id] = server_tools

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

    config = manager._configs.get(server_id)
    label = config.name if config is not None else server_id
    raise MCPError(f"Tool {tool_name} not found on server {label}")

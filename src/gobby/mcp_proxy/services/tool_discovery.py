"""Project-scoped tool inventory and schema discovery for the MCP proxy."""

import asyncio
import logging
from typing import Any, cast

from gobby.mcp_proxy.models import MCPError, ToolProxyErrorCode
from gobby.mcp_proxy.services.server_resolution import caller_project_id, resolve_server
from gobby.mcp_proxy.services.tool_proxy_utils import safe_truncate

logger = logging.getLogger("gobby.mcp.server")


async def list_tools(
    service: Any,
    server_name: str,
    session_id: str | None = None,
    *,
    project_id: str | None = None,
    scope: str | None = None,
    enforce_workflow: bool = False,
) -> dict[str, Any]:
    """List tools for a specific server with progressive discovery format."""
    server_name = service._resolve_server_name(server_name)
    project_id = caller_project_id(service, project_id=project_id, scope=scope)
    if service._is_proxy_namespace(server_name):
        logger.debug("list_tools called with server_name='gobby' — aggregating all internal tools")
        if service._internal_manager:
            brief_tools: list[dict[str, Any]] = []
            for reg in service._internal_manager.get_all_registries():
                for tool in reg.list_tools():
                    name = (
                        tool.get("name", "unknown")
                        if isinstance(tool, dict)
                        else getattr(tool, "name", "unknown")
                    )
                    desc = (
                        tool.get("description", "")
                        if isinstance(tool, dict)
                        else getattr(tool, "description", "")
                    )
                    brief_tools.append({"name": name, "brief": safe_truncate(desc)})
            if enforce_workflow and service._tool_filter and session_id:
                brief_tools = await asyncio.to_thread(
                    service._tool_filter.filter_tools, brief_tools, session_id
                )
            return {"success": True, "tools": brief_tools, "tool_count": len(brief_tools)}
        return {"success": True, "tools": [], "tool_count": 0}

    if service._internal_manager and service._internal_manager.is_internal(server_name):
        registry = service._internal_manager.get_registry(server_name)
        if registry:
            tools = registry.list_tools()
            if enforce_workflow and service._tool_filter and session_id:
                tools = await asyncio.to_thread(
                    service._tool_filter.filter_tools, tools, session_id
                )
            if enforce_workflow:
                await asyncio.to_thread(
                    service.record_listed_server, server_name, session_id=session_id
                )
            return {"success": True, "tools": tools, "tool_count": len(tools)}
        error_msg = f"Internal server '{server_name}' not found"
        suggestion = service._get_server_suggestion(server_name)
        if suggestion:
            error_msg += f". Did you mean '{suggestion}'?"
        return {
            "success": False,
            "tools": [],
            "error": error_msg,
        }

    config = resolve_server(service, server_name, project_id=project_id)
    if config is not None:
        try:
            tools_map = await service._mcp_manager.list_tools(config.id)
        except MCPError as exc:
            return {
                "success": False,
                "tools": [],
                "error": str(exc),
            }
        tools_list = tools_map.get(config.name, tools_map.get(config.id, []))
        ext_brief_tools: list[dict[str, Any]] = []
        for tool in tools_list:
            if isinstance(tool, dict):
                ext_brief_tools.append(
                    {
                        "name": tool.get("name", "unknown"),
                        "brief": safe_truncate(tool.get("description", "")),
                    }
                )
            else:
                ext_brief_tools.append(
                    {
                        "name": tool.name,
                        "brief": safe_truncate(tool.description),
                    }
                )
        if enforce_workflow and service._tool_filter and session_id:
            ext_brief_tools = await asyncio.to_thread(
                service._tool_filter.filter_tools, ext_brief_tools, session_id
            )
        if enforce_workflow:
            await asyncio.to_thread(
                service.record_listed_server, server_name, session_id=session_id
            )
        return {"success": True, "tools": ext_brief_tools, "tool_count": len(ext_brief_tools)}

    error_msg = f"Server '{server_name}' not found"
    suggestion = service._get_server_suggestion(server_name)
    if suggestion:
        error_msg += f". Did you mean '{suggestion}'?"
    return {
        "success": False,
        "tools": [],
        "error": error_msg,
    }


async def get_tool_schema(
    service: Any,
    server_name: str,
    tool_name: str,
    *,
    project_id: str | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    """Get full schema for a specific tool."""
    server_name = service._resolve_server_name(server_name)
    project_id = caller_project_id(service, project_id=project_id, scope=scope)
    if service._is_proxy_namespace(server_name):
        resolved = service._resolve_server_for_tool(tool_name)
        if resolved:
            return cast(
                "dict[str, Any]",
                await service.get_tool_schema(
                    resolved, tool_name, project_id=project_id, scope=scope
                ),
            )
        return {
            "success": False,
            "error": (
                f"Tool '{tool_name}' not found on any server "
                "(server_name='gobby' is not a real server — use list_mcp_servers() "
                "to discover server names)"
            ),
            "error_code": ToolProxyErrorCode.SERVER_NOT_FOUND.value,
        }

    if service._internal_manager and service._internal_manager.is_internal(server_name):
        registry = service._internal_manager.get_registry(server_name)
        if registry:
            schema = registry.get_schema(tool_name)
            if schema:
                return {"success": True, "tool": schema}
            return {
                "success": False,
                "error": f"Tool '{tool_name}' not found on '{server_name}'",
            }

        error_msg = f"Internal server '{server_name}' not found"
        suggestion = service._get_server_suggestion(server_name)
        if suggestion:
            error_msg += f". Did you mean '{suggestion}'?"
        return {"success": False, "error": error_msg}

    config = resolve_server(service, server_name, project_id=project_id)
    if config is None:
        error_msg = f"Server '{server_name}' not found in project scope {project_id}"
        suggestion = service._get_server_suggestion(server_name)
        if suggestion:
            error_msg += f". Did you mean '{suggestion}'?"
        return {"success": False, "error": error_msg}

    try:
        tool_info = await service._mcp_manager.get_tool_info(config.id, tool_name)
        return {"success": True, "tool": cast("dict[str, Any]", tool_info)}
    except Exception as e:
        raise MCPError(f"Failed to get schema for {tool_name} on {server_name}: {e}") from e

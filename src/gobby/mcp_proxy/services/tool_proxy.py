"""Tool proxy service."""

import logging
from typing import Any

from mcp import ListToolsResult, Tool
from pydantic import AnyUrl

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import MCPError

logger = logging.getLogger("gobby.mcp.server")


class ToolProxyService:
    """Service for proxying tool calls and resource reads to underlying MCP servers."""

    def __init__(self, mcp_manager: MCPClientManager, internal_tools: dict[str, Any] | None = None):
        self._mcp_manager = mcp_manager
        self._internal_tools = internal_tools or {}

    async def list_tools(self, server_name: str | None = None) -> ListToolsResult:
        """List all available tools."""
        all_tools = []

        # 1. Add internal tools if no specific server or server is internal
        if not server_name or server_name in ("gobby-tasks", "gobby-hooks"):
            # Internal tool handling (simplified for this extraction)
            # In a real scenario, we'd iterate self._internal_tools
            # But internal tools are not fully standardized in this mock yet
            # So we defer to how server.py handled them (it filtered by name)
            pass

        if server_name:
            # Just one server
            if server_name in self._mcp_manager._connections:
                tools_map = await self._mcp_manager.list_tools(server_name)
                if server_name in tools_map:
                    # Access tools safely (it might be a Pydantic model)
                    tools_list = tools_map[server_name]
                    # Convert to Tool Pydantic objects if they are dicts
                    for tool in tools_list:
                        if isinstance(tool, dict):
                            # Ensure name is tool.name
                            tool_obj = Tool.model_validate(tool)
                            all_tools.append(tool_obj)
                        else:
                            all_tools.append(tool)
        else:
            # All servers
            tools_map = await self._mcp_manager.list_tools()
            for s_name, t_list in tools_map.items():
                for tool in t_list:
                    if isinstance(tool, dict):
                        tool_obj = Tool.model_validate(tool)
                        all_tools.append(tool_obj)
                    else:
                        all_tools.append(tool)

        return ListToolsResult(tools=all_tools)

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        """Execute a tool."""
        # Check internal tools first
        if server_name.startswith("gobby-") and server_name in self._internal_tools:
            tool_func = self._internal_tools[server_name].get(tool_name)
            if tool_func:
                return await tool_func(arguments or {})
            else:
                raise MCPError(f"Tool {tool_name} not found in internal server {server_name}")

        # Use MCP manager
        return await self._mcp_manager.call_tool(server_name, tool_name, arguments)

    async def read_resource(self, server_name: str, uri: str) -> Any:
        """Read a resource."""
        return await self._mcp_manager.read_resource(server_name, uri)

    async def get_tool_schema(self, server_name: str, tool_name: str) -> dict[str, Any]:
        """Get schema for a tool."""
        return await self._mcp_manager.get_tool_input_schema(server_name, tool_name)

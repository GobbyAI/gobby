"""
MCP endpoint modules for the Gobby HTTP server.

This package contains decomposed endpoint handlers extracted from tools.py
using the Strangler Fig pattern. Each module handles a specific domain:

- discovery: Tool and server listing endpoints
- execution: Tool invocation endpoints
- server: Server management (add/remove/import)
- registry: Tool embedding, status, and refresh

External modules should import `create_mcp_router` from the parent package:
    from gobby.servers.routes.mcp import create_mcp_router

For direct endpoint access (e.g., testing), import from submodules:
    from gobby.servers.routes.mcp.endpoints.discovery import list_all_mcp_tools
"""

from gobby.servers.routes.mcp.endpoints.discovery import (
    list_all_mcp_tools,
    recommend_mcp_tools,
    search_mcp_tools,
)
from gobby.servers.routes.mcp.endpoints.execution import (
    call_mcp_tool,
    get_tool_schema,
    list_mcp_tools,
    mcp_proxy,
)
from gobby.servers.routes.mcp.endpoints.registry import (
    embed_mcp_tools,
    get_mcp_status,
    refresh_mcp_tools,
)
from gobby.servers.routes.mcp.endpoints.server import (
    add_mcp_server,
    import_mcp_server,
    list_mcp_servers,
    remove_mcp_server,
    set_mcp_server_enabled,
)

__all__ = [
    # Discovery
    "list_all_mcp_tools",
    "recommend_mcp_tools",
    "search_mcp_tools",
    # Execution
    "call_mcp_tool",
    "get_tool_schema",
    "list_mcp_tools",
    "mcp_proxy",
    # Registry
    "embed_mcp_tools",
    "get_mcp_status",
    "refresh_mcp_tools",
    # Server
    "add_mcp_server",
    "import_mcp_server",
    "list_mcp_servers",
    "remove_mcp_server",
    "set_mcp_server_enabled",
]

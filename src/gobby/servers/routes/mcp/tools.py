"""
MCP routes for Gobby HTTP server.

Thin router aggregation layer that composes endpoints from:
- endpoints/discovery.py - Tool listing and search
- endpoints/execution.py - Tool calls and schema retrieval
- endpoints/server.py - Server management
- endpoints/registry.py - Embedding, status, and refresh
"""

from fastapi import APIRouter

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
)


def create_mcp_router() -> APIRouter:
    """
    Create MCP router with endpoints using dependency injection.

    Returns:
        Configured APIRouter with MCP endpoints
    """
    router = APIRouter(prefix="/api/mcp", tags=["mcp"])

    # Execution endpoints from endpoints/execution.py
    router.get("/{server_name}/tools")(list_mcp_tools)
    router.post("/tools/schema")(get_tool_schema)
    router.post("/tools/call")(call_mcp_tool)
    router.post("/{server_name}/tools/{tool_name}")(mcp_proxy)

    # Server management endpoints from endpoints/server.py
    router.get("/servers")(list_mcp_servers)
    router.post("/servers")(add_mcp_server)
    router.post("/servers/import")(import_mcp_server)
    router.delete("/servers/{name}")(remove_mcp_server)

    # Discovery endpoints from endpoints/discovery.py
    router.get("/tools")(list_all_mcp_tools)
    router.post("/tools/recommend")(recommend_mcp_tools)
    router.post("/tools/search")(search_mcp_tools)

    # Registry endpoints from endpoints/registry.py
    router.post("/tools/embed")(embed_mcp_tools)
    router.get("/status")(get_mcp_status)
    router.post("/refresh")(refresh_mcp_tools)

    return router

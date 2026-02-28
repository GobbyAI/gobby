"""
Server management endpoints for MCP server lifecycle.

Extracted from tools.py as part of Phase 2 Strangler Fig decomposition.
These endpoints handle server listing, addition, import, and removal.
"""

import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import Depends, HTTPException, Request

from gobby.servers.routes.dependencies import get_internal_manager, get_mcp_manager, get_server
from gobby.utils.metrics import get_metrics_collector

if TYPE_CHECKING:
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.mcp_proxy.registry_manager import InternalToolRegistryManager
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

# Module-level metrics collector (shared across all requests)
_metrics = get_metrics_collector()


async def list_mcp_servers(
    internal_manager: "InternalToolRegistryManager | None" = Depends(get_internal_manager),
    mcp_manager: "MCPClientManager | None" = Depends(get_mcp_manager),
) -> dict[str, Any]:
    """
    List all configured MCP servers.

    Args:
        internal_manager: Internal tool registry manager (injected)
        mcp_manager: External MCP client manager (injected)

    Returns:
        List of servers with connection status
    """
    _metrics.inc_counter("http_requests_total")

    try:
        server_list = []

        # Add internal servers (gobby-tasks, gobby-memory, etc.)
        if internal_manager:
            for registry in internal_manager.get_all_registries():
                server_list.append(
                    {
                        "name": registry.name,
                        "state": "connected",
                        "transport": "internal",
                    }
                )

        # Add external MCP servers
        connected_count = len(server_list)  # all internal servers are connected
        if mcp_manager:
            for config in mcp_manager.server_configs:
                health = mcp_manager.health.get(config.name)
                state = health.state.value if health else "unknown"
                is_connected = config.name in mcp_manager.connections
                if is_connected:
                    connected_count += 1
                entry: dict[str, Any] = {
                    "name": config.name,
                    "state": state,
                    "transport": config.transport,
                }
                if not config.enabled:
                    entry["enabled"] = False
                server_list.append(entry)

        return {
            "success": True,
            "servers": server_list,
            "total": len(server_list),
            "connected": connected_count,
        }

    except Exception as e:
        _metrics.inc_counter("http_requests_errors_total")
        logger.error(f"List MCP servers error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def add_mcp_server(
    request: Request,
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """
    Add a new MCP server configuration.

    Request body:
        {
            "name": "my-server",
            "transport": "http",
            "url": "https://...",
            "enabled": true
        }

    Returns:
        Success status
    """
    start_time = time.perf_counter()
    _metrics.inc_counter("http_requests_total")

    try:
        body = await request.json()
        name = body.get("name")
        transport = body.get("transport")

        if not name or not transport:
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": "Required fields: name, transport"},
            )

        # Import here to avoid circular imports
        from gobby.mcp_proxy.models import MCPServerConfig
        from gobby.utils.project_context import get_project_context

        project_ctx = get_project_context()
        if not project_ctx or not project_ctx.get("id"):
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": "No current project found. Run 'gobby init'."},
            )
        project_id = project_ctx["id"]

        config = MCPServerConfig(
            name=name,
            project_id=project_id,
            transport=transport,
            url=body.get("url"),
            command=body.get("command"),
            args=body.get("args"),
            env=body.get("env"),
            headers=body.get("headers"),
            enabled=body.get("enabled", True),
        )

        if server.mcp_manager is None:
            response_time_ms = (time.perf_counter() - start_time) * 1000
            return {
                "success": False,
                "error": "MCP manager not available",
                "response_time_ms": response_time_ms,
            }

        await server.mcp_manager.add_server(config)

        # Broadcast MCP server added event
        ws = server.services.websocket_server
        if ws:
            try:
                await ws.broadcast_mcp_event("server_added", name)
            except Exception as e:
                logger.debug(f"Failed to broadcast mcp event server_added: {e}")

        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {
            "success": True,
            "message": f"Added MCP server: {name}",
            "response_time_ms": response_time_ms,
        }

    except HTTPException:
        raise
    except ValueError as e:
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}
    except Exception as e:
        _metrics.inc_counter("http_requests_errors_total")
        logger.error(f"Add MCP server error: {e}", exc_info=True)
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}


async def import_mcp_server(
    request: Request,
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """
    Import MCP server(s) from various sources.

    Request body:
        {
            "from_project": "other-project",  # Import from project
            "github_url": "https://...",      # Import from GitHub
            "query": "supabase mcp",          # Search and import
            "servers": ["name1", "name2"]     # Specific servers to import
        }

    Returns:
        Import result with imported/skipped/failed lists
    """
    start_time = time.perf_counter()
    _metrics.inc_counter("http_requests_total")

    try:
        body = await request.json()
        from_project = body.get("from_project")
        github_url = body.get("github_url")
        query = body.get("query")
        servers = body.get("servers")

        if not from_project and not github_url and not query:
            raise HTTPException(
                status_code=400,
                detail={
                    "success": False,
                    "error": "Specify at least one: from_project, github_url, or query",
                },
            )

        # Get current project ID from context
        from gobby.utils.project_context import get_project_context

        project_ctx = get_project_context()
        if not project_ctx or not project_ctx.get("id"):
            response_time_ms = (time.perf_counter() - start_time) * 1000
            return {
                "success": False,
                "error": "No current project. Run 'gobby init' first.",
                "response_time_ms": response_time_ms,
            }
        current_project_id = project_ctx["id"]

        if not server.config:
            response_time_ms = (time.perf_counter() - start_time) * 1000
            return {
                "success": False,
                "error": "Daemon configuration not available",
                "response_time_ms": response_time_ms,
            }

        # Create importer
        from gobby.mcp_proxy.importer import MCPServerImporter
        from gobby.storage.database import LocalDatabase

        db = LocalDatabase()
        importer = MCPServerImporter(
            config=server.config,
            db=db,
            current_project_id=current_project_id,
            mcp_client_manager=server.mcp_manager,
        )

        # Execute import based on source
        # Note: validation above ensures at least one of these is truthy
        if from_project:
            result = await importer.import_from_project(
                source_project=from_project,
                servers=servers,
            )
        elif github_url:
            result = await importer.import_from_github(github_url)
        else:
            # query must be truthy due to earlier validation
            result = await importer.import_from_query(query)

        # Broadcast MCP server imported event
        ws = server.services.websocket_server
        if ws:
            try:
                await ws.broadcast_mcp_event("server_imported", "bulk")
            except Exception as e:
                logger.debug(f"Failed to broadcast mcp event server_imported: {e}")

        response_time_ms = (time.perf_counter() - start_time) * 1000
        if isinstance(result, dict):
            result["response_time_ms"] = response_time_ms
            return result
        return {"success": True, "data": result, "response_time_ms": response_time_ms}

    except HTTPException:
        raise
    except Exception as e:
        _metrics.inc_counter("http_requests_errors_total")
        logger.error(f"Import MCP server error: {e}", exc_info=True)
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}


async def remove_mcp_server(
    name: str,
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """
    Remove an MCP server configuration.

    Args:
        name: Server name to remove

    Returns:
        Success status
    """
    start_time = time.perf_counter()
    _metrics.inc_counter("http_requests_total")

    try:
        if server.mcp_manager is None:
            response_time_ms = (time.perf_counter() - start_time) * 1000
            return {
                "success": False,
                "error": "MCP manager not available",
                "response_time_ms": response_time_ms,
            }

        await server.mcp_manager.remove_server(name)

        # Broadcast MCP server removed event
        ws = server.services.websocket_server
        if ws:
            try:
                await ws.broadcast_mcp_event("server_removed", name)
            except Exception as e:
                logger.debug(f"Failed to broadcast mcp event server_removed: {e}")

        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {
            "success": True,
            "message": f"Removed MCP server: {name}",
            "response_time_ms": response_time_ms,
        }

    except ValueError as e:
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}
    except Exception as e:
        _metrics.inc_counter("http_requests_errors_total")
        logger.error(f"Remove MCP server error: {e}", exc_info=True)
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}


__all__ = [
    "list_mcp_servers",
    "add_mcp_server",
    "import_mcp_server",
    "remove_mcp_server",
]

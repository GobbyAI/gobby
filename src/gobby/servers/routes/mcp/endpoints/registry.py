"""
Registry endpoints for MCP tool embedding, status, and refresh.

Extracted from tools.py as part of Phase 2 Strangler Fig decomposition.
These endpoints handle tool registry operations like embedding, status, and refresh.
"""

import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import Depends, Request

from gobby.servers.routes.dependencies import get_server

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


def _mcp_manager_is_connected(mcp_manager: Any, name: str) -> bool:
    is_connected = getattr(type(mcp_manager), "is_connected", None)
    if callable(is_connected):
        return bool(mcp_manager.is_connected(name))

    connections = getattr(mcp_manager, "connections", None)
    return isinstance(connections, dict) and name in connections


async def embed_mcp_tools(
    request: Request,
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """
    Generate embeddings for all tools in a project.

    Request body:
        {
            "cwd": "/path/to/project",
            "force": false
        }

    Returns:
        Embedding generation stats
    """
    start_time = time.perf_counter()

    try:
        body = await request.json()
        cwd = body.get("cwd")

        # Resolve project_id from cwd
        try:
            project_id = server.resolve_project_id(None, cwd)
        except ValueError as e:
            return {
                "success": False,
                "error": str(e),
                "response_time_ms": (time.perf_counter() - start_time) * 1000,
            }

        # Use semantic search to embed all tools
        if server._tools_handler and server._tools_handler._semantic_search:
            try:
                stats = await server._tools_handler._semantic_search.embed_all_tools(
                    project_id=project_id,
                    mcp_manager=server._mcp_db_manager,
                    internal_manager=server._internal_manager,
                )
                response_time_ms = (time.perf_counter() - start_time) * 1000
                return {
                    "success": True,
                    "stats": stats,
                    "response_time_ms": response_time_ms,
                }
            except Exception as e:
                logger.error(f"Embedding generation failed: {e}")
                return {
                    "success": False,
                    "error": str(e),
                    "response_time_ms": (time.perf_counter() - start_time) * 1000,
                }

        return {
            "success": False,
            "error": "Semantic search not configured",
            "response_time_ms": (time.perf_counter() - start_time) * 1000,
        }

    except Exception as e:
        logger.error(f"Embed tools error: {e}", exc_info=True)
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}


async def get_mcp_status(
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """
    Get MCP proxy status and health.

    Returns:
        Status summary with server counts and health info
    """
    start_time = time.perf_counter()

    try:
        total_servers = 0
        connected_servers = 0
        cached_tools = 0
        server_health: dict[str, dict[str, Any]] = {}

        # Count internal servers
        if server._internal_manager:
            for registry in server._internal_manager.get_all_registries():
                total_servers += 1
                connected_servers += 1
                cached_tools += len(registry.list_tools())
                server_health[registry.name] = {
                    "state": "connected",
                    "health": "healthy",
                    "failures": 0,
                }

        # Count external servers
        if server.mcp_manager:
            for config in server.mcp_manager.server_configs:
                total_servers += 1
                health = server.mcp_manager.health.get(config.name)
                is_connected = _mcp_manager_is_connected(server.mcp_manager, config.name)
                if is_connected:
                    connected_servers += 1

                server_health[config.name] = {
                    "state": health.state.value if health else "unknown",
                    "health": health.health.value if health else "unknown",
                    "failures": health.consecutive_failures if health else 0,
                }

        response_time_ms = (time.perf_counter() - start_time) * 1000

        return {
            "success": True,
            "total_servers": total_servers,
            "connected_servers": connected_servers,
            "cached_tools": cached_tools,
            "server_health": server_health,
            "response_time_ms": response_time_ms,
        }

    except Exception as e:
        logger.error(f"Get MCP status error: {e}", exc_info=True)
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}


async def refresh_mcp_tools(
    request: Request,
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """
    Refresh MCP tools - detect schema changes and re-index as needed.

    Request body:
        {
            "cwd": "/path/to/project",
            "force": false,
            "server": "optional-server-filter"
        }

    Returns:
        Refresh stats with new/changed/unchanged tool counts
    """
    start_time = time.perf_counter()

    try:
        body = await request.json()
        cwd = body.get("cwd")
        force = body.get("force", False)
        server_filter = body.get("server")

        # Resolve project_id from cwd
        try:
            project_id = server.resolve_project_id(None, cwd)
        except ValueError as e:
            return {
                "success": False,
                "error": str(e),
                "response_time_ms": (time.perf_counter() - start_time) * 1000,
            }

        # Need schema hash manager and semantic search
        if not server._mcp_db_manager:
            return {
                "success": False,
                "error": "MCP database manager not configured",
                "response_time_ms": (time.perf_counter() - start_time) * 1000,
            }

        from gobby.mcp_proxy.schema_hash import SchemaHashManager, compute_schema_hash

        schema_hash_manager = SchemaHashManager(db=server._mcp_db_manager.db)
        semantic_search = (
            getattr(server._tools_handler, "_semantic_search", None)
            if server._tools_handler
            else None
        )

        stats: dict[str, Any] = {
            "servers_processed": 0,
            "tools_new": 0,
            "tools_changed": 0,
            "tools_unchanged": 0,
            "tools_removed": 0,
            "embeddings_generated": 0,
            "by_server": {},
        }

        # Collect servers to process
        servers_to_process: list[str] = []

        # Internal servers
        if server._internal_manager:
            for registry in server._internal_manager.get_all_registries():
                if server_filter is None or registry.name == server_filter:
                    servers_to_process.append(registry.name)

        # External MCP servers
        if server.mcp_manager:
            for config in server.mcp_manager.server_configs:
                if config.enabled:
                    if server_filter is None or config.name == server_filter:
                        servers_to_process.append(config.name)

        # Process each server
        for server_name in servers_to_process:
            try:
                tools: list[dict[str, Any]] = []

                # Get tools from internal or external server
                if server._internal_manager and server._internal_manager.is_internal(server_name):
                    internal_registry = server._internal_manager.get_registry(server_name)
                    if internal_registry:
                        for t in internal_registry.list_tools():
                            tool_name = t.get("name", "")
                            tools.append(
                                {
                                    "name": tool_name,
                                    "description": t.get("description"),
                                    "inputSchema": internal_registry.get_schema(tool_name),
                                }
                            )
                elif server.mcp_manager:
                    try:
                        session = await server.mcp_manager.ensure_connected(server_name)
                        tools_result = await session.list_tools()
                        for mcp_tool in tools_result.tools:
                            schema = None
                            if hasattr(mcp_tool, "inputSchema"):
                                if hasattr(mcp_tool.inputSchema, "model_dump"):
                                    schema = mcp_tool.inputSchema.model_dump()
                                elif isinstance(mcp_tool.inputSchema, dict):
                                    schema = mcp_tool.inputSchema
                            tools.append(
                                {
                                    "name": getattr(mcp_tool, "name", ""),
                                    "description": getattr(mcp_tool, "description", ""),
                                    "inputSchema": schema,
                                }
                            )
                    except Exception as e:
                        logger.warning(f"Failed to connect to {server_name}: {e}")
                        stats["by_server"][server_name] = {"error": str(e)}
                        continue

                # Check for schema changes
                if force:
                    # Force mode: treat all as new
                    changes = {
                        "new": [t["name"] for t in tools],
                        "changed": [],
                        "unchanged": [],
                    }
                else:
                    changes = schema_hash_manager.check_tools_for_changes(
                        server_name=server_name,
                        project_id=project_id,
                        tools=tools,
                    )

                server_stats = {
                    "new": len(changes["new"]),
                    "changed": len(changes["changed"]),
                    "unchanged": len(changes["unchanged"]),
                    "removed": 0,
                    "embeddings": 0,
                }

                # Update schema hashes for new/changed tools
                tools_to_embed = []
                for tool in tools:
                    tool_name = tool["name"]
                    if tool_name in changes["new"] or tool_name in changes["changed"]:
                        schema = tool.get("inputSchema")
                        schema_hash = compute_schema_hash(schema)
                        schema_hash_manager.store_hash(
                            server_name=server_name,
                            tool_name=tool_name,
                            project_id=project_id,
                            schema_hash=schema_hash,
                        )
                        tools_to_embed.append(tool)
                    else:
                        # Just update verification time for unchanged
                        schema_hash_manager.update_verification_time(
                            server_name=server_name,
                            tool_name=tool_name,
                            project_id=project_id,
                        )

                # Clean up stale hashes
                valid_tool_names = [t["name"] for t in tools]
                removed = schema_hash_manager.cleanup_stale_hashes(
                    server_name=server_name,
                    project_id=project_id,
                    valid_tool_names=valid_tool_names,
                )
                server_stats["removed"] = removed

                # Generate embeddings for new/changed tools
                if semantic_search and tools_to_embed:
                    # Look up DB-assigned tool IDs if available, else use synthetic IDs
                    cached_tools = server._mcp_db_manager.get_cached_tools(
                        server_name, project_id=project_id
                    )
                    tool_id_map = {t.name: t.id for t in cached_tools}

                    for tool in tools_to_embed:
                        tool_name = tool["name"]
                        tool_id = tool_id_map.get(tool_name)
                        if not tool_id:
                            # Deterministic UUID for internal tools not in DB
                            import uuid

                            tool_id = str(
                                uuid.uuid5(uuid.NAMESPACE_DNS, f"{server_name}/{tool_name}")
                            )
                        try:
                            await semantic_search.embed_tool(
                                tool_id=tool_id,
                                name=tool_name,
                                description=tool.get("description"),
                                input_schema=tool.get("inputSchema"),
                                server_name=server_name,
                                project_id=project_id,
                            )
                            server_stats["embeddings"] += 1
                        except Exception as e:
                            logger.warning(f"Failed to embed {server_name}/{tool_name}: {e}")

                stats["by_server"][server_name] = server_stats
                stats["servers_processed"] += 1
                stats["tools_new"] += server_stats["new"]
                stats["tools_changed"] += server_stats["changed"]
                stats["tools_unchanged"] += server_stats["unchanged"]
                stats["tools_removed"] += server_stats["removed"]
                stats["embeddings_generated"] += server_stats["embeddings"]

            except Exception as e:
                logger.error(f"Error processing server {server_name}: {e}")
                stats["by_server"][server_name] = {"error": str(e)}

        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {
            "success": True,
            "force": force,
            "stats": stats,
            "response_time_ms": response_time_ms,
        }

    except Exception as e:
        logger.error(f"Refresh tools error: {e}", exc_info=True)
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}


__all__ = [
    "embed_mcp_tools",
    "get_mcp_status",
    "refresh_mcp_tools",
]

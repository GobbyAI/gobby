"""
Registry endpoints for MCP tool embedding, status, and refresh.

Extracted from tools.py as part of Phase 2 Strangler Fig decomposition.
These endpoints handle tool registry operations like embedding, status, and refresh.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import Depends, HTTPException, Request

from gobby.mcp_proxy.models import MCPServerConfig
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
                logger.error("Embedding generation failed: %s", e)
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
        logger.exception("Embed tools error: %s", e)
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
        logger.exception("Get MCP status error: %s", e)
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
        if not isinstance(body, dict):
            body = {}
        force = body.get("force", False)
        server_filter = body.get("server")
        server_id_filter = body.get("server_id")

        from gobby.mcp_proxy.services.server_resolution import resolve_server
        from gobby.servers.routes.mcp.endpoints.request_context import request_mcp_scope
        from gobby.storage.projects import GLOBAL_PROJECT_ID

        project_id = request_mcp_scope(request, server, body)

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

        work: list[tuple[str, MCPServerConfig | None]] = []

        if server._internal_manager and server_id_filter is None:
            for registry in server._internal_manager.get_all_registries():
                if server_filter is None or registry.name == server_filter:
                    work.append((registry.name, None))

        if server.mcp_manager:
            for config in server.mcp_manager.server_configs:
                if not getattr(config, "enabled", True):
                    continue
                name = getattr(config, "name", None)
                if not isinstance(name, str):
                    continue
                config_id = getattr(config, "id", None)
                if isinstance(config_id, str) and config_id:
                    resolved = resolve_server(
                        server.mcp_manager,
                        name,
                        server_id=str(server_id_filter) if server_id_filter else None,
                        project_id=project_id,
                    )
                    if resolved is None or resolved.id != config.id:
                        continue
                elif server_id_filter:
                    continue
                if server_filter is not None and name != server_filter:
                    continue
                work.append((name, config))

        filtered = server_filter is not None or bool(server_id_filter)
        if server.mcp_manager and filtered and not any(config is not None for _, config in work):
            # A filtered refresh may target a row synced after startup that the
            # manager has not loaded yet; refresh_server self-heals from the DB.
            from gobby.mcp_proxy.client_manager.server_registry import config_from_server

            mcp_db = server._mcp_db_manager
            row = None
            if mcp_db is not None:
                if server_id_filter:
                    row = mcp_db.get_server_by_id(str(server_id_filter))
                    if row is not None and str(row.project_id) not in (
                        project_id,
                        GLOBAL_PROJECT_ID,
                    ):
                        row = None
                elif server_filter is not None:
                    # Project row shadows global, mirroring resolve_server.
                    row = mcp_db.get_server(server_filter, project_id=project_id)
                    if row is None and project_id != GLOBAL_PROJECT_ID:
                        row = mcp_db.get_server(server_filter, project_id=GLOBAL_PROJECT_ID)
            if row is not None:
                if getattr(row, "enabled", True):
                    work.append((row.name, config_from_server(row)))
                else:
                    stats["by_server"][str(row.id)] = {
                        "error": "server_disabled",
                        "name": row.name,
                        "scope": (
                            "global" if str(row.project_id) == GLOBAL_PROJECT_ID else "project"
                        ),
                    }

        if filtered and not work and not stats["by_server"]:
            wanted = server_filter or str(server_id_filter)
            searched = (
                "global scope"
                if project_id == GLOBAL_PROJECT_ID
                else f"project {project_id} and global scope"
            )
            return {
                "success": False,
                "error": f"Unknown MCP server: '{wanted}' (searched {searched})",
                "error_code": "unknown_server",
                "response_time_ms": (time.perf_counter() - start_time) * 1000,
            }

        for server_name, instance in work:
            try:
                tools: list[dict[str, Any]] = []
                hash_project = str(instance.project_id) if instance is not None else project_id
                stats_key = (
                    instance.id
                    if instance is not None and isinstance(instance.id, str)
                    else server_name
                )
                scope_label = (
                    "global"
                    if instance is not None and str(instance.project_id) == GLOBAL_PROJECT_ID
                    else "project"
                )

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
                elif server.mcp_manager and instance is not None:
                    try:
                        refresh = getattr(server.mcp_manager, "refresh_server", None)
                        if callable(refresh):
                            refreshed = refresh(instance.id)
                            if asyncio.iscoroutine(refreshed):
                                await refreshed
                        loaded = server.mcp_manager.get_server_config(instance.id)
                        if loaded is None:
                            loaded = instance
                        cached: list[Any] = []
                        db = server._mcp_db_manager
                        if db is not None and hasattr(db, "get_cached_tools"):
                            cached = list(db.get_cached_tools(instance.id) or [])
                        if cached:
                            for cached_tool in cached:
                                tools.append(
                                    {
                                        "name": cached_tool.name,
                                        "description": getattr(cached_tool, "description", None),
                                        "inputSchema": getattr(cached_tool, "input_schema", None),
                                    }
                                )
                        elif loaded.tools:
                            for cached_tool in loaded.tools:
                                if isinstance(cached_tool, dict):
                                    tools.append(
                                        {
                                            "name": cached_tool.get("name"),
                                            "description": cached_tool.get("description")
                                            or cached_tool.get("brief"),
                                            "inputSchema": cached_tool.get("inputSchema"),
                                        }
                                    )
                        if not tools:
                            connect_id = (
                                instance.id if isinstance(instance.id, str) else server_name
                            )
                            session = await server.mcp_manager.ensure_connected(connect_id)
                            tools_result = await session.list_tools()
                            for mcp_tool in tools_result.tools:
                                tools.append(
                                    {
                                        "name": mcp_tool.name,
                                        "description": mcp_tool.description,
                                        "inputSchema": mcp_tool.input_schema,
                                    }
                                )
                    except Exception as e:
                        logger.warning("Failed to refresh %s: %s", server_name, e)
                        stats["by_server"][stats_key] = {
                            "error": str(e),
                            "name": server_name,
                            "scope": scope_label,
                        }
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
                        project_id=hash_project,
                        tools=tools,
                    )

                server_stats: dict[str, Any] = {
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
                        schema_hash = compute_schema_hash(
                            schema,
                            description=tool.get("description"),
                        )
                        schema_hash_manager.store_hash(
                            server_name=server_name,
                            tool_name=tool_name,
                            project_id=hash_project,
                            schema_hash=schema_hash,
                        )
                        tools_to_embed.append(tool)
                    else:
                        # Just update verification time for unchanged
                        schema_hash_manager.update_verification_time(
                            server_name=server_name,
                            tool_name=tool_name,
                            project_id=hash_project,
                        )

                # Clean up stale hashes
                valid_tool_names = [t["name"] for t in tools]
                removed = schema_hash_manager.cleanup_stale_hashes(
                    server_name=server_name,
                    project_id=hash_project,
                    valid_tool_names=valid_tool_names,
                )
                server_stats["removed"] = removed

                # Generate embeddings for new/changed tools
                if semantic_search and tools_to_embed:
                    # Look up DB-assigned tool IDs if available, else use synthetic IDs
                    stored = None
                    if instance is not None:
                        stored = type("Row", (), {"id": instance.id})()
                    else:
                        stored = server._mcp_db_manager.get_server(
                            server_name, project_id=hash_project
                        )
                    cached_tools = (
                        server._mcp_db_manager.get_cached_tools(stored.id) if stored else []
                    )
                    tool_id_map = {t.name: t.id for t in cached_tools}

                    for tool in tools_to_embed:
                        tool_name = tool["name"]
                        tool_id = tool_id_map.get(tool_name)
                        if not tool_id:
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
                                project_id=hash_project,
                                server_id=instance.id if instance is not None else None,
                            )
                            server_stats["embeddings"] += 1
                        except Exception as e:
                            logger.warning("Failed to embed %s/%s: %s", server_name, tool_name, e)

                server_stats["name"] = server_name
                server_stats["scope"] = scope_label
                stats["by_server"][stats_key] = server_stats
                stats["servers_processed"] += 1
                stats["tools_new"] += server_stats["new"]
                stats["tools_changed"] += server_stats["changed"]
                stats["tools_unchanged"] += server_stats["unchanged"]
                stats["tools_removed"] += server_stats["removed"]
                stats["embeddings_generated"] += server_stats["embeddings"]

            except Exception as e:
                logger.error("Error processing server %s: %s", server_name, e)
                stats["by_server"][server_name] = {"error": str(e)}

        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {
            "success": True,
            "force": force,
            "stats": stats,
            "response_time_ms": response_time_ms,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Refresh tools error: %s", e)
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}


__all__ = [
    "embed_mcp_tools",
    "get_mcp_status",
    "refresh_mcp_tools",
]

"""
Discovery endpoints for MCP tool and server listing.

Extracted from tools.py as part of Phase 2 Strangler Fig decomposition.
These endpoints handle tool discovery, search, and recommendations.
"""

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from collections.abc import Sequence as ABCSequence
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, cast

from fastapi import Depends, HTTPException, Request
from mcp.types import ListToolsResult

from gobby.mcp_proxy.client_manager.server_registry import truncate_tool_brief
from gobby.servers.routes.dependencies import get_metrics_manager, get_server

if TYPE_CHECKING:
    from gobby.mcp_proxy.metrics import ToolMetricsManager
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)
MCP_CALL_TIMEOUT = 30.0

# Set to keep background tasks alive (prevent garbage collection)
_background_tasks: set[asyncio.Task[Any]] = set()


class CachedToolDict(TypedDict, total=False):
    name: str | None
    brief: str | None
    description: str | None
    inputSchema: Mapping[str, Any]


class CachedToolObject(Protocol):
    name: str | None
    brief: str | None
    description: str | None


class CachedToolsConfig(Protocol):
    @property
    def tools(
        self,
    ) -> ABCSequence[CachedToolDict | Mapping[str, Any] | CachedToolObject] | None: ...


class ToolBrief(TypedDict):
    name: str
    brief: str


def _object_attr(value: object, attr: str) -> object | None:
    return getattr(value, attr, None)


def _response_tool_briefs(tool_briefs: list[ToolBrief]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], tool_briefs)


def _cached_tool_briefs(config: CachedToolsConfig) -> list[ToolBrief]:
    raw_tools = config.tools
    if not isinstance(raw_tools, ABCSequence):
        return []

    tools: list[ToolBrief] = []
    for tool in raw_tools:
        if isinstance(tool, Mapping):
            name = tool.get("name")
            brief = tool.get("brief") or tool.get("description") or ""
        else:
            name = _object_attr(tool, "name")
            brief = _object_attr(tool, "brief") or _object_attr(tool, "description") or ""
        if name:
            tools.append({"name": str(name), "brief": truncate_tool_brief(str(brief))})
    return tools


def _discovered_tools_from_list_tools_result(
    tools_result: ListToolsResult,
) -> list[dict[str, Any]]:
    tools_list: list[dict[str, Any]] = []
    for tool in tools_result.tools:
        tools_list.append(
            {
                "name": tool.name,
                "description": tool.description or "",
                "inputSchema": dict(tool.input_schema),
            }
        )
    return tools_list


def _mcp_call_timeout(server: "HTTPServer") -> float:
    proxy_config = getattr(getattr(server, "config", None), "mcp_client_proxy", None)
    timeout = getattr(proxy_config, "tool_timeout", None)
    if isinstance(timeout, int | float) and timeout > 0:
        return float(timeout)
    return MCP_CALL_TIMEOUT


def _has_manager_method(mcp_manager: Any, method_name: str) -> bool:
    return callable(getattr(type(mcp_manager), method_name, None))


def _get_external_server_config(mcp_manager: Any, server_name: str) -> Any | None:
    if _has_manager_method(mcp_manager, "get_server_config"):
        return mcp_manager.get_server_config(server_name)

    configs = getattr(mcp_manager, "_configs", None)
    if isinstance(configs, Mapping):
        return configs.get(server_name)
    return None


async def _disconnect_external_server(mcp_manager: Any, server_name: str) -> None:
    if _has_manager_method(mcp_manager, "disconnect_server"):
        try:
            result = mcp_manager.disconnect_server(server_name)
            if asyncio.iscoroutine(result):
                await asyncio.wait_for(result, timeout=5.0)
            return
        except Exception:
            logger.warning(
                "Failed to disconnect MCP server %s after discovery timeout",
                server_name,
                exc_info=True,
            )
            return

    connection = None
    connections = getattr(mcp_manager, "connections", None)
    if isinstance(connections, dict):
        connection = connections.pop(server_name, None)
    if connection is None:
        get_client = getattr(mcp_manager, "get_client", None)
        if callable(get_client):
            try:
                connection = get_client(server_name)
            except Exception:
                connection = None

    disconnect = getattr(connection, "disconnect", None)
    if not callable(disconnect):
        return

    try:
        result = disconnect()
        if asyncio.iscoroutine(result):
            await asyncio.wait_for(result, timeout=5.0)
    except Exception:
        logger.warning(
            "Failed to disconnect MCP server %s after discovery timeout",
            server_name,
            exc_info=True,
        )


async def _list_external_server_tools(
    mcp_manager: Any,
    server_name: str,
    *,
    timeout: float,
) -> list[dict[str, Any]]:
    config = _get_external_server_config(mcp_manager, server_name)
    connect_timeout = getattr(config, "connect_timeout", None)
    if isinstance(connect_timeout, int | float) and connect_timeout > 0:
        total_timeout = connect_timeout + timeout
    else:
        connect_timeout = timeout
        total_timeout = timeout
    try:
        start = time.monotonic()
        session = await asyncio.wait_for(
            mcp_manager.ensure_connected(server_name),
            timeout=connect_timeout,
        )
        # A cold connection may consume its entire configured budget. Preserve
        # the listing remainder without restarting the overall deadline.
        remaining = total_timeout - (time.monotonic() - start)
        if remaining <= 0:
            raise TimeoutError
        tools_result = await asyncio.wait_for(session.list_tools(), timeout=remaining)
        discovered_tools = _discovered_tools_from_list_tools_result(tools_result)
        await asyncio.to_thread(
            mcp_manager.cache_discovered_tools,
            server_name,
            discovered_tools,
        )
        tool_briefs: list[ToolBrief] = [
            {
                "name": tool["name"],
                "brief": truncate_tool_brief(tool.get("description")),
            }
            for tool in discovered_tools
        ]
        return _response_tool_briefs(tool_briefs)
    except TimeoutError:
        logger.warning(
            "Timed out listing tools from MCP server %s after %.1fs",
            server_name,
            total_timeout,
        )
        await _disconnect_external_server(mcp_manager, server_name)
        return []
    except Exception as e:
        logger.warning(
            "Failed to list tools from %s (%s): %r",
            server_name,
            type(e).__name__,
            e,
        )
        return []


async def list_all_mcp_tools(
    request: Request,
    server_filter: str | None = None,
    include_metrics: bool = False,
    project_id: str | None = None,
    server: "HTTPServer" = Depends(get_server),
    metrics_manager: "ToolMetricsManager | None" = Depends(get_metrics_manager),
) -> dict[str, Any]:
    """
    List tools from MCP servers.

    Args:
        server_filter: Optional server name to filter by
        include_metrics: When True, include call_count, success_rate, avg_latency for each tool
        project_id: Project ID for metrics lookup (uses current project if not specified)
        server: HTTPServer instance (injected)
        metrics_manager: Tool metrics manager (injected)

    Returns:
        Dict of server names to tool lists
    """

    try:
        from gobby.mcp_proxy.services.server_resolution import resolve_server
        from gobby.servers.routes.mcp.endpoints.request_context import request_mcp_scope
        from gobby.storage.projects import GLOBAL_PROJECT_ID

        scope = request_mcp_scope(request, server, {})
        tools_by_server: dict[str, list[dict[str, Any]]] = {}

        # Resolve project_id for metrics lookup
        resolved_project_id = None
        if include_metrics:
            try:
                resolved_project_id = server.resolve_project_id(project_id, cwd=None)
            except ValueError:
                # Project not initialized; skip metrics enrichment
                resolved_project_id = None

        # If specific server requested
        if server_filter:
            # Check internal first
            if server._internal_manager and server._internal_manager.is_internal(server_filter):
                registry = server._internal_manager.get_registry(server_filter)
                if registry:
                    tools_by_server[server_filter] = registry.list_tools()
            elif server.mcp_manager:
                resolved = resolve_server(server.mcp_manager, server_filter, project_id=scope)
                if resolved is not None:
                    if not resolved.enabled:
                        tools_by_server[resolved.name] = []
                    else:
                        tools_by_server[resolved.name] = await _list_external_server_tools(
                            server.mcp_manager,
                            resolved.id,
                            timeout=_mcp_call_timeout(server),
                        )
        elif not server_filter:
            # Get tools from all servers
            # Internal servers
            if server._internal_manager:
                for registry in server._internal_manager.get_all_registries():
                    tools_by_server[registry.name] = registry.list_tools()

            # Passive external inventory is cache-only. Live discovery is reserved
            # for filtered requests and explicit refresh operations.
            if server.mcp_manager:
                enabled_configs = [c for c in server.mcp_manager.server_configs if c.enabled]
                # Global rows first so a same-named project row shadows them.
                for wanted_scope in (GLOBAL_PROJECT_ID, scope):
                    for config in enabled_configs:
                        config_scope = str(getattr(config, "project_id", None) or GLOBAL_PROJECT_ID)
                        if config_scope == wanted_scope:
                            tools_by_server[config.name] = _response_tool_briefs(
                                _cached_tool_briefs(config)
                            )

        # Enrich with metrics if requested
        if include_metrics and metrics_manager and resolved_project_id:
            # Get all metrics for this project
            metrics_data = metrics_manager.get_metrics(project_id=resolved_project_id)
            metrics_by_key = {
                (m["server_name"], m["tool_name"]): m for m in metrics_data.get("tools", [])
            }

            for server_name, tools_list in tools_by_server.items():
                for tool in tools_list:
                    # Guard against non-dict or missing-name entries
                    if not isinstance(tool, dict) or "name" not in tool:
                        continue
                    tool_name = tool.get("name")
                    key = (server_name, tool_name)
                    if key in metrics_by_key:
                        m = metrics_by_key[key]
                        tool["call_count"] = m.get("call_count", 0)
                        tool["success_rate"] = m.get("success_rate")
                        tool["avg_latency_ms"] = m.get("avg_latency_ms")
                    else:
                        tool["call_count"] = 0
                        tool["success_rate"] = None
                        tool["avg_latency_ms"] = None

        return {
            "success": True,
            "tools": tools_by_server,
        }

    except Exception as e:
        logger.exception("List MCP tools error: %s", e)
        return {"success": False, "error": str(e)}


async def recommend_mcp_tools(
    request: Request,
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """
    Get AI-powered tool recommendations for a task.

    Request body:
        {
            "task_description": "I need to query a database",
            "agent_id": "optional-agent-id",
            "search_mode": "llm" | "semantic" | "hybrid",
            "top_k": 10,
            "min_similarity": 0.3,
            "cwd": "/path/to/project"
        }

    Returns:
        List of tool recommendations
    """

    try:
        try:
            body = await request.json()
        except json.JSONDecodeError as err:
            return {
                "success": False,
                "error": "Malformed JSON",
                "message": str(err),
            }

        task_description = body.get("task_description")
        agent_id = body.get("agent_id")
        search_mode = body.get("search_mode", "llm")
        top_k = body.get("top_k", 10)
        min_similarity = body.get("min_similarity", 0.3)
        cwd = body.get("cwd")

        if not task_description:
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": "Required field: task_description"},
            )

        # Every mode is project-scoped (LLM mode lists the project's servers),
        # so resolve project_id from cwd.
        try:
            project_id = server.resolve_project_id(None, cwd)
        except ValueError as e:
            return {
                "success": False,
                "error": str(e),
                "task": task_description,
            }

        # Use tools handler if available
        if server._tools_handler:
            result = await server._tools_handler.recommend_tools(
                task_description=task_description,
                agent_id=agent_id,
                search_mode=search_mode,
                top_k=top_k,
                min_similarity=min_similarity,
                project_id=project_id,
            )
            return result

        # Fallback: no tools handler
        return {
            "success": False,
            "error": "Tools handler not initialized",
            "recommendations": [],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Recommend tools error: %s", e)
        return {"success": False, "error": str(e)}


async def search_mcp_tools(
    request: Request,
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """
    Search for tools using semantic similarity.

    Request body:
        {
            "query": "create a file",
            "top_k": 10,
            "min_similarity": 0.0,
            "server": "optional-server-filter",
            "cwd": "/path/to/project"
        }

    Returns:
        List of matching tools with similarity scores
    """

    try:
        try:
            body = await request.json()
        except json.JSONDecodeError as err:
            return {
                "success": False,
                "error": "Malformed JSON",
                "message": str(err),
            }

        query = body.get("query")
        top_k = body.get("top_k", 10)
        min_similarity = body.get("min_similarity", 0.0)
        server_filter = body.get("server")
        cwd = body.get("cwd")

        if not query:
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": "Required field: query"},
            )

        # Resolve project_id from cwd
        try:
            project_id = server.resolve_project_id(None, cwd)
        except ValueError as e:
            return {
                "success": False,
                "error": str(e),
                "query": query,
            }

        # Use semantic search directly if available
        if server._tools_handler and server._tools_handler._semantic_search:
            try:
                import asyncio

                semantic_search = server._tools_handler._semantic_search

                # Check if embeddings exist - if not, trigger background generation
                has_existing = await semantic_search.has_embeddings(project_id)
                if not has_existing and server._mcp_db_manager:
                    logger.info(
                        "No embeddings for project %s, triggering background generation...",
                        project_id,
                    )

                    # Wrapper to log exceptions from background embedding generation
                    async def _embed_with_error_handling(proj_id: str) -> None:
                        try:
                            await semantic_search.embed_all_tools(
                                project_id=proj_id,
                                mcp_manager=server._mcp_db_manager,
                                internal_manager=server._internal_manager,
                            )
                        except Exception as e:
                            logger.exception(
                                "Background embedding generation failed for project %s: %s",
                                proj_id,
                                e,
                            )

                    # Trigger embedding generation as background task (non-blocking)
                    task = asyncio.create_task(_embed_with_error_handling(project_id))
                    _background_tasks.add(task)
                    task.add_done_callback(_background_tasks.discard)
                    # Return early indicating embeddings are being generated
                    return {
                        "success": True,
                        "embeddings_generating": True,
                        "query": query,
                        "results": [],
                        "total_results": 0,
                        "message": "Embeddings are being generated. Please retry in a few seconds.",
                    }

                results = await semantic_search.search_tools(
                    query=query,
                    project_id=project_id,
                    top_k=top_k,
                    min_similarity=min_similarity,
                    server_filter=server_filter,
                )
                visible_results = [result.to_dict() for result in results]
                return {
                    "success": True,
                    "query": query,
                    "results": visible_results,
                    "total_results": len(visible_results),
                }
            except Exception as e:
                logger.error("Semantic search failed: %s", e)
                return {
                    "success": False,
                    "error": str(e),
                    "query": query,
                }

        # Fallback: no semantic search
        return {
            "success": False,
            "error": "Semantic search not configured",
            "results": [],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Search tools error: %s", e)
        return {"success": False, "error": str(e)}


__all__ = [
    "list_all_mcp_tools",
    "recommend_mcp_tools",
    "search_mcp_tools",
]

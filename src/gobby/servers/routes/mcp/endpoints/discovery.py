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

from gobby.mcp_proxy.models import HealthState, MCPConnectionHealth
from gobby.servers.routes.dependencies import get_metrics_manager, get_server

if TYPE_CHECKING:
    from gobby.mcp_proxy.metrics import ToolMetricsManager
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)
MCP_CALL_TIMEOUT = 30.0

# Set to keep background tasks alive (prevent garbage collection)
_background_tasks: set[asyncio.Task[Any]] = set()


class HealthAwareMCPManager(Protocol):
    @property
    def health(self) -> Mapping[str, MCPConnectionHealth]: ...


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


def _log_empty_unhealthy_cache(server_name: str) -> None:
    logger.warning(
        "MCP server %s is unhealthy and has no cached tool list; returning no tools",
        server_name,
    )


def _object_attr(value: object, attr: str) -> object | None:
    return getattr(value, attr, None)


def _truncate_tool_brief(text: str | None, *, max_chars: int = 100) -> str:
    if not text:
        return ""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    return f"{text[: max_chars - 1]}…"


def _external_server_is_unhealthy(mcp_manager: HealthAwareMCPManager, server_name: str) -> bool:
    health = mcp_manager.health.get(server_name)
    return health is not None and health.health == HealthState.UNHEALTHY


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
            tools.append({"name": str(name), "brief": _truncate_tool_brief(str(brief))})
    return tools


def _tool_briefs_from_list_tools_result(tools_result: ListToolsResult) -> list[ToolBrief]:
    tools_list: list[ToolBrief] = []
    for tool in tools_result.tools:
        raw_description = _object_attr(tool, "description")
        desc = str(raw_description) if raw_description else ""
        tools_list.append(
            {
                "name": tool.name,
                "brief": _truncate_tool_brief(desc),
            }
        )
    return tools_list


def _mcp_call_timeout(server: "HTTPServer") -> float:
    proxy_config = getattr(getattr(server, "config", None), "mcp_client_proxy", None)
    timeout = getattr(proxy_config, "tool_timeout", None)
    if isinstance(timeout, int | float) and timeout > 0:
        return float(timeout)
    return MCP_CALL_TIMEOUT


async def _disconnect_external_server(mcp_manager: Any, server_name: str) -> None:
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
    try:
        start = time.monotonic()
        session = await asyncio.wait_for(
            mcp_manager.ensure_connected(server_name),
            timeout=timeout,
        )
        # Budget remaining time after connection to ensure total time <= timeout
        remaining = timeout - (time.monotonic() - start)
        if remaining <= 0:
            raise TimeoutError
        tools_result = await asyncio.wait_for(session.list_tools(), timeout=remaining)
        return _response_tool_briefs(_tool_briefs_from_list_tools_result(tools_result))
    except TimeoutError:
        logger.warning(
            "Timed out listing tools from MCP server %s after %.1fs",
            server_name,
            timeout,
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
    start_time = time.perf_counter()

    try:
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
            elif server.mcp_manager and server.mcp_manager.has_server(server_filter):
                # Check if server is enabled before attempting connection
                server_config = server.mcp_manager._configs.get(server_filter)
                if server_config and not server_config.enabled:
                    tools_by_server[server_filter] = []
                else:
                    if server_config and _external_server_is_unhealthy(
                        server.mcp_manager, server_filter
                    ):
                        cached_tools = _cached_tool_briefs(server_config)
                        if not cached_tools:
                            _log_empty_unhealthy_cache(server_filter)
                        tools_by_server[server_filter] = _response_tool_briefs(cached_tools)
                    else:
                        tools_by_server[server_filter] = await _list_external_server_tools(
                            server.mcp_manager,
                            server_filter,
                            timeout=_mcp_call_timeout(server),
                        )
        else:
            # Get tools from all servers
            # Internal servers
            if server._internal_manager:
                for registry in server._internal_manager.get_all_registries():
                    tools_by_server[registry.name] = registry.list_tools()

            # External MCP servers use cached tools when unhealthy; otherwise
            # ensure_connected provides lazy loading.
            if server.mcp_manager:
                for config in server.mcp_manager.server_configs:
                    if config.enabled:
                        if _external_server_is_unhealthy(server.mcp_manager, config.name):
                            cached_tools = _cached_tool_briefs(config)
                            if not cached_tools:
                                _log_empty_unhealthy_cache(config.name)
                            tools_by_server[config.name] = _response_tool_briefs(cached_tools)
                            continue

                        tools_by_server[config.name] = await _list_external_server_tools(
                            server.mcp_manager,
                            config.name,
                            timeout=_mcp_call_timeout(server),
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

        response_time_ms = (time.perf_counter() - start_time) * 1000

        return {
            "success": True,
            "tools": tools_by_server,
            "response_time_ms": response_time_ms,
        }

    except Exception as e:
        logger.error(f"List MCP tools error: {e}", exc_info=True)
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}


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
    start_time = time.perf_counter()

    try:
        try:
            body = await request.json()
        except json.JSONDecodeError as err:
            response_time_ms = (time.perf_counter() - start_time) * 1000
            return {
                "success": False,
                "error": "Malformed JSON",
                "message": str(err),
                "response_time_ms": response_time_ms,
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

        # For semantic/hybrid modes, resolve project_id from cwd
        project_id = None
        if search_mode in ("semantic", "hybrid"):
            try:
                project_id = server.resolve_project_id(None, cwd)
            except ValueError as e:
                response_time_ms = (time.perf_counter() - start_time) * 1000
                return {
                    "success": False,
                    "error": str(e),
                    "task": task_description,
                    "response_time_ms": response_time_ms,
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
            response_time_ms = (time.perf_counter() - start_time) * 1000
            result["response_time_ms"] = response_time_ms
            return result

        # Fallback: no tools handler
        return {
            "success": False,
            "error": "Tools handler not initialized",
            "recommendations": [],
            "response_time_ms": (time.perf_counter() - start_time) * 1000,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Recommend tools error: {e}", exc_info=True)
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}


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
    start_time = time.perf_counter()

    try:
        try:
            body = await request.json()
        except json.JSONDecodeError as err:
            response_time_ms = (time.perf_counter() - start_time) * 1000
            return {
                "success": False,
                "error": "Malformed JSON",
                "message": str(err),
                "response_time_ms": response_time_ms,
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
            response_time_ms = (time.perf_counter() - start_time) * 1000
            return {
                "success": False,
                "error": str(e),
                "query": query,
                "response_time_ms": response_time_ms,
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
                        f"No embeddings for project {project_id}, triggering background generation..."
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
                            logger.error(
                                f"Background embedding generation failed for project {proj_id}: {e}",
                                exc_info=True,
                            )

                    # Trigger embedding generation as background task (non-blocking)
                    task = asyncio.create_task(_embed_with_error_handling(project_id))
                    _background_tasks.add(task)
                    task.add_done_callback(_background_tasks.discard)
                    # Return early indicating embeddings are being generated
                    response_time_ms = (time.perf_counter() - start_time) * 1000
                    return {
                        "success": True,
                        "embeddings_generating": True,
                        "query": query,
                        "results": [],
                        "total_results": 0,
                        "message": "Embeddings are being generated. Please retry in a few seconds.",
                        "response_time_ms": response_time_ms,
                    }

                results = await semantic_search.search_tools(
                    query=query,
                    project_id=project_id,
                    top_k=top_k,
                    min_similarity=min_similarity,
                    server_filter=server_filter,
                )
                response_time_ms = (time.perf_counter() - start_time) * 1000
                return {
                    "success": True,
                    "query": query,
                    "results": [r.to_dict() for r in results],
                    "total_results": len(results),
                    "response_time_ms": response_time_ms,
                }
            except Exception as e:
                logger.error(f"Semantic search failed: {e}")
                response_time_ms = (time.perf_counter() - start_time) * 1000
                return {
                    "success": False,
                    "error": str(e),
                    "query": query,
                    "response_time_ms": response_time_ms,
                }

        # Fallback: no semantic search
        return {
            "success": False,
            "error": "Semantic search not configured",
            "results": [],
            "response_time_ms": (time.perf_counter() - start_time) * 1000,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Search tools error: {e}", exc_info=True)
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}


__all__ = [
    "list_all_mcp_tools",
    "recommend_mcp_tools",
    "search_mcp_tools",
]

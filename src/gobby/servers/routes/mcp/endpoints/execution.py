"""
Execution endpoints for MCP tool invocation.

Extracted from tools.py as part of Phase 2 Strangler Fig decomposition.
These endpoints handle tool listing, schema retrieval, and tool execution.
"""

import json
import logging
import time
from typing import TYPE_CHECKING, Any, cast

from fastapi import Depends, HTTPException, Request

from gobby.mcp_proxy.models import MCPError
from gobby.mcp_proxy.services.schema_guidance import record_schema_shown
from gobby.mcp_proxy.services.server_resolution import resolve_server
from gobby.mcp_proxy.tools.internal import normalize_internal_success_result
from gobby.mcp_proxy.wait_tools import (
    MCP_WRAPPER_PROTOCOL_VERSION_HEADER,
    mcp_wrapper_protocol_mismatch_result,
)
from gobby.servers.routes.dependencies import get_internal_manager, get_mcp_manager, get_server
from gobby.servers.routes.mcp.endpoints import request_context
from gobby.servers.routes.mcp.endpoints.discovery import _mcp_call_timeout
from gobby.servers.routes.mcp.endpoints.request_context import request_mcp_scope
from gobby.telemetry.instruments import inc_counter, observe_histogram
from gobby.utils.datetime import to_json_safe
from gobby.utils.project_context import set_project_context
from gobby.utils.session_context import get_current_session_id

if TYPE_CHECKING:
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.mcp_proxy.tools.internal import InternalRegistryManager
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

_PROXY_NAMESPACE = "gobby"
_MCP_TOOL_PREFIX = "mcp__"


def _normalize_schema_ref(server_name: str, tool_name: str) -> tuple[str, str]:
    """Map wrapper names such as mcp__gobby__call_tool onto the proxy namespace."""
    raw_server = server_name.strip()
    raw_tool = tool_name.strip()
    if raw_tool.startswith(_MCP_TOOL_PREFIX):
        rest = raw_tool[len(_MCP_TOOL_PREFIX) :]
        if "__" in rest:
            parsed_server, parsed_tool = rest.split("__", 1)
            if parsed_server and parsed_tool:
                if not raw_server or raw_server in {"?", _PROXY_NAMESPACE}:
                    raw_server = parsed_server
                raw_tool = parsed_tool
    return raw_server, raw_tool


def _json_safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], to_json_safe(payload))


def _success_response_payload(result: Any, response_time_ms: float) -> dict[str, Any]:
    """Build the MCP success wire payload.

    Tool dicts that already report success=True are flattened with
    response_time_ms added. Other results are wrapped under result while adding
    success=True and response_time_ms.
    """
    if isinstance(result, dict) and result.get("success") is True:
        return _json_safe_payload({**result, "response_time_ms": response_time_ms})
    return _json_safe_payload(
        {
            "success": True,
            "result": result,
            "response_time_ms": response_time_ms,
        }
    )


def _http_request_scope(request: Request, server: "HTTPServer", ctx_token: Any, body: Any) -> str:
    """Resolve HTTP scope and seed an explicit body project for sessionless calls."""
    payload = body if isinstance(body, dict) else {}
    scope_project = request_mcp_scope(
        request,
        server,
        payload,
        session_project_id=getattr(ctx_token, "resolved_project_id", None),
    )
    if (
        getattr(ctx_token, "resolved_project_id", None) is None
        and isinstance(payload.get("project_id"), str)
        and payload["project_id"].strip()
    ):
        ctx_token.resolved_project_id = scope_project
        ctx_token.project_token = set_project_context({"id": scope_project})
    return scope_project


def _timeout_response_payload(timeout: float, response_time_ms: float) -> dict[str, Any]:
    return {
        "success": False,
        "error": f"Tool call timed out after {timeout:g} seconds",
        "response_time_ms": response_time_ms,
    }


def _incompatible_stdio_wrapper_wait_result(
    request: Request,
    tool_name: str,
    *,
    require_stdio_proxy: bool,
) -> dict[str, Any] | None:
    provided_protocol_version = request.headers.get(MCP_WRAPPER_PROTOCOL_VERSION_HEADER)
    if provided_protocol_version is None and not require_stdio_proxy:
        return None
    return mcp_wrapper_protocol_mismatch_result(
        tool_name,
        provided_protocol_version,
    )


def _process_tool_proxy_result(
    result: Any,
    server_name: str,
    tool_name: str,
    response_time_ms: float,
) -> dict[str, Any]:
    """
    Process tool proxy result with consistent metrics, logging, and error handling.

    Tool-level errors (including server-not-found) are returned as HTTP 200 with
    ``success=False`` in the response body rather than raising HTTPException.
    This keeps MCP/HTTP clients on a consistent application-level error contract
    without nesting a second ``success=False`` payload under ``result``.

    Args:
        result: The result from tool_proxy.call_tool()
        server_name: Name of the MCP server
        tool_name: Name of the tool called
        response_time_ms: Response time in milliseconds

    Returns:
        Result dict with success status and response time
    """
    # Track metrics for tool-level failures vs successes
    if isinstance(result, dict) and result.get("success") is False:
        inc_counter("mcp_tool_calls_failed_total")

        # Return tool-level failures as a flat error payload with success=False.
        # This preserves the HTTP 200/application-error contract without forcing
        # clients to unwrap a nested {"success": false, "result": {...}} shape.
        error_code = result.get("error_code")
        if error_code:
            logger.debug(
                "MCP tool call failed: %s.%s (error_code=%s)",
                server_name,
                tool_name,
                error_code,
                extra={
                    "server": server_name,
                    "tool": tool_name,
                    "error_code": error_code,
                },
            )

        return _json_safe_payload({**result, "response_time_ms": response_time_ms})
    else:
        inc_counter("mcp_tool_calls_succeeded_total")
        logger.debug(
            "MCP tool call successful: %s.%s",
            server_name,
            tool_name,
            extra={
                "server": server_name,
                "tool": tool_name,
                "response_time_ms": response_time_ms,
            },
        )

    # Return 200 with wrapped result for success cases
    return _success_response_payload(result, response_time_ms)


async def _call_internal_tool(
    registry: Any,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any] | None,
    start_time: float,
) -> dict[str, Any]:
    """Shared helper for calling internal registry tools.

    Args:
        registry: The internal tool registry
        server_name: Name of the MCP server
        tool_name: Name of the tool to call
        arguments: Arguments to pass to the tool
        start_time: Request start time for response_time_ms calculation

    Returns:
        Tool execution result dict

    Raises:
        HTTPException: 404 if tool not found, 500 on execution error
    """
    # Check if tool exists before calling - return helpful 404 if not
    if not registry.get_schema(tool_name):
        available = [t["name"] for t in registry.list_tools()]
        raise HTTPException(
            status_code=404,
            detail={
                "success": False,
                "error": f"Tool '{tool_name}' not found on '{server_name}'. "
                f"Available: {', '.join(available)}. "
                f"Use list_tools(server_name='{server_name}') to see all tools, "
                f"or get_tool_schema(server_name='{server_name}', tool_name='...') for full schema.",
            },
        )
    try:
        result = normalize_internal_success_result(await registry.call(tool_name, arguments or {}))
        response_time_ms = (time.perf_counter() - start_time) * 1000
        inc_counter("mcp_tool_calls_succeeded_total")
        return _success_response_payload(result, response_time_ms)
    except Exception as e:
        inc_counter("mcp_tool_calls_failed_total")
        logger.exception(
            "Internal MCP tool call error: %s.%s",
            server_name,
            tool_name,
            extra={"server": server_name, "tool": tool_name},
        )
        raise HTTPException(status_code=500, detail="Internal server error") from e


async def list_mcp_tools(
    server_name: str,
    request: Request,
    server: "HTTPServer" = Depends(get_server),
    internal_manager: "InternalRegistryManager | None" = Depends(get_internal_manager),
    mcp_manager: "MCPClientManager | None" = Depends(get_mcp_manager),
) -> dict[str, Any]:
    """
    List available tools from an MCP server.

    Args:
        server_name: Name of the MCP server (e.g., "supabase", "context7")
        internal_manager: Internal tool registry manager (injected)
        mcp_manager: External MCP client manager (injected)

    Returns:
        List of available tools with their descriptions
    """
    start_time = time.perf_counter()
    ctx_token = await request_context._set_context_for_request(server, {}, request)

    try:
        # Check internal registries first (gobby-tasks, gobby-memory, etc.)
        if internal_manager and internal_manager.is_internal(server_name):
            registry = internal_manager.get_registry(server_name)
            if registry:
                tools = registry.list_tools()
                response_time_ms = (time.perf_counter() - start_time) * 1000
                observe_histogram("list_mcp_tools", response_time_ms / 1000)
                if server.tool_proxy:
                    server.tool_proxy.record_listed_server(
                        server_name,
                        session_id=ctx_token.resolved_session_id,
                    )
                result = {
                    "success": True,
                    "tools": tools,
                    "tool_count": len(tools),
                    "response_time_ms": response_time_ms,
                }
                return result
            response_time_ms = (time.perf_counter() - start_time) * 1000
            result = {
                "success": False,
                "error": f"Internal server '{server_name}' not found",
                "response_time_ms": response_time_ms,
            }
            return result

        if mcp_manager is None:
            response_time_ms = (time.perf_counter() - start_time) * 1000
            result = {
                "success": False,
                "error": "MCP manager not available",
                "response_time_ms": response_time_ms,
            }
            return result

        # Resolve the caller-visible (name, scope) to a config; the manager is
        # id-keyed and never sees names (services/server_resolution.py).
        scope_project = _http_request_scope(request, server, ctx_token, {})
        resolved = resolve_server(mcp_manager, server_name, project_id=scope_project)
        if resolved is None:
            raise HTTPException(
                status_code=404,
                detail={"success": False, "error": f"Unknown MCP server: '{server_name}'"},
            )
        server_name = resolved.name

        # Use ensure_connected for lazy loading - connects on-demand if not connected
        try:
            session = await mcp_manager.ensure_connected(resolved.id)
        except KeyError as e:
            response_time_ms = (time.perf_counter() - start_time) * 1000
            result = {"success": False, "error": str(e), "response_time_ms": response_time_ms}
            return result
        except Exception as e:
            response_time_ms = (time.perf_counter() - start_time) * 1000
            result = {
                "success": False,
                "error": f"MCP server '{server_name}' connection failed: {e}",
                "response_time_ms": response_time_ms,
            }
            return result

        # List tools using MCP SDK
        try:
            tools_result = await session.list_tools()
            tools = []
            for tool in tools_result.tools:
                tool_dict: dict[str, Any] = {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.input_schema,
                }
                tools.append(tool_dict)

            response_time_ms = (time.perf_counter() - start_time) * 1000

            logger.debug(
                "Listed %s tools from %s",
                len(tools),
                server_name,
                extra={
                    "server": server_name,
                    "tool_count": len(tools),
                    "response_time_ms": response_time_ms,
                },
            )
            if server.tool_proxy:
                server.tool_proxy.record_listed_server(
                    server_name,
                    session_id=ctx_token.resolved_session_id,
                )

            result = {
                "success": True,
                "tools": tools,
                "tool_count": len(tools),
                "response_time_ms": response_time_ms,
            }
            return result

        except Exception as e:
            logger.exception(
                "Failed to list tools from %s: %s",
                server_name,
                e,
                extra={"server": server_name},
            )
            response_time_ms = (time.perf_counter() - start_time) * 1000
            result = {
                "success": False,
                "error": f"Failed to list tools: {e}",
                "response_time_ms": response_time_ms,
            }
            return result

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("MCP list tools error: %s", server_name)
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}
    finally:
        request_context._reset_context(ctx_token)


def _record_schema_lease(
    server: "HTTPServer",
    body: dict[str, Any],
    server_name: str,
    tool_name: str,
) -> None:
    """Grant the schema lease for a successfully served tool schema.

    The daemon must not depend on the CLI's PostToolUse hook echoing this call
    back — a dropped hook channel would otherwise deadlock the
    progressive-discovery gates (#19891). Best-effort: never fails the response.
    """
    if not server.tool_proxy:
        return
    session_id = body.get("session_id") or get_current_session_id()
    if not session_id:
        return
    try:
        record_schema_shown(
            server.tool_proxy,
            session_id,
            server_name=server_name,
            tool_name=tool_name,
        )
    except Exception as exc:
        logger.debug("Failed to record schema lease for %s:%s: %s", server_name, tool_name, exc)


async def get_tool_schema(
    request: Request,
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """
    Get full schema for a specific tool.

    Request body:
        {
            "server_name": "supabase",
            "tool_name": "list_tables"
        }

    Returns:
        Tool schema with inputSchema
    """
    start_time = time.perf_counter()

    try:
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": f"Invalid JSON: {exc.msg}"},
            ) from exc
        server_name = body.get("server_name")
        tool_name = body.get("tool_name")

        server_id = body.get("server_id")
        if (not server_name and not server_id) or not tool_name:
            raise HTTPException(
                status_code=400,
                detail={
                    "success": False,
                    "error": "Required fields: server_name or server_id, and tool_name",
                },
            )
        if server_name:
            server_name, tool_name = _normalize_schema_ref(str(server_name), str(tool_name))

        ctx_token = await request_context._set_context_for_request(server, body, request)

        try:
            # Check internal first
            if server._internal_manager and server._internal_manager.is_internal(server_name):
                registry = server._internal_manager.get_registry(server_name)
                if registry:
                    schema = registry.get_schema(tool_name)
                    if schema:
                        response_time_ms = (time.perf_counter() - start_time) * 1000
                        # Build response with description only if present
                        result: dict[str, Any] = {
                            "success": True,
                            "name": schema.get("name", tool_name),
                            "inputSchema": schema.get("inputSchema"),
                            "server": server_name,
                            "response_time_ms": response_time_ms,
                        }
                        if schema.get("description"):
                            result["description"] = schema["description"]
                        _record_schema_lease(server, body, server_name, tool_name)
                        return result
                    raise HTTPException(
                        status_code=404,
                        detail={
                            "success": False,
                            "error": f"Tool '{tool_name}' not found on server '{server_name}'",
                        },
                    )

            if server.mcp_manager is None:
                raise HTTPException(
                    status_code=503,
                    detail={"success": False, "error": "MCP manager not available"},
                )

            scope_project = _http_request_scope(request, server, ctx_token, body)
            resolved = (
                resolve_server(
                    server.mcp_manager,
                    server_name,
                    server_id=str(server_id) if isinstance(server_id, str) else None,
                    project_id=scope_project,
                )
                if server.mcp_manager is not None
                else None
            )
            if resolved is not None:
                server_name = resolved.name
            elif resolved is None and server_name != _PROXY_NAMESPACE:
                response_time_ms = (time.perf_counter() - start_time) * 1000
                return {
                    "success": False,
                    "error": f"Unknown MCP server: '{server_id or server_name}'",
                    "response_time_ms": response_time_ms,
                }
            if server.tool_proxy is not None:
                proxied = await server.tool_proxy.get_tool_schema(
                    server_name, tool_name, project_id=scope_project
                )
                response_time_ms = (time.perf_counter() - start_time) * 1000
                if not proxied.get("success"):
                    return {
                        "success": False,
                        "error": proxied.get("error") or f"Unknown MCP server: '{server_name}'",
                        "response_time_ms": response_time_ms,
                    }
                tool_schema = proxied.get("tool")
                schema = tool_schema if isinstance(tool_schema, dict) else {}
                result = {
                    "success": True,
                    "name": schema.get("name", tool_name),
                    "inputSchema": schema.get("inputSchema"),
                    "server": server_name,
                    "response_time_ms": response_time_ms,
                }
                description = schema.get("description")
                if description:
                    result["description"] = description
                _record_schema_lease(server, body, server_name, tool_name)
                return result

            # Get from external MCP server
            try:
                tool_info = await server.mcp_manager.get_tool_info(server_name, tool_name)
                response_time_ms = (time.perf_counter() - start_time) * 1000

                # Build response with description only if present
                response: dict[str, Any] = {
                    "success": True,
                    "name": tool_info.get("name", tool_name),
                    "inputSchema": tool_info.get("inputSchema"),
                    "server": server_name,
                    "response_time_ms": response_time_ms,
                }
                if tool_info.get("description"):
                    response["description"] = tool_info["description"]
                _record_schema_lease(server, body, server_name, tool_name)
                return response

            except (KeyError, ValueError, MCPError) as e:
                # Tool or server not found
                response_time_ms = (time.perf_counter() - start_time) * 1000
                response = {"success": False, "error": str(e), "response_time_ms": response_time_ms}
                return response
            except Exception as e:
                # Connection, timeout, or internal errors
                logger.exception(
                    "Failed to get tool schema %s/%s: %s",
                    server_name,
                    tool_name,
                    e,
                )
                response_time_ms = (time.perf_counter() - start_time) * 1000
                response = {
                    "success": False,
                    "error": f"Failed to get tool schema: {e}",
                    "response_time_ms": response_time_ms,
                }
                return response
        finally:
            request_context._reset_context(ctx_token)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Get tool schema error: %s", e)
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}


async def call_mcp_tool(
    request: Request,
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """
    Call an MCP tool.

    Request body:
        {
            "server_name": "supabase",
            "tool_name": "list_tables",
            "arguments": {}
        }

    Returns:
        Tool execution result
    """
    start_time = time.perf_counter()
    inc_counter("mcp_tool_calls_total")

    try:
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": f"Invalid JSON: {exc.msg}"},
            ) from exc
        server_name = body.get("server_name")
        server_id = body.get("server_id")
        tool_name = body.get("tool_name")
        arguments = body.get("arguments", {})
        raw_intent = body.get("intent")
        intent = raw_intent if isinstance(raw_intent, str) and raw_intent else None
        offload = body.get("offload", True)
        if not isinstance(offload, bool):
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": "offload must be a boolean"},
            )

        if (not server_name and not server_id) or not tool_name:
            raise HTTPException(
                status_code=400,
                detail={
                    "success": False,
                    "error": "Required fields: server_name or server_id, and tool_name",
                },
            )

        incompatible_wrapper_result = _incompatible_stdio_wrapper_wait_result(
            request,
            tool_name,
            require_stdio_proxy=False,
        )
        if incompatible_wrapper_result is not None:
            return incompatible_wrapper_result

        # Set project context from session_id or stdio proxy headers
        ctx_token = await request_context._set_context_for_request(server, arguments, request)
        # Note: session_id is NOT stripped from arguments — tools like
        # get_session and other lookup tools use it as their own parameter.
        # request_context._set_context_for_request reads it non-destructively via .get().
        # InternalToolRegistry.call strips unknown kwargs via signature inspection.
        try:
            timeout = _mcp_call_timeout(server)
            scope_project = _http_request_scope(request, server, ctx_token, body)
            if server.mcp_manager is not None:
                resolved = resolve_server(
                    server.mcp_manager,
                    str(server_name) if server_name else None,
                    server_id=str(server_id) if isinstance(server_id, str) else None,
                    project_id=scope_project,
                )
                if resolved is None and not (
                    server._internal_manager
                    and isinstance(server_name, str)
                    and server._internal_manager.is_internal(server_name)
                ):
                    response_time_ms = (time.perf_counter() - start_time) * 1000
                    return {
                        "success": False,
                        "error": f"Unknown MCP server: '{server_id or server_name}'",
                        "response_time_ms": response_time_ms,
                    }
                if resolved is not None:
                    server_name = resolved.name
            # Route through ToolProxyService for consistent error enrichment
            if server.tool_proxy:
                result = await server.tool_proxy.call_tool(
                    server_name,
                    tool_name,
                    arguments,
                    session_id=ctx_token.resolved_session_id,
                    timeout=timeout,
                    wrapper_originated=True,
                    intent=intent,
                    project_id=scope_project,
                    offload=offload,
                )
                response_time_ms = (time.perf_counter() - start_time) * 1000
                return _process_tool_proxy_result(result, server_name, tool_name, response_time_ms)

            # Fallback: no tool_proxy available, use direct registry calls
            # Check internal first
            if server._internal_manager and server._internal_manager.is_internal(server_name):
                registry = server._internal_manager.get_registry(server_name)
                if registry:
                    return await _call_internal_tool(
                        registry, server_name, tool_name, arguments, start_time
                    )

            if server.mcp_manager is None:
                raise HTTPException(
                    status_code=503,
                    detail={"success": False, "error": "MCP manager not available"},
                )

            # Call external MCP tool
            try:
                result = await server.mcp_manager.call_tool(
                    server_name, tool_name, arguments, timeout=timeout
                )
                response_time_ms = (time.perf_counter() - start_time) * 1000
                inc_counter("mcp_tool_calls_succeeded_total")

                return _success_response_payload(result, response_time_ms)

            except TimeoutError:
                inc_counter("mcp_tool_calls_failed_total")
                response_time_ms = (time.perf_counter() - start_time) * 1000
                return _timeout_response_payload(timeout, response_time_ms)
            except Exception as e:
                inc_counter("mcp_tool_calls_failed_total")
                logger.exception(
                    "MCP tool call error: %s.%s",
                    server_name,
                    tool_name,
                    extra={"server": server_name, "tool": tool_name},
                )
                raise HTTPException(status_code=500, detail="Internal server error") from e
        finally:
            request_context._reset_context(ctx_token)

    except HTTPException:
        raise
    except Exception as e:
        inc_counter("mcp_tool_calls_failed_total")
        logger.exception("Call MCP tool error")
        raise HTTPException(status_code=500, detail="Internal server error") from e


async def mcp_proxy(
    server_name: str,
    tool_name: str,
    request: Request,
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """
    Unified MCP proxy endpoint for calling MCP server tools.

    Args:
        server_name: Name of the MCP server
        tool_name: Name of the tool to call
        request: FastAPI request with tool arguments in body

    Returns:
        Tool execution result
    """
    start_time = time.perf_counter()
    inc_counter("mcp_tool_calls_total")

    try:
        # Parse request body as tool arguments
        try:
            arguments = await request.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": f"Invalid JSON in request body: {e}"},
            ) from e
        raw_intent = request.query_params.get("intent")
        intent = raw_intent if isinstance(raw_intent, str) and raw_intent else None

        incompatible_wrapper_result = _incompatible_stdio_wrapper_wait_result(
            request,
            tool_name,
            require_stdio_proxy=True,
        )
        if incompatible_wrapper_result is not None:
            return incompatible_wrapper_result

        # Set project context from session_id or stdio proxy headers
        ctx_token = await request_context._set_context_for_request(server, arguments, request)
        try:
            timeout = _mcp_call_timeout(server)
            scope_project = _http_request_scope(request, server, ctx_token, arguments)
            # Route through ToolProxyService for consistent error enrichment
            if server.tool_proxy:
                result = await server.tool_proxy.call_tool(
                    server_name,
                    tool_name,
                    arguments,
                    session_id=ctx_token.resolved_session_id,
                    timeout=timeout,
                    wrapper_originated=True,
                    intent=intent,
                    project_id=scope_project,
                )
                response_time_ms = (time.perf_counter() - start_time) * 1000
                return _process_tool_proxy_result(result, server_name, tool_name, response_time_ms)

            # Fallback: no tool_proxy available, use direct registry calls
            # Check internal registries first (gobby-tasks, gobby-memory, etc.)
            if server._internal_manager and server._internal_manager.is_internal(server_name):
                registry = server._internal_manager.get_registry(server_name)
                if registry:
                    return await _call_internal_tool(
                        registry, server_name, tool_name, arguments, start_time
                    )
                raise HTTPException(
                    status_code=404,
                    detail={
                        "success": False,
                        "error": f"Internal server '{server_name}' not found",
                    },
                )

            if server.mcp_manager is None:
                raise HTTPException(
                    status_code=503,
                    detail={"success": False, "error": "MCP manager not available"},
                )

            # Call MCP tool
            try:
                result = await server.mcp_manager.call_tool(
                    server_name, tool_name, arguments, timeout=timeout
                )

                response_time_ms = (time.perf_counter() - start_time) * 1000

                logger.debug(
                    "MCP tool call successful: %s.%s",
                    server_name,
                    tool_name,
                    extra={
                        "server": server_name,
                        "tool": tool_name,
                        "response_time_ms": response_time_ms,
                    },
                )

                inc_counter("mcp_tool_calls_succeeded_total")

                return _success_response_payload(result, response_time_ms)

            except TimeoutError:
                inc_counter("mcp_tool_calls_failed_total")
                response_time_ms = (time.perf_counter() - start_time) * 1000
                return _timeout_response_payload(timeout, response_time_ms)
            except ValueError as e:
                inc_counter("mcp_tool_calls_failed_total")
                logger.warning(
                    "MCP tool not found: %s.%s",
                    server_name,
                    tool_name,
                    extra={"server": server_name, "tool": tool_name, "error": str(e)},
                )
                raise HTTPException(
                    status_code=404, detail={"success": False, "error": str(e)}
                ) from e
            except Exception as e:
                inc_counter("mcp_tool_calls_failed_total")
                logger.exception(
                    "MCP tool call error: %s.%s",
                    server_name,
                    tool_name,
                    extra={"server": server_name, "tool": tool_name},
                )
                raise HTTPException(status_code=500, detail="Internal server error") from e

        finally:
            request_context._reset_context(ctx_token)

    except HTTPException:
        raise
    except Exception as e:
        inc_counter("mcp_tool_calls_failed_total")
        logger.exception("MCP proxy error: %s.%s", server_name, tool_name)
        raise HTTPException(status_code=500, detail="Internal server error") from e


__all__ = [
    "list_mcp_tools",
    "get_tool_schema",
    "call_mcp_tool",
    "mcp_proxy",
    "_process_tool_proxy_result",
]

"""
Execution endpoints for MCP tool invocation.

Extracted from tools.py as part of Phase 2 Strangler Fig decomposition.
These endpoints handle tool listing, schema retrieval, and tool execution.
"""

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import Depends, HTTPException, Request

from gobby.mcp_proxy.tools.internal import normalize_internal_success_result
from gobby.servers.routes.dependencies import get_internal_manager, get_mcp_manager, get_server
from gobby.storage.session_resolution import resolve_session_reference
from gobby.telemetry.instruments import inc_counter, observe_histogram
from gobby.utils.session_context import (
    SeededContextTokens,
    get_current_session_id,
    reset_seeded_contexts,
    resolve_and_seed_contexts,
)

if TYPE_CHECKING:
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.mcp_proxy.registry_manager import InternalToolRegistryManager
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


# Backwards-compatible alias — older call sites (and tests) may still reach for
# ``_ContextTokens``. The returned shape is now ``SeededContextTokens``; callers
# treat it as an opaque handle for ``_reset_context`` / ``reset_seeded_contexts``.
_ContextTokens = SeededContextTokens


def _get_requested_session_id(arguments: Any, request: Request | None = None) -> str | None:
    """Return the raw session reference from tool arguments or HTTP headers.

    Discovery routes need the caller-supplied value for audit/proxy events even
    when context seeding cannot resolve it to a platform UUID.
    """
    if isinstance(arguments, dict):
        session_id = arguments.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id

    if request is None:
        return None

    header_session_id = request.headers.get("x-gobby-session-id")
    return header_session_id or None


def _get_argument_session_id(arguments: Any) -> str | None:
    """Return a target-tool session_id from the request body when present."""
    if isinstance(arguments, dict):
        session_id = arguments.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
    return None


def _get_discovery_session_id(arguments: Any, request: Request | None = None) -> str | None:
    """Return the session ref that should own HTTP discovery side effects.

    For HTTP callers, the session header identifies the requesting CLI session.
    Body/session arguments may target some other session for tool semantics, so
    discovery tracking prefers the header and only falls back to arguments.
    """
    if request is not None:
        header_session_id = request.headers.get("x-gobby-session-id")
        if header_session_id:
            return header_session_id

    return _get_requested_session_id(arguments, request)


def _session_ref_seq_num(session_ref: str | None) -> int | None:
    if not session_ref:
        return None
    raw = session_ref[1:] if session_ref.startswith("#") else session_ref
    return int(raw) if raw.isdigit() else None


def _derive_project_from_unique_session_seq(
    server: "HTTPServer", session_ref: str | None
) -> str | None:
    """Return a project_id for an unscoped #N session ref when it is unambiguous."""
    seq_num = _session_ref_seq_num(session_ref)
    session_manager = server.session_manager if server.session_manager else None
    db = session_manager.db if session_manager else None
    if seq_num is None or db is None:
        return None

    try:
        rows = db.fetchall(
            """
            SELECT DISTINCT project_id
            FROM sessions
            WHERE seq_num = ? AND project_id IS NOT NULL
            LIMIT 2
            """,
            (seq_num,),
        )
    except Exception as exc:
        logger.debug(
            "Could not derive project from session ref %r: %s",
            session_ref,
            exc,
        )
        return None

    if len(rows) == 1:
        project_id = rows[0]["project_id"]
        return str(project_id) if project_id else None
    if len(rows) > 1:
        logger.debug(
            "Session ref %r is ambiguous across projects; project header is required",
            session_ref,
        )
    return None


def _set_context_for_request(
    server: "HTTPServer", arguments: Any, request: Request | None = None
) -> SeededContextTokens:
    """Set project and session context vars from the best available source.

    Priority:
      1. X-Gobby-Session-Id header (the caller/workflow context)
      2. session_id from tool arguments (the target tool parameter)
      3. X-Gobby-Project-Id header (injected by stdio proxy from CWD project.json)

    The stdio process runs in the CLI's project directory, so its CWD-derived
    project_id is always correct. The daemon's CWD is NOT — it points to the
    gobby project regardless of which project the caller is in.

    Returns seeded tokens; pass them to ``_reset_context`` after the tool call.
    """
    header_session_id = request.headers.get("x-gobby-session-id") if request else None
    project_id_header = request.headers.get("x-gobby-project-id") if request else None
    argument_session_id = _get_argument_session_id(arguments)

    # Header session is wrapper/caller context. Body session_id remains a
    # target-tool parameter and must not make child-session workflow
    # enforcement apply to the caller.
    session_id = header_session_id or argument_session_id

    # HTTP-specific bootstrap: when the incoming session_id is #N/numeric and
    # the X-Gobby-Project-Id header is missing, derive a project scope from the
    # header-session UUID so the #N lookup can succeed. (After Change 1,
    # resolve_session_reference handles external_id UUIDs in the header too.)
    canonical_project_ref = project_id_header
    if not canonical_project_ref and header_session_id:
        canonical_project_ref = _derive_project_from_unique_session_seq(server, header_session_id)
    if (
        not canonical_project_ref
        and header_session_id
        and argument_session_id
        and server.session_manager
        and argument_session_id.lstrip("#").isdigit()
    ):
        try:
            bootstrap_id = resolve_session_reference(server.session_manager.db, header_session_id)
            bootstrap_session = server.session_manager.get(bootstrap_id)
            if bootstrap_session:
                canonical_project_ref = bootstrap_session.project_id
        except Exception as e:
            logger.debug(
                f"HTTP project bootstrap from header session {header_session_id!r} failed: {e}"
            )

    db = server.session_manager.db if server.session_manager else None
    return resolve_and_seed_contexts(
        session_ref=session_id,
        session_manager=server.session_manager if server.session_manager else None,
        project_ref=canonical_project_ref,
        project_ref_is_fallback=True,
        db=db,
    )


def _reset_context(tokens: SeededContextTokens) -> None:
    """Reset project and session context vars."""
    reset_seeded_contexts(tokens)


async def _emit_proxy_after_tool(
    server: "HTTPServer",
    *,
    session_id: str | None = None,
    tool_name: str,
    tool_input: dict[str, Any],
    result: dict[str, Any],
    is_failure: bool = False,
) -> None:
    """Emit synthetic proxy AFTER_TOOL events for HTTP discovery routes."""
    if server.tool_proxy is None:
        return

    await server.tool_proxy.emit_synthetic_proxy_after_tool(
        session_id=session_id or get_current_session_id(),
        tool_name=tool_name,
        tool_input=tool_input,
        result=result,
        is_failure=is_failure,
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
                f"MCP tool call failed: {server_name}.{tool_name} (error_code={error_code})",
                extra={
                    "server": server_name,
                    "tool": tool_name,
                    "error_code": error_code,
                },
            )

        return {**result, "response_time_ms": response_time_ms}
    else:
        inc_counter("mcp_tool_calls_succeeded_total")
        logger.debug(
            f"MCP tool call successful: {server_name}.{tool_name}",
            extra={
                "server": server_name,
                "tool": tool_name,
                "response_time_ms": response_time_ms,
            },
        )

    # Return 200 with wrapped result for success cases
    return {
        "success": True,
        "result": result,
        "response_time_ms": response_time_ms,
    }


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
        return {
            "success": True,
            "result": result,
            "response_time_ms": response_time_ms,
        }
    except Exception as e:
        inc_counter("mcp_tool_calls_failed_total")
        error_msg = str(e) or f"{type(e).__name__}: (no message)"
        raise HTTPException(
            status_code=500,
            detail={"success": False, "error": error_msg},
        ) from e


async def list_mcp_tools(
    server_name: str,
    request: Request,
    server: "HTTPServer" = Depends(get_server),
    internal_manager: "InternalToolRegistryManager | None" = Depends(get_internal_manager),
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
    requested_session_id = _get_discovery_session_id({}, request)
    ctx_token = _set_context_for_request(server, {}, request)

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
                        session_id=requested_session_id,
                    )
                result = {
                    "success": True,
                    "tools": tools,
                    "tool_count": len(tools),
                    "response_time_ms": response_time_ms,
                }
                await _emit_proxy_after_tool(
                    server,
                    session_id=requested_session_id,
                    tool_name="list_tools",
                    tool_input={"server_name": server_name},
                    result=result,
                )
                return result
            response_time_ms = (time.perf_counter() - start_time) * 1000
            result = {
                "success": False,
                "error": f"Internal server '{server_name}' not found",
                "response_time_ms": response_time_ms,
            }
            await _emit_proxy_after_tool(
                server,
                session_id=requested_session_id,
                tool_name="list_tools",
                tool_input={"server_name": server_name},
                result=result,
                is_failure=True,
            )
            return result

        if mcp_manager is None:
            response_time_ms = (time.perf_counter() - start_time) * 1000
            result = {
                "success": False,
                "error": "MCP manager not available",
                "response_time_ms": response_time_ms,
            }
            await _emit_proxy_after_tool(
                server,
                session_id=requested_session_id,
                tool_name="list_tools",
                tool_input={"server_name": server_name},
                result=result,
                is_failure=True,
            )
            return result

        # Check if server is configured
        if not mcp_manager.has_server(server_name):
            raise HTTPException(
                status_code=404,
                detail={"success": False, "error": f"Unknown MCP server: '{server_name}'"},
            )

        # Use ensure_connected for lazy loading - connects on-demand if not connected
        try:
            session = await mcp_manager.ensure_connected(server_name)
        except KeyError as e:
            response_time_ms = (time.perf_counter() - start_time) * 1000
            result = {"success": False, "error": str(e), "response_time_ms": response_time_ms}
            await _emit_proxy_after_tool(
                server,
                session_id=requested_session_id,
                tool_name="list_tools",
                tool_input={"server_name": server_name},
                result=result,
                is_failure=True,
            )
            return result
        except Exception as e:
            response_time_ms = (time.perf_counter() - start_time) * 1000
            result = {
                "success": False,
                "error": f"MCP server '{server_name}' connection failed: {e}",
                "response_time_ms": response_time_ms,
            }
            await _emit_proxy_after_tool(
                server,
                session_id=requested_session_id,
                tool_name="list_tools",
                tool_input={"server_name": server_name},
                result=result,
                is_failure=True,
            )
            return result

        # List tools using MCP SDK
        try:
            tools_result = await session.list_tools()
            tools = []
            for tool in tools_result.tools:
                tool_dict: dict[str, Any] = {
                    "name": tool.name,
                    "description": tool.description if hasattr(tool, "description") else None,
                }

                # Handle inputSchema
                if hasattr(tool, "inputSchema"):
                    schema = tool.inputSchema
                    if hasattr(schema, "model_dump"):
                        tool_dict["inputSchema"] = schema.model_dump()
                    elif isinstance(schema, dict):
                        tool_dict["inputSchema"] = schema
                    else:
                        tool_dict["inputSchema"] = None
                else:
                    tool_dict["inputSchema"] = None

                tools.append(tool_dict)

            response_time_ms = (time.perf_counter() - start_time) * 1000

            logger.debug(
                f"Listed {len(tools)} tools from {server_name}",
                extra={
                    "server": server_name,
                    "tool_count": len(tools),
                    "response_time_ms": response_time_ms,
                },
            )
            if server.tool_proxy:
                server.tool_proxy.record_listed_server(
                    server_name,
                    session_id=get_current_session_id(),
                )

            result = {
                "success": True,
                "tools": tools,
                "tool_count": len(tools),
                "response_time_ms": response_time_ms,
            }
            await _emit_proxy_after_tool(
                server,
                tool_name="list_tools",
                tool_input={"server_name": server_name},
                result=result,
            )
            return result

        except Exception as e:
            logger.error(
                f"Failed to list tools from {server_name}: {e}",
                exc_info=True,
                extra={"server": server_name},
            )
            response_time_ms = (time.perf_counter() - start_time) * 1000
            result = {
                "success": False,
                "error": f"Failed to list tools: {e}",
                "response_time_ms": response_time_ms,
            }
            await _emit_proxy_after_tool(
                server,
                tool_name="list_tools",
                tool_input={"server_name": server_name},
                result=result,
                is_failure=True,
            )
            return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"MCP list tools error: {server_name}", exc_info=True)
        response_time_ms = (time.perf_counter() - start_time) * 1000
        return {"success": False, "error": str(e), "response_time_ms": response_time_ms}
    finally:
        _reset_context(ctx_token)


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

        if not server_name or not tool_name:
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": "Required fields: server_name, tool_name"},
            )

        requested_session_id = _get_discovery_session_id(body, request)
        ctx_token = _set_context_for_request(server, body, request)

        try:
            # Check internal first
            if server._internal_manager and server._internal_manager.is_internal(server_name):
                registry = server._internal_manager.get_registry(server_name)
                if registry:
                    schema = registry.get_schema(tool_name)
                    if schema:
                        response_time_ms = (time.perf_counter() - start_time) * 1000
                        # unlocked_tools is owned by the track-schema-lookup
                        # rule fired off the synthetic AFTER_TOOL emitted below.
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
                        await _emit_proxy_after_tool(
                            server,
                            session_id=requested_session_id,
                            tool_name="get_tool_schema",
                            tool_input={"server_name": server_name, "tool_name": tool_name},
                            result=result,
                        )
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

            # Get from external MCP server
            try:
                tool_info = await server.mcp_manager.get_tool_info(server_name, tool_name)
                response_time_ms = (time.perf_counter() - start_time) * 1000
                # unlocked_tools is owned by the track-schema-lookup rule fired
                # off the synthetic AFTER_TOOL emitted below.

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
                await _emit_proxy_after_tool(
                    server,
                    session_id=requested_session_id,
                    tool_name="get_tool_schema",
                    tool_input={"server_name": server_name, "tool_name": tool_name},
                    result=response,
                )
                return response

            except (KeyError, ValueError) as e:
                # Tool or server not found
                response_time_ms = (time.perf_counter() - start_time) * 1000
                response = {"success": False, "error": str(e), "response_time_ms": response_time_ms}
                await _emit_proxy_after_tool(
                    server,
                    session_id=requested_session_id,
                    tool_name="get_tool_schema",
                    tool_input={"server_name": server_name, "tool_name": tool_name},
                    result=response,
                    is_failure=True,
                )
                return response
            except Exception as e:
                # Connection, timeout, or internal errors
                logger.error(
                    f"Failed to get tool schema {server_name}/{tool_name}: {e}",
                    exc_info=True,
                )
                response_time_ms = (time.perf_counter() - start_time) * 1000
                response = {
                    "success": False,
                    "error": f"Failed to get tool schema: {e}",
                    "response_time_ms": response_time_ms,
                }
                await _emit_proxy_after_tool(
                    server,
                    session_id=requested_session_id,
                    tool_name="get_tool_schema",
                    tool_input={"server_name": server_name, "tool_name": tool_name},
                    result=response,
                    is_failure=True,
                )
                return response
        finally:
            _reset_context(ctx_token)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get tool schema error: {e}", exc_info=True)
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
        tool_name = body.get("tool_name")
        arguments = body.get("arguments", {})

        if not server_name or not tool_name:
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": "Required fields: server_name, tool_name"},
            )

        # Set project context from session_id or stdio proxy headers
        ctx_token = _set_context_for_request(server, arguments, request)
        # Note: session_id is NOT stripped from arguments — tools like
        # get_session and get_handoff_context use it as their own parameter.
        # _set_context_for_request reads it non-destructively via .get().
        # InternalToolRegistry.call strips unknown kwargs via signature inspection.
        try:
            # Route through ToolProxyService for consistent error enrichment
            if server.tool_proxy:
                result = await server.tool_proxy.call_tool(
                    server_name,
                    tool_name,
                    arguments,
                    session_id=get_current_session_id(),
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
                result = await server.mcp_manager.call_tool(server_name, tool_name, arguments)
                response_time_ms = (time.perf_counter() - start_time) * 1000
                inc_counter("mcp_tool_calls_succeeded_total")

                return {
                    "success": True,
                    "result": result,
                    "response_time_ms": response_time_ms,
                }

            except Exception as e:
                inc_counter("mcp_tool_calls_failed_total")
                error_msg = str(e) or f"{type(e).__name__}: (no message)"
                raise HTTPException(
                    status_code=500, detail={"success": False, "error": error_msg}
                ) from e
        finally:
            _reset_context(ctx_token)

    except HTTPException:
        raise
    except Exception as e:
        inc_counter("mcp_tool_calls_failed_total")
        error_msg = str(e) or f"{type(e).__name__}: (no message)"
        logger.error(f"Call MCP tool error: {error_msg}", exc_info=True)
        raise HTTPException(status_code=500, detail={"success": False, "error": error_msg}) from e


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

        # Set project context from session_id or stdio proxy headers
        ctx_token = _set_context_for_request(server, arguments, request)
        try:
            # Route through ToolProxyService for consistent error enrichment
            if server.tool_proxy:
                result = await server.tool_proxy.call_tool(
                    server_name,
                    tool_name,
                    arguments,
                    session_id=get_current_session_id(),
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
                result = await server.mcp_manager.call_tool(server_name, tool_name, arguments)

                response_time_ms = (time.perf_counter() - start_time) * 1000

                logger.debug(
                    f"MCP tool call successful: {server_name}.{tool_name}",
                    extra={
                        "server": server_name,
                        "tool": tool_name,
                        "response_time_ms": response_time_ms,
                    },
                )

                inc_counter("mcp_tool_calls_succeeded_total")

                return {
                    "success": True,
                    "result": result,
                    "response_time_ms": response_time_ms,
                }

            except ValueError as e:
                inc_counter("mcp_tool_calls_failed_total")
                logger.warning(
                    f"MCP tool not found: {server_name}.{tool_name}",
                    extra={"server": server_name, "tool": tool_name, "error": str(e)},
                )
                raise HTTPException(
                    status_code=404, detail={"success": False, "error": str(e)}
                ) from e
            except Exception as e:
                inc_counter("mcp_tool_calls_failed_total")
                error_msg = str(e) or f"{type(e).__name__}: (no message)"
                logger.error(
                    f"MCP tool call error: {server_name}.{tool_name}",
                    exc_info=True,
                    extra={"server": server_name, "tool": tool_name},
                )
                raise HTTPException(
                    status_code=500, detail={"success": False, "error": error_msg}
                ) from e

        finally:
            _reset_context(ctx_token)

    except HTTPException:
        raise
    except Exception as e:
        inc_counter("mcp_tool_calls_failed_total")
        error_msg = str(e) or f"{type(e).__name__}: (no message)"
        logger.error(f"MCP proxy error: {server_name}.{tool_name}", exc_info=True)
        raise HTTPException(status_code=500, detail={"success": False, "error": error_msg}) from e


__all__ = [
    "list_mcp_tools",
    "get_tool_schema",
    "call_mcp_tool",
    "mcp_proxy",
    "_process_tool_proxy_result",
]

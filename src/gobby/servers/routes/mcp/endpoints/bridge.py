"""Stdio bridge lifecycle endpoints."""

from typing import TYPE_CHECKING, Any

from fastapi import Depends, HTTPException, Request

from gobby.hooks.event_handlers._session_start.claims import MCP_PROXY_READY_VARIABLE
from gobby.servers.routes.dependencies import get_server
from gobby.servers.routes.mcp.endpoints import request_context
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


async def report_bridge_ready(
    request: Request,
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """Record that the caller's CLI has listed the Gobby proxy tools.

    The memory-lifecycle rules read ``_mcp_proxy_ready`` to tell a first turn
    that started before the bridge connected from a session with no bridge.
    An unresolved wrapper caller gets 409 SESSION_REQUIRED, which the bridge
    retries while the CLI's session registration catches up.
    """
    tokens = await request_context._set_context_for_request(server, {}, request)
    try:
        session_id = tokens.resolved_session_id
        if session_id is None:
            return {"success": False, "error": "No session resolved for bridge report"}
        variables = SessionVariableManager(server.session_manager.db)
        recorded = await server.run_db(
            variables.merge_variables, session_id, {MCP_PROXY_READY_VARIABLE: True}
        )
        return {"success": bool(recorded), "session_id": session_id}
    finally:
        request_context._reset_context(tokens)


async def get_bridge_tool_timeouts(
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """Return the per-tool timeouts the stdio bridge applies to its daemon calls.

    Serving the daemon's active map keeps the storage stack, a hub connection
    and a full config read out of the bridge's first tool call (#23680).
    """
    config = server.config
    if config is None:
        raise HTTPException(status_code=503, detail="Daemon configuration unavailable")
    return {"success": True, "tool_timeouts": dict(config.mcp_client_proxy.tool_timeouts)}

"""HTTP MCP transport for Ask's three wrapper tools.

The existing HTTP tool handlers own identity, workflow policy and schema leases.
This adapter supplies their request shape without starting Python in the source
checkout, which an Ask investigator must never be allowed to read.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import Context, MCPServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.types import Message

from gobby.mcp_proxy.wait_tools import (
    MCP_WRAPPER_PROTOCOL_VERSION,
    MCP_WRAPPER_PROTOCOL_VERSION_HEADER,
)
from gobby.servers.routes.mcp.endpoints import execution

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

ASK_MCP_PATH = "/api/ask/mcp"


def _tool_request(ctx: Context, body: dict[str, Any]) -> Request:
    """Retain the authenticated HTTP identity while replacing the MCP envelope."""
    original = ctx.request_context.request
    if not isinstance(original, Request):
        raise PermissionError("Ask MCP requires an authenticated HTTP request")
    caller_project = original.headers.get("X-Gobby-Caller-Project-Id") or original.headers.get(
        "X-Gobby-Project-Id"
    )
    for target_project in (original.headers.get("X-Gobby-Project-Id"), body.get("project_id")):
        if target_project is not None and target_project != caller_project:
            raise PermissionError("Ask MCP cannot target another project")
    payload = json.dumps(body).encode()
    headers = [
        (key, value)
        for key, value in original.scope["headers"]
        if key.lower()
        not in {b"content-length", MCP_WRAPPER_PROTOCOL_VERSION_HEADER.lower().encode()}
    ]
    headers.append(
        (
            MCP_WRAPPER_PROTOCOL_VERSION_HEADER.lower().encode(),
            MCP_WRAPPER_PROTOCOL_VERSION.encode(),
        )
    )

    async def receive() -> Message:
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request({**original.scope, "headers": headers}, receive=receive)


def create_ask_mcp_app(server: HTTPServer, *, host: str) -> Starlette:
    """Expose only the wrappers admitted by the immutable Ask launch profile."""
    mcp = MCPServer("gobby")

    @mcp.tool()
    async def call_tool(
        server_name: str,
        tool_name: str,
        ctx: Context,
        arguments: dict[str, Any] | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
        intent: str | None = None,
    ) -> dict[str, Any]:
        """Call an allowed stage tool; the authenticated run owns caller identity."""
        request = _tool_request(
            ctx,
            {
                "server_name": server_name,
                "tool_name": tool_name,
                "arguments": arguments or {},
                "session_id": session_id,
                "project_id": project_id,
                "intent": intent,
                # Evidence owns bounded pagination. An offloaded gobby-results
                # reference is inaccessible through Ask's exact stage allowlist.
                "offload": False,
            },
        )
        return await execution.call_mcp_tool(request, server)

    @mcp.tool()
    async def get_tool_schema(
        server_name: str,
        tool_name: str,
        ctx: Context,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Get and lease the schema of an allowed stage tool before calling it."""
        request = _tool_request(
            ctx,
            {"server_name": server_name, "tool_name": tool_name, "session_id": session_id},
        )
        return await execution.get_tool_schema(request, server)

    @mcp.tool()
    async def list_tools(
        server_name: str, ctx: Context, session_id: str | None = None
    ) -> dict[str, Any]:
        """List the tools this run's current stage may call on a server."""
        request = _tool_request(ctx, {"session_id": session_id})
        return await execution.list_mcp_tools(
            server_name, request, server, server._internal_manager, server.mcp_manager
        )

    # Each POST carries and authenticates its own identity; no connection session
    # can retain a prior run's authority across subsequent requests.
    return mcp.streamable_http_app(
        streamable_http_path=ASK_MCP_PATH,
        stateless_http=True,
        json_response=True,
        host=host,
    )

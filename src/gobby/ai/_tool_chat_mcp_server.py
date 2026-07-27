"""Isolated streamable-HTTP MCP server for external tool-chat agents."""

from __future__ import annotations

import secrets
import socket
from collections.abc import Awaitable, Callable

from aiohttp import web

from gobby.ai._tool_chat_contracts import (
    MAX_TOOL_CALLS_STOP_REASON,
    MAX_TURNS_STOP_REASON,
    ToolLoopLimits,
)
from gobby.ai._tool_chat_tools import ToolPolicyError, ToolRuntime, tool_result_is_error

_MCP_PROTOCOL_VERSION = "2025-06-18"
_SERVER_NAME = "gobby-tool-loop"
_SERVER_VERSION = "1.0.0"

InterruptCallback = Callable[[], Awaitable[None]]


class ToolLoopController:
    """Coordinate turn/call termination across a provider and its MCP server."""

    def __init__(self, limits: ToolLoopLimits) -> None:
        self.limits = limits
        self.turns = 0
        self.stop_reason: str | None = None
        self._interrupt: InterruptCallback | None = None

    def set_interrupt(self, callback: InterruptCallback) -> None:
        self._interrupt = callback

    def record_turn(self) -> None:
        self.turns += 1

    async def before_tool(self, runtime: ToolRuntime) -> str | None:
        reason: str | None = None
        if self.limits.max_turns is not None and self.turns >= self.limits.max_turns:
            reason = MAX_TURNS_STOP_REASON
        elif runtime.budget_exhausted:
            reason = MAX_TOOL_CALLS_STOP_REASON
        if reason is None:
            return None
        if self.stop_reason is None:
            self.stop_reason = reason
            if self._interrupt is not None:
                await self._interrupt()
        return reason


class ToolRuntimeMCPServer:
    """Serve one request's :class:`ToolRuntime` on an authenticated loopback socket."""

    def __init__(self, runtime: ToolRuntime, controller: ToolLoopController) -> None:
        self._runtime = runtime
        self._controller = controller
        self._token = secrets.token_urlsafe(32)
        self._runner: web.AppRunner | None = None
        self._site: web.SockSite | None = None
        self._socket: socket.socket | None = None
        self.url: str | None = None

    @property
    def authorization_header(self) -> str:
        return f"Bearer {self._token}"

    async def start(self) -> None:
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_post("/mcp", self._handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()

        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind(("127.0.0.1", 0))
        server_socket.listen(128)
        server_socket.setblocking(False)
        port = int(server_socket.getsockname()[1])

        site = web.SockSite(runner, server_socket)
        try:
            await site.start()
        except BaseException:
            server_socket.close()
            await runner.cleanup()
            raise

        self._runner = runner
        self._site = site
        self._socket = server_socket
        self.url = f"http://127.0.0.1:{port}/mcp"

    async def stop(self) -> None:
        runner = self._runner
        server_socket = self._socket
        self._runner = None
        self._site = None
        self._socket = None
        self.url = None
        try:
            if runner is not None:
                await runner.cleanup()
        finally:
            if server_socket is not None:
                server_socket.close()

    async def __aenter__(self) -> ToolRuntimeMCPServer:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        await self.stop()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if not secrets.compare_digest(
            request.headers.get("Authorization", ""),
            self.authorization_header,
        ):
            raise web.HTTPUnauthorized()
        try:
            payload = await request.json()
        except (ValueError, TypeError):
            return self._json_error(None, -32700, "Parse error")
        if not isinstance(payload, dict):
            return self._json_error(None, -32600, "Invalid Request")

        request_id = payload.get("id")
        method = payload.get("method")
        if not isinstance(method, str):
            return self._json_error(request_id, -32600, "Invalid Request")
        if method == "notifications/initialized":
            return web.Response(status=202)
        if "id" not in payload:
            return web.Response(status=202)

        if method == "initialize":
            params = payload.get("params")
            protocol_version = _MCP_PROTOCOL_VERSION
            if isinstance(params, dict) and isinstance(params.get("protocolVersion"), str):
                protocol_version = params["protocolVersion"]
            return self._json_result(
                request_id,
                {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": _SERVER_NAME, "version": _SERVER_VERSION},
                },
            )
        if method == "ping":
            return self._json_result(request_id, {})
        if method == "tools/list":
            return self._json_result(
                request_id,
                {
                    "tools": [
                        {
                            "name": name,
                            "description": self._runtime.description_for(name),
                            "inputSchema": self._runtime.input_schema_for(name),
                        }
                        for name in self._runtime.tool_names()
                    ]
                },
            )
        if method == "tools/call":
            return await self._call_tool(request_id, payload.get("params"))
        return self._json_error(request_id, -32601, f"Method not found: {method}")

    async def _call_tool(self, request_id: object, params: object) -> web.Response:
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return self._json_error(request_id, -32602, "Invalid tools/call params")
        reason = await self._controller.before_tool(self._runtime)
        if reason is not None:
            return self._json_result(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": f"[error: {reason} exhausted before tool execution]",
                        }
                    ],
                    "isError": True,
                },
            )
        try:
            text = await self._runtime.execute(params["name"], params.get("arguments", {}))
        except ToolPolicyError as exc:
            text = f"[error: {exc}]"
        return self._json_result(
            request_id,
            {
                "content": [{"type": "text", "text": text}],
                "isError": tool_result_is_error(text),
            },
        )

    @staticmethod
    def _json_result(request_id: object, result: object) -> web.Response:
        return web.json_response({"jsonrpc": "2.0", "id": request_id, "result": result})

    @staticmethod
    def _json_error(
        request_id: object,
        code: int,
        message: str,
    ) -> web.Response:
        return web.json_response(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )

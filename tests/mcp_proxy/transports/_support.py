"""Shared fixtures for MCP 2.0 transport tests.

Two layers:

* ``FakeClient`` stands in for ``mcp.client.Client`` so lifecycle tests can
  observe transport entry/exit ordering and inject handshake or exit failures
  without a live server.
* ``modern_server`` / ``legacy_transport`` are real in-memory peers driven by
  the real ``Client``: the modern one answers ``server/discover``; the legacy
  one rejects it at the wire level and only speaks the ``initialize``
  handshake, exercising ``negotiate_auto``'s fallback for real.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any
from unittest.mock import AsyncMock

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.message import SessionMessage
from mcp.types import (
    METHOD_NOT_FOUND,
    ErrorData,
    JSONRPCError,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
)

LEGACY_PROTOCOL_VERSION = "2025-11-25"
LEGACY_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string", "default": None}},
}


class FakeClient:
    """Mirror of ``Client``'s contract: enter the transport, expose ``session``.

    ``handshake_error`` reproduces ``Client.__aenter__`` failing after the
    transport was entered (the real client unwinds the transport itself before
    re-raising). ``exit_error`` / ``exit_delay`` shape ``__aexit__`` for the
    disconnect error-handling tests.
    """

    def __init__(
        self,
        transport: Any,
        *,
        session: Any | None = None,
        lifecycle: list[str] | None = None,
        handshake_error: BaseException | None = None,
        handshake_gate: asyncio.Event | None = None,
        exit_error: BaseException | None = None,
        exit_delay: float = 0.0,
    ) -> None:
        self.transport = transport
        self._session = session if session is not None else AsyncMock()
        self.lifecycle = lifecycle if lifecycle is not None else []
        self.handshake_error = handshake_error
        self.handshake_gate = handshake_gate
        self.exit_error = exit_error
        self.exit_delay = exit_delay
        self.entered = False
        self.exited = False
        self.streams: Any = None

    async def __aenter__(self) -> FakeClient:
        self.streams = await self.transport.__aenter__()
        self.lifecycle.append("transport-enter")
        try:
            if self.handshake_gate is not None:
                await self.handshake_gate.wait()
            if self.handshake_error is not None:
                raise self.handshake_error
        except BaseException as exc:
            # Like the real Client: a failed or cancelled handshake unwinds
            # the transport before the error leaves __aenter__.
            await self.transport.__aexit__(type(exc), exc, None)
            self.lifecycle.append("transport-exit")
            raise
        self.entered = True
        self.lifecycle.append("handshake")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.exited = True
        if self.exit_delay:
            await asyncio.sleep(self.exit_delay)
        if self.exit_error is not None:
            raise self.exit_error
        await self.transport.__aexit__(exc_type, exc_val, exc_tb)
        self.lifecycle.append("transport-exit")

    @property
    def session(self) -> Any:
        if not self.entered:
            raise RuntimeError("Client must be used within an async context manager")
        return self._session


@asynccontextmanager
async def recording_transport(
    lifecycle: list[str], *, enter_error: BaseException | None = None
) -> AsyncIterator[tuple[Any, Any]]:
    """Transport double that records entry/exit and can fail on entry."""
    if enter_error is not None:
        raise enter_error
    lifecycle.append("streams-open")
    try:
        yield object(), object()
    finally:
        lifecycle.append("streams-closed")


def modern_server(name: str = "modern", version: str = "9.9.9") -> MCPServer:
    """A real MCPServer (2026-07-28 era) with one echo tool."""
    server = MCPServer(name, version=version)

    @server.tool()
    def echo(text: str) -> str:
        return text

    return server


async def _serve_legacy(read: Any, write: Any) -> None:
    """Answer the handshake-era wire only; ``server/discover`` is unknown here."""
    async with read, write:
        async for item in read:
            if isinstance(item, Exception):
                raise item
            message = item.message
            if isinstance(message, JSONRPCNotification):
                continue
            assert isinstance(message, JSONRPCRequest)
            reply: JSONRPCResponse | JSONRPCError
            if message.method == "initialize":
                reply = JSONRPCResponse(
                    jsonrpc="2.0",
                    id=message.id,
                    result={
                        "protocolVersion": LEGACY_PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "legacy", "version": "1.0"},
                    },
                )
            elif message.method == "tools/list":
                reply = JSONRPCResponse(
                    jsonrpc="2.0",
                    id=message.id,
                    result={"tools": [{"name": "echo", "inputSchema": LEGACY_TOOL_SCHEMA}]},
                )
            elif message.method == "tools/call":
                params = message.params or {}
                text = str(params.get("arguments", {}).get("text", ""))
                reply = JSONRPCResponse(
                    jsonrpc="2.0",
                    id=message.id,
                    result={"content": [{"type": "text", "text": text}], "isError": False},
                )
            else:
                reply = JSONRPCError(
                    jsonrpc="2.0",
                    id=message.id,
                    error=ErrorData(
                        code=METHOD_NOT_FOUND, message=f"Method not found: {message.method}"
                    ),
                )
            await write.send(SessionMessage(reply))


@asynccontextmanager
async def legacy_transport() -> AsyncIterator[tuple[Any, Any]]:
    """In-memory ``Transport`` to a server that only speaks the legacy handshake."""
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(_serve_legacy, *server_streams)
            try:
                yield client_streams
            finally:
                task_group.cancel_scope.cancel()

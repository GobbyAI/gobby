"""Adapter between Starlette and the daemon WebSocket connection handler."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import WebSocket


class ASGIWebSocketAdapter(AsyncIterator[str | bytes]):
    """Expose a Starlette WebSocket through the interface used by WebSocketServer."""

    def __init__(self, websocket: WebSocket, *, user_id: str) -> None:
        self._websocket = websocket
        self.user_id = user_id
        self.latency = 0.0
        self.remote_address = websocket.client
        self.subscriptions: set[str] = set()
        self.accepted = False
        self.closed = False
        self.disconnected = False
        self.close_code: int | None = None
        self.close_reason: str | None = None

    async def accept(self) -> None:
        await self._websocket.accept()
        self.accepted = True

    async def send(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            await self._websocket.send_bytes(message)
        else:
            await self._websocket.send_text(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed or self.disconnected:
            return
        self.closed = True
        self.close_code = code
        self.close_reason = reason
        await self._websocket.close(code=code, reason=reason)

    def __aiter__(self) -> ASGIWebSocketAdapter:
        return self

    async def __anext__(self) -> str | bytes:
        message = await self._websocket.receive()
        message_type = message.get("type")
        if message_type == "websocket.disconnect":
            self.disconnected = True
            self.close_code = int(message.get("code", 1000))
            self.close_reason = str(message.get("reason", ""))
            raise StopAsyncIteration
        if message_type != "websocket.receive":
            raise RuntimeError(f"Unexpected ASGI WebSocket message: {message_type}")
        if message.get("bytes") is not None:
            return bytes(message["bytes"])
        if message.get("text") is not None:
            return str(message["text"])
        raise RuntimeError("ASGI WebSocket receive message had no text or bytes payload")

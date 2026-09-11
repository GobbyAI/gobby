"""Unavailable proxy backends must still complete the ASGI handshake."""

from unittest.mock import patch

import pytest
from starlette.types import Message
from starlette.websockets import WebSocket

from gobby.servers._app_ui import _proxy_websocket


@pytest.mark.asyncio
async def test_unavailable_backend_rejects_websocket_handshake() -> None:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    websocket = WebSocket({"type": "websocket", "headers": []}, receive, send)
    with patch("websockets.connect", side_effect=ConnectionRefusedError("backend unavailable")):
        await _proxy_websocket(websocket, "ws://localhost:5173/__vite_hmr")

    assert sent == [{"type": "websocket.close", "code": 1011, "reason": ""}]

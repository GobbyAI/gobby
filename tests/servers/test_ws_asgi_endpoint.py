from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import ConnectionClosedError

from gobby.servers._app_ui import _mount_ws_endpoint
from gobby.servers.http import HTTPServer
from gobby.servers.websocket.asgi_adapter import ASGIWebSocketAdapter
from gobby.servers.websocket.server import WebSocketServer

pytestmark = pytest.mark.unit


class _AuthService:
    enabled = True

    def is_request_authenticated(self, websocket: WebSocket) -> bool:
        return (
            websocket.headers.get("Authorization") == "Bearer browser-token"
            or websocket.cookies.get("gobby_session") == "session-token"
        )


class _WebSocketServer:
    def __init__(
        self,
        *,
        return_immediately: bool = False,
        handler_error: Exception | None = None,
    ) -> None:
        self.clients: dict[Any, dict[str, Any]] = {}
        self.received: list[str | bytes] = []
        self.handler_calls = 0
        self.return_immediately = return_immediately
        self.handler_error = handler_error

    async def run_db(self, func: Any, *args: Any) -> Any:
        return func(*args)

    async def _handle_connection(self, websocket: Any) -> None:
        self.handler_calls += 1
        if self.handler_error is not None:
            raise self.handler_error
        self.clients[websocket] = {"user_id": websocket.user_id}
        try:
            if self.return_immediately:
                return
            await websocket.send(json.dumps({"type": "connection_established"}))
            for index in range(50):
                await websocket.send(json.dumps({"type": "token_event", "index": index}))
                await websocket.send(json.dumps({"type": "session_usage_updated", "index": index}))
            await websocket.send(b"tts-audio")
            async for message in websocket:
                self.received.append(message)
                await websocket.close(code=1012, reason="service restart")
        finally:
            self.clients.pop(websocket, None)

    async def handle_connection(self, websocket: Any) -> None:
        await self._handle_connection(websocket)


def _client(
    websocket_server: _WebSocketServer | None,
    *,
    auth_service: Any = None,
) -> TestClient:
    app = FastAPI()
    server = SimpleNamespace(
        auth_service=auth_service or _AuthService(),
        services=SimpleNamespace(websocket_server=websocket_server),
        websocket_server=websocket_server,
    )
    _mount_ws_endpoint(app, cast(HTTPServer, server))
    return TestClient(app)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "send_method"),
    [
        pytest.param("text payload", "send_text", id="text"),
        pytest.param(b"binary payload", "send_bytes", id="binary"),
    ],
)
async def test_adapter_send_normalizes_starlette_disconnect(
    message: str | bytes,
    send_method: str,
) -> None:
    websocket = MagicMock(spec=WebSocket)
    websocket.client = ("127.0.0.1", 1234)
    send = AsyncMock(side_effect=WebSocketDisconnect(code=1006, reason="client vanished"))
    setattr(websocket, send_method, send)
    adapter = ASGIWebSocketAdapter(websocket, user_id="test-user")

    with pytest.raises(ConnectionClosedError) as exc_info:
        await adapter.send(message)

    send.assert_awaited_once_with(message)
    assert isinstance(exc_info.value.__cause__, WebSocketDisconnect)
    assert adapter.disconnected is True
    assert adapter.closed is False
    assert adapter.close_code == 1006
    assert adapter.close_reason == "client vanished"


@pytest.mark.asyncio
async def test_adapter_close_suppresses_starlette_disconnect() -> None:
    websocket = MagicMock(spec=WebSocket)
    websocket.client = ("127.0.0.1", 1234)
    websocket.close = AsyncMock(
        side_effect=WebSocketDisconnect(code=1006, reason="client vanished")
    )
    adapter = ASGIWebSocketAdapter(websocket, user_id="test-user")

    await adapter.close(code=1011, reason="handler failed")

    assert adapter.closed is True
    assert adapter.disconnected is True
    assert adapter.close_code == 1006
    assert adapter.close_reason == "client vanished"


@pytest.mark.asyncio
async def test_disconnect_during_welcome_cleans_up_without_unexpected_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = MagicMock()
    config.host = "localhost"
    config.port = 60888
    config.ping_interval = 30
    config.ping_timeout = 10
    config.max_message_size = 1024
    websocket_server = WebSocketServer(config, MagicMock())
    cleanup_tmux_client = AsyncMock()

    app = FastAPI()
    server = SimpleNamespace(
        auth_service=SimpleNamespace(enabled=False),
        services=SimpleNamespace(websocket_server=websocket_server),
        websocket_server=websocket_server,
    )
    _mount_ws_endpoint(app, cast(HTTPServer, server))
    route = next(
        route for route in app.routes if isinstance(route, WebSocketRoute) and route.path == "/ws"
    )

    websocket = MagicMock(spec=WebSocket)
    websocket.client = ("127.0.0.1", 1234)
    websocket.accept = AsyncMock()
    websocket.send_text = AsyncMock(
        side_effect=WebSocketDisconnect(code=1006, reason="client vanished")
    )
    websocket.close = AsyncMock()
    caplog.set_level(logging.DEBUG, logger="gobby.servers.websocket.server")

    with patch.object(websocket_server, "_cleanup_tmux_client", cleanup_tmux_client):
        await route.endpoint(websocket)

    assert websocket_server.clients == {}
    cleanup_tmux_client.assert_awaited_once()
    websocket.close.assert_not_awaited()
    assert "connection closed abnormally" in caplog.text
    assert "Unexpected error for client" not in caplog.text


def test_unauthenticated_handshake_is_rejected_before_handler() -> None:
    websocket_server = _WebSocketServer()
    client = _client(websocket_server)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws"):
            pass

    assert exc_info.value.code == 4401
    assert exc_info.value.reason == "Authentication required"
    assert websocket_server.handler_calls == 0
    assert websocket_server.clients == {}


def test_authenticated_text_binary_bursts_close_reason_and_registry_cleanup() -> None:
    websocket_server = _WebSocketServer()
    client = _client(websocket_server)
    client.cookies.set("gobby_session", "session-token")

    with client.websocket_connect("/ws") as websocket:
        websocket.send_text(json.dumps({"type": "subscribe", "events": ["token_event"]}))
        assert websocket.receive_json() == {"type": "connection_established"}
        events = [websocket.receive_json() for _ in range(100)]
        assert sum(event["type"] == "token_event" for event in events) == 50
        assert sum(event["type"] == "session_usage_updated" for event in events) == 50
        assert websocket.receive_bytes() == b"tts-audio"
        with pytest.raises(WebSocketDisconnect) as exc_info:
            websocket.receive_text()
        assert exc_info.value.code == 1012
        assert exc_info.value.reason == "service restart"

    assert websocket_server.received == [
        json.dumps({"type": "subscribe", "events": ["token_event"]})
    ]
    assert websocket_server.clients == {}


def test_handler_failure_is_not_masked_as_clean_close() -> None:
    websocket_server = _WebSocketServer(handler_error=RuntimeError("handler failed"))
    client = _client(websocket_server)

    with client.websocket_connect(
        "/ws",
        headers={"Authorization": "Bearer browser-token"},
    ) as websocket:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            websocket.receive_text()

    assert exc_info.value.code == 1011
    assert exc_info.value.reason == "WebSocket handler exited unexpectedly"
    assert websocket_server.clients == {}


def test_clean_handler_return_uses_unexpected_exit_close() -> None:
    websocket_server = _WebSocketServer(return_immediately=True)
    client = _client(websocket_server)

    with client.websocket_connect(
        "/ws",
        headers={"Authorization": "Bearer browser-token"},
    ) as websocket:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            websocket.receive_text()

    assert exc_info.value.code == 1011
    assert exc_info.value.reason == "WebSocket handler exited unexpectedly"


def test_missing_websocket_server_returns_retry_later_close() -> None:
    client = _client(None)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws"):
            pass

    assert exc_info.value.code == 1013
    assert exc_info.value.reason == "WebSocket server unavailable"


def test_authentication_disabled_skips_handshake_check() -> None:
    websocket_server = _WebSocketServer(return_immediately=True)
    client = _client(websocket_server, auth_service=SimpleNamespace(enabled=False))

    with client.websocket_connect("/ws") as websocket:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            websocket.receive_text()

    assert exc_info.value.code == 1011
    assert websocket_server.handler_calls == 1

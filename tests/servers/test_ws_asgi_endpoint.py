from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import uvicorn
import websockets
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import ConnectionClosedError, InvalidStatus

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
    websocket_server = WebSocketServer(config, MagicMock(), AsyncMock(return_value="local-cli"))
    cast(Any, websocket_server).run_db = AsyncMock(side_effect=lambda func, *args: func(*args))
    cleanup_tmux_client = AsyncMock()

    app = FastAPI()
    server = SimpleNamespace(
        auth_service=SimpleNamespace(is_request_authenticated=lambda _request: True),
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

    with patch.object(cast(Any, websocket_server), "_cleanup_tmux_client", cleanup_tmux_client):
        await route.endpoint(websocket)

    assert websocket_server.clients == {}
    cleanup_tmux_client.assert_awaited_once()
    websocket.close.assert_not_awaited()
    assert "connection closed abnormally" in caplog.text
    assert "Unexpected error for client" not in caplog.text


def test_unauthenticated_handshake_is_rejected_before_handler(
    caplog: pytest.LogCaptureFixture,
) -> None:
    websocket_server = _WebSocketServer()
    client = _client(websocket_server)
    caplog.set_level(logging.ERROR)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws") as websocket:
            websocket.receive_text()

    assert exc_info.value.code == 4401
    assert exc_info.value.reason == "Authentication required"
    assert websocket_server.handler_calls == 0
    assert websocket_server.clients == {}
    assert "Exception in ASGI application" not in caplog.text
    assert "transfer_data_task" not in caplog.text
    assert "AttributeError" not in caplog.text


async def test_unauthenticated_handshake_closes_without_accept() -> None:
    websocket_server = _WebSocketServer()
    app = FastAPI()
    server = SimpleNamespace(
        auth_service=_AuthService(),
        services=SimpleNamespace(websocket_server=websocket_server),
        websocket_server=websocket_server,
    )
    _mount_ws_endpoint(app, cast(HTTPServer, server))
    route = next(
        route for route in app.routes if isinstance(route, WebSocketRoute) and route.path == "/ws"
    )

    events: list[str] = []
    websocket = MagicMock(spec=WebSocket)
    websocket.accept = AsyncMock(side_effect=lambda: events.append("accept"))
    websocket.close = AsyncMock(side_effect=lambda **_kwargs: events.append("close"))

    await route.endpoint(websocket)

    assert events == ["close"]
    websocket.accept.assert_not_awaited()
    websocket.close.assert_awaited_once_with(code=4401, reason="Authentication required")
    assert websocket_server.handler_calls == 0


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
        with client.websocket_connect("/ws") as websocket:
            websocket.receive_text()

    assert exc_info.value.code == 1013
    assert exc_info.value.reason == "WebSocket server unavailable"


def test_authentication_is_always_checked() -> None:
    websocket_server = _WebSocketServer(return_immediately=True)
    client = _client(
        websocket_server,
        auth_service=SimpleNamespace(is_request_authenticated=lambda _request: False),
    )

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws") as websocket:
            websocket.receive_text()

    assert exc_info.value.code == 4401
    assert websocket_server.handler_calls == 0


class _LogProbe(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        rendered = record.getMessage()
        if record.exc_info:
            rendered = f"{rendered}\n{logging.Formatter().formatException(record.exc_info)}"
        self.messages.append(rendered)

    def text(self) -> str:
        return "\n".join(self.messages)


@asynccontextmanager
async def _live_ws_server(websocket_server: _WebSocketServer | None) -> AsyncIterator[str]:
    app = FastAPI()
    server = SimpleNamespace(
        auth_service=_AuthService(),
        services=SimpleNamespace(websocket_server=websocket_server),
        websocket_server=websocket_server,
    )
    _mount_ws_endpoint(app, cast(HTTPServer, server))
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        access_log=False,
        lifespan="off",
        log_config=None,
        ws="websockets",
    )
    http = uvicorn.Server(config)
    task = asyncio.create_task(http.serve())
    try:
        while not http.started:
            if task.done():
                await task
            await asyncio.sleep(0)
        sockets = http.servers[0].sockets
        assert sockets is not None
        port = int(sockets[0].getsockname()[1])
        yield f"ws://127.0.0.1:{port}/ws"
    finally:
        http.should_exit = True
        await asyncio.wait_for(task, timeout=5)


async def _hangup_upgrade(url: str) -> None:
    host_port = url.removeprefix("ws://").split("/", 1)[0]
    host, port_s = host_port.rsplit(":", 1)
    _reader, writer = await asyncio.open_connection(host, int(port_s))
    writer.write(
        b"GET /ws HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        b"Sec-WebSocket-Version: 13\r\n"
        b"\r\n"
    )
    await writer.drain()
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "websocket_server",
    [pytest.param(_WebSocketServer(), id="unauthenticated"), pytest.param(None, id="no-server")],
)
async def test_pre_accept_rejection_hangup_does_not_raise_in_uvicorn(
    websocket_server: _WebSocketServer | None,
) -> None:
    probe = _LogProbe()
    logger = logging.getLogger("uvicorn.error")
    logger.addHandler(probe)
    try:
        async with _live_ws_server(websocket_server) as url:
            await _hangup_upgrade(url)
    finally:
        logger.removeHandler(probe)

    combined = probe.text()
    assert "Exception in ASGI application" not in combined
    assert "transfer_data_task" not in combined
    assert "AttributeError" not in combined


@pytest.mark.asyncio
async def test_unauthenticated_live_client_is_handshake_rejected() -> None:
    async with _live_ws_server(_WebSocketServer()) as url:
        with pytest.raises(InvalidStatus) as exc_info:
            async with websockets.connect(url, open_timeout=2.0, close_timeout=2.0):
                pass

    assert exc_info.value.response.status_code == 403

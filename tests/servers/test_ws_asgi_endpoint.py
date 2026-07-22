from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from gobby.servers._app_ui import _mount_ws_endpoint

pytestmark = pytest.mark.unit


class _AuthService:
    enabled = True

    def is_request_authenticated(self, websocket: WebSocket) -> bool:
        return (
            websocket.headers.get("Authorization") == "Bearer browser-token"
            or websocket.cookies.get("gobby_session") == "session-token"
        )


class _WebSocketServer:
    def __init__(self, *, return_immediately: bool = False) -> None:
        self.clients: dict[Any, dict[str, Any]] = {}
        self.received: list[str | bytes] = []
        self.handler_calls = 0
        self.return_immediately = return_immediately

    async def run_db(self, func: Any, *args: Any) -> Any:
        return func(*args)

    async def _handle_connection(self, websocket: Any) -> None:
        self.handler_calls += 1
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


def _client(
    websocket_server: _WebSocketServer,
    *,
    auth_service: _AuthService | None = None,
) -> TestClient:
    app = FastAPI()
    server = SimpleNamespace(
        auth_service=auth_service or _AuthService(),
        services=SimpleNamespace(websocket_server=websocket_server),
        websocket_server=websocket_server,
    )
    _mount_ws_endpoint(app, server)
    return TestClient(app)


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
    assert websocket_server.clients == {}

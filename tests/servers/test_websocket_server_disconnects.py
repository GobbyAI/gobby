"""Tests for WebSocketServer disconnect handling during message processing."""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

from gobby.servers.websocket.server import WebSocketServer

pytestmark = pytest.mark.unit


class IteratingWebSocket:
    def __init__(self, messages: list[str] | None = None, user_id: str = "test-user") -> None:
        self.user_id = user_id
        self.latency = 0.1
        self.sent_messages: list[str] = []
        self.closed = False
        self.remote_address = ("127.0.0.1", 12345)
        self._messages = list(messages or [])

    async def send(self, message: str) -> None:
        self.sent_messages.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True

    def __aiter__(self) -> IteratingWebSocket:
        return self

    async def __anext__(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        raise StopAsyncIteration


@pytest.fixture
def mock_config() -> MagicMock:
    config = MagicMock()
    config.host = "localhost"
    config.port = 60888
    config.ping_interval = 30
    config.ping_timeout = 10
    config.max_message_size = 1024
    return config


@pytest.fixture
def mock_mcp_manager() -> MagicMock:
    return MagicMock()


@pytest.fixture
def server(mock_config: MagicMock, mock_mcp_manager: MagicMock) -> WebSocketServer:
    return WebSocketServer(mock_config, mock_mcp_manager, AsyncMock(return_value="test-user"))


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["5", "[]"])
async def test_handle_message_rejects_non_object_json(
    server: WebSocketServer, message: str
) -> None:
    websocket = IteratingWebSocket()

    await server._handle_message(websocket, message)

    assert len(websocket.sent_messages) == 1
    assert json.loads(websocket.sent_messages[0]) == {
        "type": "error",
        "code": "ERROR",
        "message": "Message must be a JSON object",
    }


class TestHandleConnectionDisconnects:
    @pytest.mark.asyncio
    async def test_disconnect_cleans_attached_session_tts(self, server: WebSocketServer) -> None:
        ws = IteratingWebSocket(messages=['{"type":"session_update"}'])
        pipeline = MagicMock()
        pipeline.cancel = AsyncMock()
        server._active_tts_pipelines["attached-session"] = pipeline
        server._attached_tts_offsets.update(
            {
                "attached-session:message-1": 12,
                "attached-session:message-2": 34,
                "other-session:message-1": 56,
            }
        )
        server._rebroadcast_pending_interactions = AsyncMock()
        server._cleanup_tmux_client = AsyncMock()
        server._check_voice_idle = AsyncMock()

        async def disconnect_after_attach(websocket: IteratingWebSocket, _message: str) -> None:
            server.clients[websocket]["attached_session_id"] = "attached-session"
            raise ConnectionClosedOK(
                Close(1001, "going away"),
                Close(1001, "going away"),
                True,
            )

        server._handle_message = AsyncMock(side_effect=disconnect_after_attach)

        await server._handle_connection(ws)

        pipeline.cancel.assert_awaited_once_with()
        assert "attached-session" not in server._active_tts_pipelines
        assert server._attached_tts_offsets == {"other-session:message-1": 56}
        server._check_voice_idle.assert_awaited_once_with()
        assert ws not in server.clients

    @pytest.mark.asyncio
    async def test_connection_closed_during_handler_does_not_log_message_error(
        self, server: WebSocketServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        ws = IteratingWebSocket(messages=['{"type":"terminal_list"}'])
        server._rebroadcast_pending_interactions = AsyncMock()
        server._handle_message = AsyncMock(
            side_effect=ConnectionClosedOK(
                Close(1001, "going away"),
                Close(1001, "going away"),
                True,
            )
        )
        server._send_error = AsyncMock()

        with caplog.at_level(logging.DEBUG):
            await server._handle_connection(ws)

        server._send_error.assert_not_awaited()
        assert "Message handling error" not in caplog.text
        assert "disconnected normally" in caplog.text
        assert ws not in server.clients

    @pytest.mark.asyncio
    async def test_non_connection_handler_error_still_logs_and_sends_error(
        self, server: WebSocketServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        ws = IteratingWebSocket(messages=['{"type":"terminal_list"}'])
        server._rebroadcast_pending_interactions = AsyncMock()
        server._handle_message = AsyncMock(side_effect=RuntimeError("boom"))
        server._send_error = AsyncMock()

        with caplog.at_level(logging.ERROR):
            await server._handle_connection(ws)

        server._send_error.assert_awaited_once_with(ws, "Internal server error")
        assert "Message handling error" in caplog.text
        assert ws not in server.clients

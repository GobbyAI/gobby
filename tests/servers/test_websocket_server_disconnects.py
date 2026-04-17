"""Tests for WebSocketServer disconnect handling during message processing."""

from __future__ import annotations

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
    return WebSocketServer(mock_config, mock_mcp_manager)


class TestHandleConnectionDisconnects:
    @pytest.mark.asyncio
    async def test_connection_closed_during_handler_does_not_log_message_error(
        self, server: WebSocketServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        ws = IteratingWebSocket(messages=['{"type":"tmux_list_sessions"}'])
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
        ws = IteratingWebSocket(messages=['{"type":"tmux_list_sessions"}'])
        server._rebroadcast_pending_interactions = AsyncMock()
        server._handle_message = AsyncMock(side_effect=RuntimeError("boom"))
        server._send_error = AsyncMock()

        with caplog.at_level(logging.ERROR):
            await server._handle_connection(ws)

        server._send_error.assert_awaited_once_with(ws, "Internal server error")
        assert "Message handling error" in caplog.text
        assert ws not in server.clients

"""Tests for the WebSocket server wrapper."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.servers.websocket.server import WebSocketServer


@pytest.mark.asyncio
async def test_start_passes_warning_level_websockets_logger() -> None:
    config = MagicMock()
    config.host = "127.0.0.1"
    config.port = 60888
    config.ping_interval = 30
    config.ping_timeout = 10
    config.max_message_size = 1024

    server = WebSocketServer(config=config, mcp_manager=MagicMock())
    server._cleanup_idle_sessions = AsyncMock()

    with patch("gobby.servers.websocket.server.serve", new_callable=AsyncMock) as mock_serve:
        await server.start()

    assert server._cleanup_task is not None
    await server._cleanup_task
    mock_serve.assert_awaited_once()
    serve_kwargs = mock_serve.await_args.kwargs
    assert serve_kwargs["logger"].name == "websockets.server"
    assert serve_kwargs["logger"].level == logging.WARNING

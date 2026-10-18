"""Native-mode wiring of WebSocketServer: bind on start, fanout to the publisher."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.servers.websocket.server import WebSocketServer


@pytest.fixture
def publisher() -> MagicMock:
    native = MagicMock()
    native.bind = AsyncMock(return_value="generation-1")
    native.publish = AsyncMock()
    native.stop = AsyncMock()
    return native


@pytest.fixture
def server(publisher: MagicMock) -> WebSocketServer:
    mock_config = MagicMock()
    mock_config.host = "127.0.0.1"
    mock_config.port = 0
    mock_config.ping_interval = 30
    mock_config.ping_timeout = 10
    mock_config.max_message_size = 1024
    websocket_server = WebSocketServer(mock_config, MagicMock(), AsyncMock(return_value="u"))
    websocket_server.native_publisher = publisher
    # configure_terminals attaches the lease registry at composition time.
    websocket_server.lease_registry = MagicMock()
    websocket_server.lease_registry.shutdown_lifecycle_publication = AsyncMock()
    websocket_server._cleanup_idle_sessions = AsyncMock()
    return websocket_server


@pytest.mark.asyncio
async def test_native_broadcast_hands_frames_to_publisher_without_clients(
    server: WebSocketServer, publisher: MagicMock
) -> None:
    message = {"type": "session_event", "event": "created"}
    frames = [{"type": "chunk", "n": 1}, {"type": "chunk", "n": 2}]
    local_client = AsyncMock()
    server.clients[local_client] = {"subscriptions": set()}

    await server.broadcast(message, frames=frames)

    publisher.publish.assert_awaited_once_with(message, frames)
    # gdaemon's broker owns fanout: the local client is neither written nor dropped.
    local_client.send.assert_not_awaited()
    assert local_client in server.clients


@pytest.mark.asyncio
async def test_native_session_event_reaches_publisher_with_no_clients(
    server: WebSocketServer, publisher: MagicMock
) -> None:
    server._session_event_project_id = AsyncMock(return_value=None)

    await server.broadcast_session_event("created", "sess-1")

    publisher.publish.assert_awaited_once()
    (message, _frames), _kwargs = publisher.publish.await_args
    assert message["type"] == "session_event"
    assert message["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_native_start_binds_through_publisher_and_serves_no_listener(
    server: WebSocketServer, publisher: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve = AsyncMock()
    monkeypatch.setattr("gobby.servers.websocket.server.serve", serve)

    await server.start()

    publisher.bind.assert_awaited_once()
    serve.assert_not_awaited()
    assert server._server is None


@pytest.mark.asyncio
async def test_native_start_raises_on_bind_failure(
    server: WebSocketServer, publisher: MagicMock
) -> None:
    publisher.bind.side_effect = RuntimeError("bind refused")

    with pytest.raises(RuntimeError, match="bind refused"):
        await server.start()

    server.lease_registry.shutdown_lifecycle_publication.assert_awaited_once()


@pytest.mark.asyncio
async def test_native_stop_stops_the_publisher(
    server: WebSocketServer, publisher: MagicMock
) -> None:
    order: list[str] = []
    server._cleanup_terminals = AsyncMock(side_effect=lambda: order.append("terminals"))
    server.cleanup_voice = AsyncMock()
    publisher.stop.side_effect = lambda: order.append("publisher")
    await server.start()

    await server.stop()

    # Terminals drain before the publisher stops, so no fanout starts after the stop.
    assert order == ["terminals", "publisher"]
    publisher.stop.assert_awaited_once()

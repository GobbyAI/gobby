"""Tests for WebSocket subscriptions."""

from unittest.mock import MagicMock

import pytest
from gobby.servers.websocket import WebSocketServer


class MockWebSocket:
    def __init__(self, user_id="test-user"):
        self.user_id = user_id
        self.latency = 0.1
        self.sent_messages = []
        self.closed = False

    async def send(self, message):
        self.sent_messages.append(message)

    async def close(self, code=1000, reason=""):
        self.closed = True


@pytest.fixture
def mock_mcp_manager():
    return MagicMock()


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.host = "localhost"
    config.port = 8765
    config.ping_interval = 30
    config.ping_timeout = 10
    config.max_message_size = 1024
    return config


@pytest.mark.asyncio
async def test_subscribe_success(mock_config, mock_mcp_manager):
    server = WebSocketServer(mock_config, mock_mcp_manager)
    ws = MockWebSocket()

    data = {"events": ["event1", "event2"]}
    await server._handle_subscribe(ws, data)

    assert hasattr(ws, "subscriptions")
    assert "event1" in ws.subscriptions
    assert "event2" in ws.subscriptions

    # Check success message
    assert len(ws.sent_messages) == 1
    assert "subscribe_success" in ws.sent_messages[0]


@pytest.mark.asyncio
async def test_unsubscribe_success(mock_config, mock_mcp_manager):
    server = WebSocketServer(mock_config, mock_mcp_manager)
    ws = MockWebSocket()
    ws.subscriptions = {"event1", "event2"}

    data = {"events": ["event1"]}
    await server._handle_unsubscribe(ws, data)

    assert "event1" not in ws.subscriptions
    assert "event2" in ws.subscriptions

    # Check success message
    assert len(ws.sent_messages) == 1
    assert "unsubscribe_success" in ws.sent_messages[0]


@pytest.mark.asyncio
async def test_unsubscribe_all(mock_config, mock_mcp_manager):
    server = WebSocketServer(mock_config, mock_mcp_manager)
    ws = MockWebSocket()
    ws.subscriptions = {"event1", "event2"}

    data = {"events": ["*"]}
    await server._handle_unsubscribe(ws, data)

    assert len(ws.subscriptions) == 0


@pytest.mark.asyncio
async def test_broadcast_filtering(mock_config, mock_mcp_manager):
    server = WebSocketServer(mock_config, mock_mcp_manager)

    # Client 1: No subscription (should receive everything)
    ws1 = MockWebSocket("client1")
    server.clients[ws1] = {"id": "1"}

    # Client 2: Subscribed to event1
    ws2 = MockWebSocket("client2")
    ws2.subscriptions = {"event1"}
    server.clients[ws2] = {"id": "2"}

    # Client 3: Subscribed to event2
    ws3 = MockWebSocket("client3")
    ws3.subscriptions = {"event2"}
    server.clients[ws3] = {"id": "3"}

    # 1. Broadcast event1
    msg1 = {"type": "hook_event", "event_type": "event1"}
    await server.broadcast(msg1)

    assert len(ws1.sent_messages) == 1
    assert len(ws2.sent_messages) == 1
    assert len(ws3.sent_messages) == 0

    # 2. Broadcast event2
    msg2 = {"type": "hook_event", "event_type": "event2"}
    await server.broadcast(msg2)

    assert len(ws1.sent_messages) == 2
    assert len(ws2.sent_messages) == 1
    assert len(ws3.sent_messages) == 1

    # 3. Broadcast system message (no event_type)
    msg3 = {"type": "system_message"}
    await server.broadcast(msg3)

    assert len(ws1.sent_messages) == 3
    assert len(ws2.sent_messages) == 2
    assert len(ws3.sent_messages) == 2

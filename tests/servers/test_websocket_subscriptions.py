"""Tests for WebSocket subscriptions."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.servers.websocket.server import WebSocketServer

pytestmark = pytest.mark.unit


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
    config.port = 60888
    config.ping_interval = 30
    config.ping_timeout = 10
    config.max_message_size = 1024
    return config


@pytest.mark.asyncio
async def test_subscribe_success(mock_config, mock_mcp_manager):
    server = WebSocketServer(mock_config, mock_mcp_manager, AsyncMock(return_value="test-user"))
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
    server = WebSocketServer(mock_config, mock_mcp_manager, AsyncMock(return_value="test-user"))
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
    server = WebSocketServer(mock_config, mock_mcp_manager, AsyncMock(return_value="test-user"))
    ws = MockWebSocket()
    ws.subscriptions = {"event1", "event2"}

    data = {"events": ["*"]}
    await server._handle_unsubscribe(ws, data)

    assert len(ws.subscriptions) == 0


@pytest.mark.asyncio
async def test_broadcast_filtering(mock_config, mock_mcp_manager):
    server = WebSocketServer(mock_config, mock_mcp_manager, AsyncMock(return_value="test-user"))

    # Client 1: No subscription (should receive nothing after deprecation cleanup)
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

    # Client 4: Subscribed to wildcard (receives everything)
    ws4 = MockWebSocket("client4")
    ws4.subscriptions = {"*"}
    server.clients[ws4] = {"id": "4"}

    # 1. Broadcast event1
    msg1 = {"type": "hook_event", "event_type": "event1"}
    await server.broadcast(msg1)

    assert len(ws1.sent_messages) == 0  # No subscriptions = receive nothing
    assert len(ws2.sent_messages) == 1
    assert len(ws3.sent_messages) == 0
    assert len(ws4.sent_messages) == 1  # Wildcard receives everything

    # 2. Broadcast event2
    msg2 = {"type": "hook_event", "event_type": "event2"}
    await server.broadcast(msg2)

    assert len(ws1.sent_messages) == 0
    assert len(ws2.sent_messages) == 1
    assert len(ws3.sent_messages) == 1
    assert len(ws4.sent_messages) == 2

    # 3. Broadcast system message (no event_type, not hook_event or session_message)
    msg3 = {"type": "system_message"}
    await server.broadcast(msg3)

    assert len(ws1.sent_messages) == 0  # Still receives nothing
    assert (
        len(ws2.sent_messages) == 2
    )  # Non-hook/non-session messages pass through for subscribed clients
    assert len(ws3.sent_messages) == 2
    assert len(ws4.sent_messages) == 3


@pytest.mark.asyncio
async def test_parametric_subscription_matches(mock_config, mock_mcp_manager):
    """Parametric subscription 'type:key=value' filters by message field."""
    server = WebSocketServer(mock_config, mock_mcp_manager, AsyncMock(return_value="test-user"))

    ws1 = MockWebSocket("client1")
    ws1.subscriptions = {"session_message:session_id=abc123"}
    server.clients[ws1] = {"id": "1"}

    ws2 = MockWebSocket("client2")
    ws2.subscriptions = {"session_message:session_id=other456"}
    server.clients[ws2] = {"id": "2"}

    # Broadcast session_message for abc123
    msg = {"type": "session_message", "session_id": "abc123", "message": {"role": "user"}}
    await server.broadcast(msg)

    assert len(ws1.sent_messages) == 1  # Matches
    assert len(ws2.sent_messages) == 0  # Different session_id


@pytest.mark.asyncio
async def test_parametric_subscription_no_match(mock_config, mock_mcp_manager):
    """Parametric subscription doesn't match if the value is different."""
    server = WebSocketServer(mock_config, mock_mcp_manager, AsyncMock(return_value="test-user"))

    ws = MockWebSocket("client1")
    ws.subscriptions = {"session_message:session_id=abc123"}
    server.clients[ws] = {"id": "1"}

    # Broadcast session_message for a different session
    msg = {"type": "session_message", "session_id": "xyz789", "message": {"role": "user"}}
    await server.broadcast(msg)

    assert len(ws.sent_messages) == 0


@pytest.mark.asyncio
async def test_parametric_and_type_subscription_coexist(mock_config, mock_mcp_manager):
    """A client can have both type-level and parametric subscriptions."""
    server = WebSocketServer(mock_config, mock_mcp_manager, AsyncMock(return_value="test-user"))

    # Client subscribed to all session_message AND parametric hook_event
    ws = MockWebSocket("client1")
    ws.subscriptions = {"session_message", "hook_event:session_id=ext-123"}
    server.clients[ws] = {"id": "1"}

    # session_message: type-level match
    msg1 = {"type": "session_message", "session_id": "any", "message": {}}
    await server.broadcast(msg1)
    assert len(ws.sent_messages) == 1

    # hook_event with matching session_id: parametric match
    msg2 = {"type": "hook_event", "session_id": "ext-123", "event_type": "after_tool"}
    await server.broadcast(msg2)
    assert len(ws.sent_messages) == 2

    # hook_event with non-matching session_id: no match
    msg3 = {"type": "hook_event", "session_id": "other", "event_type": "after_tool"}
    await server.broadcast(msg3)
    assert len(ws.sent_messages) == 2  # No change

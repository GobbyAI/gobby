"""Tests for WebSocket broadcast events in agent messaging.

Verifies that send_message broadcasts agent_message events via WebSocket.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════════
# Mock helpers (reused from test_agent_messaging)
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class MockSession:
    id: str
    parent_session_id: str | None = None
    project_id: str = "project-1"
    status: str = "active"
    agent_depth: int = 0


@dataclass
class MockMessage:
    id: str = "msg-1"
    from_session: str = "s-from"
    to_session: str = "s-to"
    content: str = "hello"
    priority: str = "normal"
    sent_at: str = "2026-01-01T00:00:00"
    read_at: str | None = None
    message_type: str = "message"
    metadata_json: str | None = None
    delivered_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "from_session": self.from_session,
            "to_session": self.to_session,
            "content": self.content,
            "priority": self.priority,
            "sent_at": self.sent_at,
        }


class MockWebSocket:
    """Mock WebSocket with subscription support."""

    def __init__(self, user_id: str = "test-user") -> None:
        self.user_id = user_id
        self.latency = 0.1
        self.sent_messages: list[str] = []
        self.closed = False
        self.subscriptions: set[str] = {"*"}

    async def send(self, message: str) -> None:
        self.sent_messages.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True

    def all_messages(self) -> list[dict]:
        return [json.loads(m) for m in self.sent_messages]

    def messages_of_type(self, msg_type: str) -> list[dict]:
        return [m for m in self.all_messages() if m.get("type") == msg_type]


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_session_manager():
    mgr = MagicMock()
    mgr.resolve_session_reference = MagicMock(side_effect=lambda ref, project_id=None: ref)
    mgr.get = MagicMock(return_value=None)
    mgr.is_ancestor = MagicMock(return_value=False)
    return mgr


@pytest.fixture
def mock_message_manager():
    mgr = MagicMock()
    mgr.create_message = MagicMock(return_value=MockMessage())
    mgr.get_undelivered_messages = MagicMock(return_value=[])
    mgr.mark_delivered = MagicMock()
    return mgr


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.fetchone = MagicMock(return_value=None)
    db.execute = MagicMock()
    return db


@pytest.fixture
def mock_broadcast_fn():
    return AsyncMock()


@pytest.fixture
def messaging_registry_with_broadcast(
    mock_session_manager,
    mock_message_manager,
    mock_db,
    mock_broadcast_fn,
):
    """Registry with broadcast_fn wired."""
    from gobby.mcp_proxy.tools.agent_messaging import add_messaging_tools

    registry = InternalToolRegistry(
        name="gobby-agents",
        description="Agent messaging with broadcast",
    )
    add_messaging_tools(
        registry=registry,
        message_manager=mock_message_manager,
        session_manager=mock_session_manager,
        db=mock_db,
        broadcast_fn=mock_broadcast_fn,
    )
    return registry


# ═══════════════════════════════════════════════════════════════════════
# BroadcastMixin: new methods and event type registration
# ═══════════════════════════════════════════════════════════════════════


class TestBroadcastMixinAgentMessaging:
    """Test that BroadcastMixin has an agent_message method."""

    def test_broadcast_agent_message_method_exists(self) -> None:
        from gobby.servers.websocket.broadcast import BroadcastMixin

        assert hasattr(BroadcastMixin, "broadcast_agent_message")

    @pytest.mark.asyncio
    async def test_broadcast_agent_message_sends_correct_type(self) -> None:
        """broadcast_agent_message sends type=agent_message with fields."""
        from gobby.servers.websocket.broadcast import BroadcastMixin

        ws = MockWebSocket()
        mixin = BroadcastMixin()
        mixin.clients = {ws: {"id": "1"}}

        await mixin.broadcast_agent_message(
            event="message_sent",
            from_session="s-from",
            to_session="s-to",
        )

        msgs = ws.messages_of_type("agent_message")
        assert len(msgs) == 1
        assert msgs[0]["event"] == "message_sent"
        assert msgs[0]["from_session"] == "s-from"
        assert msgs[0]["to_session"] == "s-to"
        assert "timestamp" in msgs[0]


class TestSubscriptionFiltering:
    """Test that agent_message requires explicit subscription."""

    @pytest.mark.asyncio
    async def test_agent_message_requires_subscription(self) -> None:
        """Client without agent_message subscription does not receive it."""
        from gobby.servers.websocket.broadcast import BroadcastMixin

        ws_no_sub = MockWebSocket()
        ws_no_sub.subscriptions = {"hook_event"}  # subscribed to something else

        ws_with_sub = MockWebSocket()
        ws_with_sub.subscriptions = {"agent_message"}

        mixin = BroadcastMixin()
        mixin.clients = {ws_no_sub: {"id": "1"}, ws_with_sub: {"id": "2"}}

        await mixin.broadcast_agent_message(
            event="message_sent",
            from_session="s-from",
            to_session="s-to",
        )

        assert len(ws_no_sub.sent_messages) == 0
        assert len(ws_with_sub.sent_messages) == 1

    @pytest.mark.asyncio
    async def test_wildcard_receives_agent_events(self) -> None:
        """Client with wildcard subscription receives agent events."""
        from gobby.servers.websocket.broadcast import BroadcastMixin

        ws = MockWebSocket()
        ws.subscriptions = {"*"}

        mixin = BroadcastMixin()
        mixin.clients = {ws: {"id": "1"}}

        await mixin.broadcast_agent_message(
            event="message_sent",
            from_session="a",
            to_session="b",
        )

        assert len(ws.sent_messages) == 1


# ═══════════════════════════════════════════════════════════════════════
# send_message broadcasts agent_message event
# ═══════════════════════════════════════════════════════════════════════


class TestSendMessageBroadcast:
    """send_message calls broadcast_fn with agent_message event on success."""

    @pytest.mark.asyncio
    async def test_broadcast_on_success(
        self,
        messaging_registry_with_broadcast,
        mock_session_manager,
        mock_broadcast_fn,
    ) -> None:
        """Successful send_message triggers agent_message broadcast."""
        mock_session_manager.get.side_effect = lambda sid: {
            "s-from": MockSession(id="s-from", project_id="proj-1"),
            "s-to": MockSession(id="s-to", project_id="proj-1"),
        }.get(sid)

        result = await messaging_registry_with_broadcast.call(
            "send_message",
            {
                "from_session": "s-from",
                "target": "session",
                "target_id": "s-to",
                "content": "hello",
            },
        )

        assert result["success"] is True
        mock_broadcast_fn.assert_called_once()
        call_kwargs = mock_broadcast_fn.call_args[1]
        assert call_kwargs["msg_type"] == "agent_message"
        assert call_kwargs["event"] == "message_sent"
        assert call_kwargs["from_session"] == "s-from"
        assert call_kwargs["to_session"] == "s-to"

    @pytest.mark.asyncio
    async def test_send_message_returns_failed_broadcasts_per_recipient(
        self,
        mock_session_manager,
        mock_message_manager,
        mock_db,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Persisted fanout succeeds, but failed WebSocket broadcasts are reported."""
        from gobby.mcp_proxy.tools.agent_messaging import add_messaging_tools

        broadcast_fn = AsyncMock(side_effect=[RuntimeError("socket down"), None])
        registry = InternalToolRegistry(
            name="gobby-agents",
            description="Agent messaging with partial broadcast failure",
        )
        add_messaging_tools(
            registry=registry,
            message_manager=mock_message_manager,
            session_manager=mock_session_manager,
            db=mock_db,
            broadcast_fn=broadcast_fn,
        )
        mock_session_manager.get.side_effect = lambda sid: {
            "s-from": MockSession(id="s-from", project_id="proj-1"),
            "s-child-1": MockSession(id="s-child-1", project_id="proj-1"),
            "s-child-2": MockSession(id="s-child-2", project_id="proj-1"),
        }.get(sid)
        mock_db.fetchone.side_effect = lambda sql, params=(): (
            {"id": "proj-1"} if "FROM projects" in sql else None
        )
        mock_db.fetchall.return_value = [
            {
                "child_session_id": "s-child-1",
                "child_status": "active",
                "parent_session_id": "s-from",
                "parent_status": "active",
            },
            {
                "child_session_id": "s-child-2",
                "child_status": "active",
                "parent_session_id": "s-from",
                "parent_status": "active",
            },
        ]
        mock_message_manager.create_message.side_effect = lambda **kwargs: MockMessage(
            id=f"msg-{kwargs['to_session']}",
            from_session=kwargs["from_session"],
            to_session=kwargs["to_session"],
            content=kwargs["content"],
            priority=kwargs["priority"],
            message_type=kwargs["message_type"],
            metadata_json=kwargs["metadata_json"],
        )
        caplog.set_level(logging.WARNING, logger="gobby.mcp_proxy.tools.agent_messaging")

        result = await registry.call(
            "send_message",
            {
                "from_session": "s-from",
                "target": "project",
                "target_id": "proj-1",
                "content": "hello agents",
            },
        )

        assert result["success"] is False
        assert result["recipient_session_ids"] == ["s-child-1", "s-child-2"]
        assert result["failed_broadcasts"] == [
            {"recipient_session_id": "s-child-1", "error": "socket down"}
        ]
        assert broadcast_fn.await_count == 2
        failure_log = next(
            record
            for record in caplog.records
            if record.message == "Failed to broadcast agent_message"
        )
        assert failure_log.to_session == "s-child-1"
        assert failure_log.exc_info is not None

    @pytest.mark.asyncio
    async def test_no_broadcast_on_failure(
        self,
        messaging_registry_with_broadcast,
        mock_session_manager,
        mock_broadcast_fn,
    ) -> None:
        """Failed send_message does not broadcast."""
        mock_session_manager.get.return_value = None  # session not found

        result = await messaging_registry_with_broadcast.call(
            "send_message",
            {
                "from_session": "no-such",
                "target": "session",
                "target_id": "s-to",
                "content": "hi",
            },
        )

        assert result["success"] is False
        mock_broadcast_fn.assert_not_called()

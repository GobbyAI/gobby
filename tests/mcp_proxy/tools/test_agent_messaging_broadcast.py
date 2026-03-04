"""Tests for WebSocket broadcast events in agent messaging.

Verifies that send_message, send_command, and complete_command
broadcast agent_message and agent_command events via WebSocket.
"""

from __future__ import annotations

import json
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


@dataclass
class MockCommand:
    id: str = "cmd-1"
    from_session: str = "s-parent"
    to_session: str = "s-child"
    command_text: str = "Run tests"
    status: str = "pending"
    created_at: str = "2026-01-01T00:00:00"
    allowed_tools: str | None = None
    allowed_mcp_tools: str | None = None
    exit_condition: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "from_session": self.from_session,
            "to_session": self.to_session,
            "command_text": self.command_text,
            "status": self.status,
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
def mock_command_manager():
    mgr = MagicMock()
    mgr.create_command = MagicMock(return_value=MockCommand())
    mgr.get_command = MagicMock(return_value=None)
    mgr.list_commands = MagicMock(return_value=[])
    mgr.update_status = MagicMock(return_value=MockCommand(status="running"))
    return mgr


@pytest.fixture
def mock_session_var_manager():
    mgr = MagicMock()
    mgr.merge_variables = MagicMock(return_value=True)
    mgr.delete_variables = MagicMock()
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
    mock_command_manager,
    mock_session_var_manager,
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
        command_manager=mock_command_manager,
        session_var_manager=mock_session_var_manager,
        db=mock_db,
        broadcast_fn=mock_broadcast_fn,
    )
    return registry


# ═══════════════════════════════════════════════════════════════════════
# BroadcastMixin: new methods and event type registration
# ═══════════════════════════════════════════════════════════════════════


class TestBroadcastMixinAgentMessaging:
    """Test that BroadcastMixin has agent_message and agent_command methods."""

    def test_broadcast_agent_message_method_exists(self) -> None:
        from gobby.servers.websocket.broadcast import BroadcastMixin

        assert hasattr(BroadcastMixin, "broadcast_agent_message")

    def test_broadcast_agent_command_method_exists(self) -> None:
        from gobby.servers.websocket.broadcast import BroadcastMixin

        assert hasattr(BroadcastMixin, "broadcast_agent_command")

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

    @pytest.mark.asyncio
    async def test_broadcast_agent_command_sends_correct_type(self) -> None:
        """broadcast_agent_command sends type=agent_command with fields."""
        from gobby.servers.websocket.broadcast import BroadcastMixin

        ws = MockWebSocket()
        mixin = BroadcastMixin()
        mixin.clients = {ws: {"id": "1"}}

        await mixin.broadcast_agent_command(
            event="command_sent",
            from_session="s-parent",
            to_session="s-child",
            command_id="cmd-1",
        )

        msgs = ws.messages_of_type("agent_command")
        assert len(msgs) == 1
        assert msgs[0]["event"] == "command_sent"
        assert msgs[0]["from_session"] == "s-parent"
        assert msgs[0]["to_session"] == "s-child"
        assert msgs[0]["command_id"] == "cmd-1"
        assert "timestamp" in msgs[0]


class TestSubscriptionFiltering:
    """Test that agent_message and agent_command require explicit subscription."""

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
    async def test_agent_command_requires_subscription(self) -> None:
        """Client without agent_command subscription does not receive it."""
        from gobby.servers.websocket.broadcast import BroadcastMixin

        ws_no_sub = MockWebSocket()
        ws_no_sub.subscriptions = {"hook_event"}

        ws_with_sub = MockWebSocket()
        ws_with_sub.subscriptions = {"agent_command"}

        mixin = BroadcastMixin()
        mixin.clients = {ws_no_sub: {"id": "1"}, ws_with_sub: {"id": "2"}}

        await mixin.broadcast_agent_command(
            event="command_sent",
            from_session="s-parent",
            to_session="s-child",
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
        await mixin.broadcast_agent_command(
            event="command_sent",
            from_session="a",
            to_session="b",
        )

        assert len(ws.sent_messages) == 2


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
            {"from_session": "s-from", "to_session": "s-to", "content": "hello"},
        )

        assert result["success"] is True
        mock_broadcast_fn.assert_called_once()
        call_kwargs = mock_broadcast_fn.call_args[1]
        assert call_kwargs["msg_type"] == "agent_message"
        assert call_kwargs["event"] == "message_sent"
        assert call_kwargs["from_session"] == "s-from"
        assert call_kwargs["to_session"] == "s-to"

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
            {"from_session": "no-such", "to_session": "s-to", "content": "hi"},
        )

        assert result["success"] is False
        mock_broadcast_fn.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# send_command broadcasts agent_command event
# ═══════════════════════════════════════════════════════════════════════


class TestSendCommandBroadcast:
    """send_command calls broadcast_fn with agent_command event on success."""

    @pytest.mark.asyncio
    async def test_broadcast_on_success(
        self,
        messaging_registry_with_broadcast,
        mock_session_manager,
        mock_command_manager,
        mock_broadcast_fn,
    ) -> None:
        """Successful send_command triggers agent_command broadcast."""
        mock_session_manager.is_ancestor.return_value = True
        mock_command_manager.list_commands.return_value = []

        result = await messaging_registry_with_broadcast.call(
            "send_command",
            {
                "from_session": "s-parent",
                "to_session": "s-child",
                "command_text": "Run tests",
            },
        )

        assert result["success"] is True
        mock_broadcast_fn.assert_called_once()
        call_kwargs = mock_broadcast_fn.call_args[1]
        assert call_kwargs["msg_type"] == "agent_command"
        assert call_kwargs["event"] == "command_sent"
        assert call_kwargs["from_session"] == "s-parent"
        assert call_kwargs["to_session"] == "s-child"

    @pytest.mark.asyncio
    async def test_no_broadcast_on_failure(
        self,
        messaging_registry_with_broadcast,
        mock_session_manager,
        mock_broadcast_fn,
    ) -> None:
        """Failed send_command does not broadcast."""
        mock_session_manager.is_ancestor.return_value = False

        result = await messaging_registry_with_broadcast.call(
            "send_command",
            {
                "from_session": "s-unrelated",
                "to_session": "s-child",
                "command_text": "Run tests",
            },
        )

        assert result["success"] is False
        mock_broadcast_fn.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# complete_command broadcasts agent_command event
# ═══════════════════════════════════════════════════════════════════════


class TestCompleteCommandBroadcast:
    """complete_command calls broadcast_fn with agent_command event on success."""

    @pytest.mark.asyncio
    async def test_broadcast_on_success(
        self,
        messaging_registry_with_broadcast,
        mock_command_manager,
        mock_broadcast_fn,
    ) -> None:
        """Successful complete_command triggers agent_command broadcast."""
        mock_command_manager.get_command.return_value = MockCommand(
            id="cmd-1",
            from_session="s-parent",
            to_session="s-child",
            status="running",
        )

        result = await messaging_registry_with_broadcast.call(
            "complete_command",
            {"session_id": "s-child", "command_id": "cmd-1", "result": "Done"},
        )

        assert result["success"] is True
        mock_broadcast_fn.assert_called_once()
        call_kwargs = mock_broadcast_fn.call_args[1]
        assert call_kwargs["msg_type"] == "agent_command"
        assert call_kwargs["event"] == "command_completed"
        assert call_kwargs["command_id"] == "cmd-1"

    @pytest.mark.asyncio
    async def test_no_broadcast_on_failure(
        self,
        messaging_registry_with_broadcast,
        mock_command_manager,
        mock_broadcast_fn,
    ) -> None:
        """Failed complete_command does not broadcast."""
        mock_command_manager.get_command.return_value = None

        result = await messaging_registry_with_broadcast.call(
            "complete_command",
            {"session_id": "s-child", "command_id": "no-such", "result": "Done"},
        )

        assert result["success"] is False
        mock_broadcast_fn.assert_not_called()

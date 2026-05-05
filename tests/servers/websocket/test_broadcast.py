"""Tests for WebSocket broadcast mixin.

Tests broadcast edge cases, subscription filtering, and event methods.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from websockets.exceptions import ConnectionClosed

from gobby.servers.websocket.broadcast import BroadcastMixin

pytestmark = pytest.mark.unit


class FakeBroadcaster(BroadcastMixin):
    """Concrete class using BroadcastMixin for testing."""

    def __init__(self) -> None:
        self.clients: dict[Any, dict[str, Any]] = {}


def _make_ws(subscriptions: set[str] | None = None) -> AsyncMock:
    """Create a mock WebSocket with optional subscriptions."""
    ws = AsyncMock()
    ws.subscriptions = subscriptions
    ws.send = AsyncMock()
    return ws


# ═══════════════════════════════════════════════════════════════════════
# _is_subscribed
# ═══════════════════════════════════════════════════════════════════════


class TestIsSubscribed:
    """Tests for _is_subscribed filtering logic."""

    def test_no_subscriptions_returns_false(self) -> None:
        b = FakeBroadcaster()
        ws = MagicMock()
        ws.subscriptions = None
        assert b._is_subscribed(ws, {"type": "task_event"}) is False

    def test_wildcard_subscription_returns_true(self) -> None:
        b = FakeBroadcaster()
        ws = MagicMock()
        ws.subscriptions = {"*"}
        assert b._is_subscribed(ws, {"type": "session_event"}) is True

    def test_non_event_type_passes_for_any_subscriber(self) -> None:
        b = FakeBroadcaster()
        ws = MagicMock()
        ws.subscriptions = {"some_sub"}
        # "task_event" is NOT in the event_types set, so it passes through
        assert b._is_subscribed(ws, {"type": "task_event"}) is True

    def test_event_type_requires_explicit_subscription(self) -> None:
        b = FakeBroadcaster()
        ws = MagicMock()
        ws.subscriptions = {"other_type"}
        # session_event IS in the event_types set, requires explicit sub
        assert b._is_subscribed(ws, {"type": "session_event"}) is False

    def test_event_type_with_matching_subscription(self) -> None:
        b = FakeBroadcaster()
        ws = MagicMock()
        ws.subscriptions = {"session_event"}
        assert b._is_subscribed(ws, {"type": "session_event"}) is True

    def test_parametric_subscription_matches(self) -> None:
        b = FakeBroadcaster()
        ws = MagicMock()
        ws.subscriptions = {"session_message:session_id=abc123"}
        msg = {"type": "session_message", "session_id": "abc123"}
        assert b._is_subscribed(ws, msg) is True

    def test_parametric_subscription_no_match(self) -> None:
        b = FakeBroadcaster()
        ws = MagicMock()
        ws.subscriptions = {"session_message:session_id=abc123"}
        msg = {"type": "session_message", "session_id": "xyz789"}
        assert b._is_subscribed(ws, msg) is False

    def test_parametric_subscription_wrong_type(self) -> None:
        b = FakeBroadcaster()
        ws = MagicMock()
        ws.subscriptions = {"agent_event:run_id=r1"}
        msg = {"type": "session_event", "run_id": "r1"}
        assert b._is_subscribed(ws, msg) is False

    def test_hook_event_granularity_by_event_type(self) -> None:
        b = FakeBroadcaster()
        ws = MagicMock()
        ws.subscriptions = {"before_tool"}
        msg = {"type": "hook_event", "event_type": "before_tool"}
        assert b._is_subscribed(ws, msg) is True

    def test_hook_event_no_match(self) -> None:
        b = FakeBroadcaster()
        ws = MagicMock()
        ws.subscriptions = {"after_tool"}
        msg = {"type": "hook_event", "event_type": "before_tool"}
        assert b._is_subscribed(ws, msg) is False

    def test_parametric_subscription_without_equals_skipped(self) -> None:
        b = FakeBroadcaster()
        ws = MagicMock()
        ws.subscriptions = {"session_event:noequalssign"}
        msg = {"type": "session_event", "session_id": "abc"}
        assert b._is_subscribed(ws, msg) is False


# ═══════════════════════════════════════════════════════════════════════
# broadcast
# ═══════════════════════════════════════════════════════════════════════


class TestBroadcast:
    """Tests for the broadcast method."""

    @pytest.mark.asyncio
    async def test_broadcast_empty_clients(self) -> None:
        b = FakeBroadcaster()
        result = await b.broadcast({"type": "test"})
        assert result is None
        assert b.clients == {}

    @pytest.mark.asyncio
    async def test_broadcast_sends_to_subscribed_clients(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"*"})
        b.clients[ws] = {}

        await b.broadcast({"type": "test", "data": "hello"})
        ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_skips_unsubscribed_clients(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"session_event"})
        b.clients[ws] = {}

        await b.broadcast({"type": "agent_event", "data": "hello"})
        ws.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_broadcast_handles_connection_closed(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"*"})
        ws.send.side_effect = ConnectionClosed(None, None)
        b.clients[ws] = {}

        result = await b.broadcast({"type": "test"})
        assert result is None
        assert ws in b.clients

    @pytest.mark.asyncio
    async def test_broadcast_handles_generic_exception(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"*"})
        ws.send.side_effect = RuntimeError("send failed")
        b.clients[ws] = {}

        result = await b.broadcast({"type": "test"})
        assert result is None
        assert ws in b.clients

    @pytest.mark.asyncio
    async def test_broadcast_multiple_clients(self) -> None:
        b = FakeBroadcaster()
        ws1 = _make_ws(subscriptions={"*"})
        ws2 = _make_ws(subscriptions={"*"})
        ws3 = _make_ws(subscriptions=None)  # Not subscribed
        b.clients[ws1] = {}
        b.clients[ws2] = {}
        b.clients[ws3] = {}

        await b.broadcast({"type": "test"})
        ws1.send.assert_called_once()
        ws2.send.assert_called_once()
        ws3.send.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# broadcast_* convenience methods
# ═══════════════════════════════════════════════════════════════════════


class TestBroadcastEventMethods:
    """Tests for typed broadcast methods."""

    @pytest.mark.asyncio
    async def test_broadcast_session_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"session_event"})
        b.clients[ws] = {}

        await b.broadcast_session_event("created", "sess-123", title="Test")
        ws.send.assert_called_once()
        import json

        msg = json.loads(ws.send.call_args[0][0])
        assert msg["type"] == "session_event"
        assert msg["event"] == "created"
        assert msg["session_id"] == "sess-123"
        assert msg["title"] == "Test"
        assert "timestamp" in msg

    @pytest.mark.asyncio
    async def test_broadcast_pipeline_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"pipeline_event"})
        b.clients[ws] = {}

        await b.broadcast_pipeline_event("step_completed", "pe-123", step_id="s1")
        ws.send.assert_called_once()
        import json

        msg = json.loads(ws.send.call_args[0][0])
        assert msg["type"] == "pipeline_event"
        assert msg["execution_id"] == "pe-123"

    @pytest.mark.asyncio
    async def test_broadcast_agent_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"agent_event"})
        b.clients[ws] = {}

        await b.broadcast_agent_event("spawned", "run-1", "parent-1")
        ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_terminal_output(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"terminal_output"})
        b.clients[ws] = {}

        await b.broadcast_terminal_output("run-1", "hello world")
        ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_tmux_session_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"tmux_session_event"})
        b.clients[ws] = {}

        await b.broadcast_tmux_session_event("created", "my-session", "gobby")
        ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_agent_message(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"agent_message"})
        b.clients[ws] = {}

        await b.broadcast_agent_message("message_sent", "from-1", "to-2", content="hi")
        ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_agent_command(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"agent_command"})
        b.clients[ws] = {}

        await b.broadcast_agent_command("command_sent", "from-1", "to-2")
        ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_trace_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"trace_event"})
        b.clients[ws] = {}

        await b.broadcast_trace_event({"trace_id": "t-1", "name": "test"})
        ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_skill_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"skill_event"})
        b.clients[ws] = {}

        await b.broadcast_skill_event("created", "skill-1")
        ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_mcp_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"mcp_event"})
        b.clients[ws] = {}

        await b.broadcast_mcp_event("added", "my-server")
        ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_workflow_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"workflow_event"})
        b.clients[ws] = {}

        await b.broadcast_workflow_event("updated", "def-1")
        ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_project_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"project_event"})
        b.clients[ws] = {}

        await b.broadcast_project_event("updated", "proj-1")
        ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_cron_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"cron_event"})
        b.clients[ws] = {}

        await b.broadcast_cron_event("created", "job-1")
        ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_worktree_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"worktree_event"})
        b.clients[ws] = {}

        await b.broadcast_worktree_event("created", "wt-1")
        ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_autonomous_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"autonomous_event"})
        b.clients[ws] = {}

        await b.broadcast_autonomous_event("started", "sess-1")
        ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_canvas_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"canvas_event"})
        b.clients[ws] = {}

        await b.broadcast_canvas_event("updated", "canvas-1", "conv-1")
        ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_artifact_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"*"})
        b.clients[ws] = {}

        await b.broadcast_artifact_event("created", "conv-1", artifact_id="a-1")
        ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_task_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"*"})
        b.clients[ws] = {}

        await b.broadcast_task_event("created", "task-1", title="New task")
        ws.send.assert_called_once()

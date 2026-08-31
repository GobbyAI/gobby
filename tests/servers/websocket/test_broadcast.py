"""Tests for WebSocket broadcast mixin.

Tests broadcast edge cases, subscription filtering, and event methods.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from websockets.exceptions import ConnectionClosed

from gobby.servers.websocket import broadcast as broadcast_module
from gobby.servers.websocket.broadcast import BroadcastMixin

pytestmark = pytest.mark.unit


class FakeBroadcaster(BroadcastMixin):
    """Concrete class using BroadcastMixin for testing."""

    def __init__(self) -> None:
        self.clients: dict[Any, dict[str, Any]] = {}


class FakeWebSocket:
    """Minimal websocket fake that records serialized broadcast payloads."""

    def __init__(
        self,
        subscriptions: set[str] | None = None,
        *,
        send_error: Exception | None = None,
        send_started: asyncio.Event | None = None,
        send_release: asyncio.Event | None = None,
        close_started: asyncio.Event | None = None,
        close_release: asyncio.Event | None = None,
    ) -> None:
        self.subscriptions = subscriptions
        self.send_error = send_error
        self.send_started = send_started
        self.send_release = send_release
        self.close_started = close_started
        self.close_release = close_release
        self.sent: list[str] = []
        self.closed: tuple[int, str] | None = None

    async def send(self, message: str) -> None:
        if self.send_started is not None:
            self.send_started.set()
        if self.send_release is not None:
            await self.send_release.wait()
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(message)

    async def close(self, *, code: int, reason: str) -> None:
        if self.close_started is not None:
            self.close_started.set()
        if self.close_release is not None:
            await self.close_release.wait()
        self.closed = (code, reason)


def _make_ws(subscriptions: set[str] | None = None) -> FakeWebSocket:
    """Create a fake WebSocket with optional subscriptions."""
    return FakeWebSocket(subscriptions=subscriptions)


def _sent_message(ws: FakeWebSocket) -> dict[str, Any]:
    assert len(ws.sent) == 1
    return json.loads(ws.sent[0])


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
    async def test_broadcast_ignores_nonserializable_payload(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"*"})
        b.clients[ws] = {}

        await b.broadcast({"type": "test", "data": object()})

        assert ws.sent == []

    @pytest.mark.asyncio
    async def test_broadcast_sends_to_subscribed_clients(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"*"})
        b.clients[ws] = {}

        await b.broadcast(
            {"type": "test", "data": "hello", "created_at": datetime(2026, 7, 3, tzinfo=UTC)}
        )
        assert _sent_message(ws) == {
            "type": "test",
            "data": "hello",
            "created_at": "2026-07-03T00:00:00+00:00",
        }

    @pytest.mark.asyncio
    async def test_broadcast_skips_unsubscribed_clients(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"session_event"})
        b.clients[ws] = {}

        await b.broadcast({"type": "agent_event", "data": "hello"})
        assert ws.sent == []

    @pytest.mark.asyncio
    async def test_broadcast_handles_connection_closed(self) -> None:
        b = FakeBroadcaster()
        ws = FakeWebSocket(subscriptions={"*"}, send_error=ConnectionClosed(None, None))
        b.clients[ws] = {}

        result = await b.broadcast({"type": "test"})
        assert result is None
        assert ws not in b.clients

    @pytest.mark.asyncio
    async def test_broadcast_handles_generic_exception(self) -> None:
        b = FakeBroadcaster()
        ws = FakeWebSocket(subscriptions={"*"}, send_error=RuntimeError("send failed"))
        b.clients[ws] = {}

        result = await b.broadcast({"type": "test"})
        assert result is None
        assert ws not in b.clients

    async def test_broadcast_removes_client_when_subscription_check_fails(self) -> None:
        failed = FakeWebSocket(subscriptions={"*"})
        healthy = FakeWebSocket(subscriptions={"*"})

        class SubscriptionFailureBroadcaster(FakeBroadcaster):
            def _is_subscribed(self, websocket: Any, message: dict[str, Any]) -> bool:
                if websocket is failed:
                    raise RuntimeError("subscription check failed")
                return super()._is_subscribed(websocket, message)

        b = SubscriptionFailureBroadcaster()
        b.clients = {failed: {}, healthy: {}}

        await b.broadcast({"type": "test"})

        assert failed not in b.clients
        assert _sent_message(healthy) == {"type": "test"}

    async def test_broadcast_sends_concurrently(self) -> None:
        barrier = asyncio.Barrier(2)

        class BarrierWebSocket(FakeWebSocket):
            async def send(self, message: str) -> None:
                await barrier.wait()
                await super().send(message)

        b = FakeBroadcaster()
        clients = [BarrierWebSocket(subscriptions={"*"}) for _ in range(2)]
        b.clients = {client: {} for client in clients}

        await asyncio.wait_for(b.broadcast({"type": "test"}), timeout=0.5)

        assert all(client.sent for client in clients)

    async def test_broadcast_times_out_and_removes_stalled_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        never = asyncio.Event()

        class StalledWebSocket(FakeWebSocket):
            async def send(self, message: str) -> None:
                await never.wait()

        stalled = StalledWebSocket(subscriptions={"*"})
        healthy = FakeWebSocket(subscriptions={"*"})
        b = FakeBroadcaster()
        b.clients = {stalled: {}, healthy: {}}
        monkeypatch.setattr(broadcast_module, "BROADCAST_SEND_TIMEOUT_SECONDS", 0.01)

        await b.broadcast({"type": "test"})

        assert healthy.sent
        assert stalled not in b.clients

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
        assert _sent_message(ws1) == {"type": "test"}
        assert _sent_message(ws2) == {"type": "test"}
        assert ws3.sent == []

    async def test_stalled_client_does_not_delay_healthy_recipient(self) -> None:
        b = FakeBroadcaster()
        stalled_send_started = asyncio.Event()
        stalled_send_release = asyncio.Event()
        stalled = FakeWebSocket(
            subscriptions={"*"},
            send_started=stalled_send_started,
            send_release=stalled_send_release,
        )
        healthy_send_started = asyncio.Event()
        healthy = FakeWebSocket(subscriptions={"*"}, send_started=healthy_send_started)
        b.clients[stalled] = {}
        b.clients[healthy] = {}

        broadcast_task = asyncio.create_task(b.broadcast({"type": "test"}))
        await asyncio.wait_for(stalled_send_started.wait(), timeout=0.1)
        await asyncio.wait_for(healthy_send_started.wait(), timeout=0.1)

        assert _sent_message(healthy) == {"type": "test"}
        assert not broadcast_task.done()

        stalled_send_release.set()
        await broadcast_task

    async def test_timed_out_client_is_removed_and_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(broadcast_module, "BROADCAST_SEND_TIMEOUT_SECONDS", 0.01)
        b = FakeBroadcaster()
        stalled = FakeWebSocket(subscriptions={"*"}, send_release=asyncio.Event())
        b.clients[stalled] = {}

        await b.broadcast({"type": "test"})

        assert stalled not in b.clients
        assert stalled.closed == (1011, "Broadcast send timed out")

    async def test_timed_out_client_close_is_also_bounded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(broadcast_module, "BROADCAST_SEND_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(broadcast_module, "BROADCAST_CLOSE_TIMEOUT_SECONDS", 0.01)
        close_started = asyncio.Event()
        stalled = FakeWebSocket(
            subscriptions={"*"},
            send_release=asyncio.Event(),
            close_started=close_started,
            close_release=asyncio.Event(),
        )
        b = FakeBroadcaster()
        b.clients[stalled] = {}

        await b.broadcast({"type": "test"})

        assert close_started.is_set()
        assert stalled not in b.clients
        assert stalled.closed is None


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
        msg = _sent_message(ws)
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
        msg = _sent_message(ws)
        assert msg["type"] == "pipeline_event"
        assert msg["execution_id"] == "pe-123"
        assert msg["step_id"] == "s1"

    @pytest.mark.asyncio
    async def test_broadcast_agent_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"agent_event"})
        b.clients[ws] = {}

        await b.broadcast_agent_event("spawned", "run-1", "parent-1")
        msg = _sent_message(ws)
        assert msg["type"] == "agent_event"
        assert msg["event"] == "spawned"
        assert msg["run_id"] == "run-1"
        assert msg["parent_session_id"] == "parent-1"

    @pytest.mark.asyncio
    async def test_broadcast_terminal_output(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"terminal_output"})
        b.clients[ws] = {}

        await b.broadcast_terminal_output("term-1", "hello world")
        msg = _sent_message(ws)
        assert msg["type"] == "terminal_output"
        assert msg["terminal_id"] == "term-1"
        assert msg["attachment_id"] is None
        assert msg["data"] == "hello world"

    @pytest.mark.asyncio
    async def test_broadcast_terminal_output_includes_attachment_id(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"terminal_output"})
        b.clients[ws] = {}

        await b.broadcast_terminal_output("term-1", "hello world", "att-1")
        msg = _sent_message(ws)
        assert msg["type"] == "terminal_output"
        assert msg["terminal_id"] == "term-1"
        assert msg["attachment_id"] == "att-1"
        assert msg["data"] == "hello world"

    @pytest.mark.asyncio
    async def test_broadcast_terminal_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"terminal_event"})
        b.clients[ws] = {}

        await b.broadcast_tmux_session_event("created", terminal_id="term-1")
        msg = _sent_message(ws)
        assert msg["type"] == "terminal_event"
        assert msg["event"] == "created"
        assert msg["terminal_id"] == "term-1"

    @pytest.mark.asyncio
    async def test_broadcast_agent_message(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"agent_message"})
        b.clients[ws] = {}

        await b.broadcast_agent_message("message_sent", "from-1", "to-2", content="hi")
        msg = _sent_message(ws)
        assert msg["type"] == "agent_message"
        assert msg["event"] == "message_sent"
        assert msg["from_session"] == "from-1"
        assert msg["to_session"] == "to-2"
        assert msg["content"] == "hi"

    @pytest.mark.asyncio
    async def test_broadcast_trace_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"trace_event"})
        b.clients[ws] = {}

        await b.broadcast_trace_event({"trace_id": "t-1", "name": "test"})
        msg = _sent_message(ws)
        assert msg["type"] == "trace_event"
        assert msg["trace_id"] == "t-1"
        assert msg["span"] == {"trace_id": "t-1", "name": "test"}

    @pytest.mark.asyncio
    async def test_broadcast_skill_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"skill_event"})
        b.clients[ws] = {}

        await b.broadcast_skill_event("created", "skill-1")
        msg = _sent_message(ws)
        assert msg["type"] == "skill_event"
        assert msg["event"] == "created"
        assert msg["skill_id"] == "skill-1"

    @pytest.mark.asyncio
    async def test_broadcast_mcp_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"mcp_event"})
        b.clients[ws] = {}

        await b.broadcast_mcp_event("added", "my-server")
        msg = _sent_message(ws)
        assert msg["type"] == "mcp_event"
        assert msg["event"] == "added"
        assert msg["server_name"] == "my-server"

    @pytest.mark.asyncio
    async def test_broadcast_workflow_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"workflow_event"})
        b.clients[ws] = {}

        await b.broadcast_workflow_event("updated", "def-1")
        msg = _sent_message(ws)
        assert msg["type"] == "workflow_event"
        assert msg["event"] == "updated"
        assert msg["definition_id"] == "def-1"

    @pytest.mark.asyncio
    async def test_broadcast_project_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"project_event"})
        b.clients[ws] = {}

        await b.broadcast_project_event("updated", "proj-1")
        msg = _sent_message(ws)
        assert msg["type"] == "project_event"
        assert msg["event"] == "updated"
        assert msg["project_id"] == "proj-1"

    @pytest.mark.asyncio
    async def test_broadcast_cron_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"cron_event"})
        b.clients[ws] = {}

        await b.broadcast_cron_event("created", "job-1")
        msg = _sent_message(ws)
        assert msg["type"] == "cron_event"
        assert msg["event"] == "created"
        assert msg["job_id"] == "job-1"

    @pytest.mark.asyncio
    async def test_broadcast_worktree_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"worktree_event"})
        b.clients[ws] = {}

        await b.broadcast_worktree_event("created", "wt-1")
        msg = _sent_message(ws)
        assert msg["type"] == "worktree_event"
        assert msg["event"] == "created"
        assert msg["worktree_id"] == "wt-1"

    @pytest.mark.asyncio
    async def test_broadcast_autonomous_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"autonomous_event"})
        b.clients[ws] = {}

        await b.broadcast_autonomous_event("started", "sess-1")
        msg = _sent_message(ws)
        assert msg["type"] == "autonomous_event"
        assert msg["event"] == "started"
        assert msg["session_id"] == "sess-1"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_broadcast_task_event(self) -> None:
        b = FakeBroadcaster()
        ws = _make_ws(subscriptions={"*"})
        b.clients[ws] = {}

        await b.broadcast_task_event("created", "task-1", title="New task")
        msg = _sent_message(ws)
        assert msg["type"] == "task_event"
        assert msg["event"] == "created"
        assert msg["task_id"] == "task-1"
        assert msg["title"] == "New task"

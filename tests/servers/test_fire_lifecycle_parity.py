"""Tests for _fire_lifecycle CLI parity features (D1, D2, D6).

Covers:
- D1: Blocking webhook evaluation in _fire_lifecycle
- D2: Event broadcasting via HookEventBroadcaster
- D6: Inter-session message piggyback delivery
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse
from gobby.servers.websocket.chat import ChatMixin

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class ChatMixinHost(ChatMixin):
    """Minimal host providing attributes ChatMixin expects."""

    def __init__(self) -> None:
        self.clients: dict[Any, Any] = {}
        self._chat_sessions: dict[str, Any] = {}
        self._active_chat_tasks: dict[str, Any] = {}
        self.workflow_handler: Any = None
        self.event_handlers: Any = None
        self.webhook_dispatcher: Any = None
        self.hook_broadcaster: Any = None
        self.inter_session_msg_manager: Any = None
        self.mcp_manager: Any = None
        self.internal_manager: Any = None

    async def _send_error(
        self,
        websocket: object,
        message: str,
        request_id: str | None = None,
        code: str = "ERROR",
    ) -> None:
        pass


@pytest.fixture
def host() -> ChatMixinHost:
    return ChatMixinHost()


def _make_session(db_session_id: str = "sess-123", seq_num: int = 42) -> MagicMock:
    session = MagicMock()
    session.db_session_id = db_session_id
    session.seq_num = seq_num
    session.project_path = "/tmp/project"
    return session


def _make_workflow_handler(
    decision: str = "allow", context: str | None = None
) -> MagicMock:
    handler = MagicMock()
    response = HookResponse(decision=decision, context=context)
    handler.evaluate.return_value = response
    return handler


# ---------------------------------------------------------------------------
# D1: Blocking Webhooks
# ---------------------------------------------------------------------------


class TestFireLifecycleBlockingWebhooks:
    """D1: _fire_lifecycle should evaluate blocking webhooks."""

    @pytest.mark.asyncio
    async def test_webhook_blocks_event(self, host: ChatMixinHost) -> None:
        """When a blocking webhook returns 'block', _fire_lifecycle returns block."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        # Setup webhook dispatcher that blocks
        endpoint = MagicMock()
        endpoint.enabled = True
        endpoint.can_block = True

        dispatcher = MagicMock()
        dispatcher.config.enabled = True
        dispatcher.config.endpoints = [endpoint]
        dispatcher._matches_event.return_value = True
        dispatcher._build_payload.return_value = {"event_type": "before_tool"}
        dispatcher._dispatch_single = AsyncMock(
            return_value=MagicMock(decision="block", response_body={"reason": "Policy violation"})
        )
        dispatcher.get_blocking_decision.return_value = ("block", "Policy violation")
        host.webhook_dispatcher = dispatcher

        result = await host._fire_lifecycle("conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"})

        assert result is not None
        assert result["decision"] == "block"
        assert result["reason"] == "Policy violation"

    @pytest.mark.asyncio
    async def test_webhook_allows_event(self, host: ChatMixinHost) -> None:
        """When blocking webhooks allow, processing continues normally."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        dispatcher = MagicMock()
        dispatcher.config.enabled = True
        dispatcher.config.endpoints = []  # No matching endpoints
        host.webhook_dispatcher = dispatcher

        result = await host._fire_lifecycle("conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"})

        assert result is not None
        assert result["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_webhook_error_fails_open(self, host: ChatMixinHost) -> None:
        """Webhook evaluation errors should fail open (allow)."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        dispatcher = MagicMock()
        dispatcher.config.enabled = True
        dispatcher.config.endpoints = [MagicMock(enabled=True, can_block=True)]
        dispatcher._matches_event.side_effect = RuntimeError("Webhook crash")
        host.webhook_dispatcher = dispatcher

        result = await host._fire_lifecycle("conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"})

        assert result is not None
        assert result["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_no_webhook_dispatcher_proceeds(self, host: ChatMixinHost) -> None:
        """Without a webhook dispatcher, processing continues normally."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        result = await host._fire_lifecycle("conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"})

        assert result is not None
        assert result["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_webhook_disabled_config_proceeds(self, host: ChatMixinHost) -> None:
        """When webhooks are disabled in config, processing continues normally."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        dispatcher = MagicMock()
        dispatcher.config.enabled = False
        host.webhook_dispatcher = dispatcher

        result = await host._fire_lifecycle("conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"})

        assert result is not None
        assert result["decision"] == "allow"


# ---------------------------------------------------------------------------
# D2: Event Broadcasting
# ---------------------------------------------------------------------------


class TestFireLifecycleBroadcasting:
    """D2: _fire_lifecycle should broadcast events for audit trail."""

    @pytest.mark.asyncio
    async def test_broadcasts_event_after_processing(self, host: ChatMixinHost) -> None:
        """_fire_lifecycle should call hook_broadcaster.broadcast_event."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        broadcaster = MagicMock()
        broadcaster.broadcast_event = AsyncMock()
        host.hook_broadcaster = broadcaster

        result = await host._fire_lifecycle("conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"})

        assert result is not None
        assert result["decision"] == "allow"
        broadcaster.broadcast_event.assert_awaited_once()

        # Verify event and response are passed
        call_args = broadcaster.broadcast_event.call_args
        event_arg = call_args[0][0]
        response_arg = call_args[0][1]
        assert isinstance(event_arg, HookEvent)
        assert event_arg.event_type == HookEventType.BEFORE_TOOL
        assert isinstance(response_arg, HookResponse)

    @pytest.mark.asyncio
    async def test_broadcast_error_does_not_crash(self, host: ChatMixinHost) -> None:
        """Broadcast errors should be swallowed (not crash lifecycle)."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        broadcaster = MagicMock()
        broadcaster.broadcast_event = AsyncMock(side_effect=RuntimeError("Broadcast failed"))
        host.hook_broadcaster = broadcaster

        result = await host._fire_lifecycle("conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"})

        # Should still return a valid result despite broadcast error
        assert result is not None
        assert result["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_no_broadcaster_proceeds(self, host: ChatMixinHost) -> None:
        """Without a broadcaster, processing continues normally."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        result = await host._fire_lifecycle("conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"})

        assert result is not None
        assert result["decision"] == "allow"


# ---------------------------------------------------------------------------
# D6: Inter-session Message Piggyback
# ---------------------------------------------------------------------------


class TestFireLifecycleMessagePiggyback:
    """D6: _fire_lifecycle should inject pending inter-session messages."""

    @pytest.mark.asyncio
    async def test_piggyback_on_before_tool(self, host: ChatMixinHost) -> None:
        """Pending messages should be injected into context on BEFORE_TOOL."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        msg = MagicMock()
        msg.content = "Agent A completed subtask #42"
        msg.id = "msg-1"
        msg.message_type = "message"
        msg.from_session = "aaaa1111-0000-0000-0000-000000000000"
        msg.priority = "normal"

        mgr = MagicMock()
        mgr.get_undelivered_messages.return_value = [msg]
        host.inter_session_msg_manager = mgr

        result = await host._fire_lifecycle("conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"})

        assert result is not None
        assert "[Pending P2P messages from other sessions]:" in result["context"]
        assert "Agent A completed subtask #42" in result["context"]
        mgr.mark_delivered.assert_called_once_with("msg-1")

    @pytest.mark.asyncio
    async def test_piggyback_on_after_tool(self, host: ChatMixinHost) -> None:
        """Pending messages should be injected on AFTER_TOOL too."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        msg = MagicMock()
        msg.content = "Task completed"
        msg.id = "msg-2"
        msg.message_type = "message"
        msg.from_session = "bbbb2222-0000-0000-0000-000000000000"
        msg.priority = "normal"

        mgr = MagicMock()
        mgr.get_undelivered_messages.return_value = [msg]
        host.inter_session_msg_manager = mgr

        result = await host._fire_lifecycle("conv-1", HookEventType.AFTER_TOOL, {"tool_name": "bash"})

        assert result is not None
        assert "Task completed" in result["context"]

    @pytest.mark.asyncio
    async def test_no_piggyback_on_session_start(self, host: ChatMixinHost) -> None:
        """Message piggyback should NOT run on SESSION_START events."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        mgr = MagicMock()
        host.inter_session_msg_manager = mgr

        result = await host._fire_lifecycle("conv-1", HookEventType.SESSION_START, {})

        assert result is not None
        mgr.get_undelivered_messages.assert_not_called()

    @pytest.mark.asyncio
    async def test_piggyback_on_before_agent(self, host: ChatMixinHost) -> None:
        """Message piggyback SHOULD run on BEFORE_AGENT — ensures messages
        arrive at agent turn start even before any tool calls."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        msg = MagicMock()
        msg.content = "Urgent update from coordinator"
        msg.id = "msg-agent-1"
        msg.message_type = "message"
        msg.from_session = "cccc3333-0000-0000-0000-000000000000"
        msg.priority = "normal"

        mgr = MagicMock()
        mgr.get_undelivered_messages.return_value = [msg]
        host.inter_session_msg_manager = mgr

        result = await host._fire_lifecycle("conv-1", HookEventType.BEFORE_AGENT, {"prompt": "hi"})

        assert result is not None
        assert "[Pending P2P messages from other sessions]:" in result["context"]
        assert "Urgent update from coordinator" in result["context"]
        mgr.mark_delivered.assert_called_once_with("msg-agent-1")

    @pytest.mark.asyncio
    async def test_no_pending_messages(self, host: ChatMixinHost) -> None:
        """When there are no pending messages, context is unchanged."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler(context="rule context")

        mgr = MagicMock()
        mgr.get_undelivered_messages.return_value = []
        host.inter_session_msg_manager = mgr

        result = await host._fire_lifecycle("conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"})

        assert result is not None
        assert "Pending P2P messages" not in (result["context"] or "")
        assert "Pending messages from web chat" not in (result["context"] or "")

    @pytest.mark.asyncio
    async def test_piggyback_merges_with_existing_context(self, host: ChatMixinHost) -> None:
        """Pending messages should merge with existing rule/handler context."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler(context="Rule context here")

        msg = MagicMock()
        msg.content = "Agent message"
        msg.id = "msg-3"
        msg.message_type = "message"
        msg.from_session = "dddd4444-0000-0000-0000-000000000000"
        msg.priority = "normal"

        mgr = MagicMock()
        mgr.get_undelivered_messages.return_value = [msg]
        host.inter_session_msg_manager = mgr

        result = await host._fire_lifecycle("conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"})

        assert result is not None
        # Should contain both rule context and message context
        assert "Rule context here" in result["context"]
        assert "Agent message" in result["context"]

    @pytest.mark.asyncio
    async def test_piggyback_error_fails_gracefully(self, host: ChatMixinHost) -> None:
        """Inter-session message errors should not crash lifecycle."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        mgr = MagicMock()
        mgr.get_undelivered_messages.side_effect = RuntimeError("DB error")
        host.inter_session_msg_manager = mgr

        result = await host._fire_lifecycle("conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"})

        assert result is not None
        assert result["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_no_manager_proceeds(self, host: ChatMixinHost) -> None:
        """Without inter_session_msg_manager, processing continues normally."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        result = await host._fire_lifecycle("conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"})

        assert result is not None
        assert result["decision"] == "allow"


# ---------------------------------------------------------------------------
# Integration: All three features together
# ---------------------------------------------------------------------------


class TestFireLifecycleFullParity:
    """Test all parity features working together."""

    @pytest.mark.asyncio
    async def test_all_features_work_together(self, host: ChatMixinHost) -> None:
        """Webhook check + message piggyback + broadcasting all fire."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler(context="workflow ctx")

        # Webhook dispatcher (allows)
        dispatcher = MagicMock()
        dispatcher.config.enabled = True
        dispatcher.config.endpoints = []
        host.webhook_dispatcher = dispatcher

        # Broadcaster
        broadcaster = MagicMock()
        broadcaster.broadcast_event = AsyncMock()
        host.hook_broadcaster = broadcaster

        # Inter-session messages
        msg = MagicMock()
        msg.content = "From agent"
        msg.id = "msg-99"
        msg.message_type = "message"
        msg.from_session = "eeee5555-0000-0000-0000-000000000000"
        msg.priority = "normal"
        mgr = MagicMock()
        mgr.get_undelivered_messages.return_value = [msg]
        host.inter_session_msg_manager = mgr

        result = await host._fire_lifecycle("conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"})

        assert result is not None
        assert result["decision"] == "allow"
        assert "workflow ctx" in result["context"]
        assert "From agent" in result["context"]
        broadcaster.broadcast_event.assert_awaited_once()
        mgr.mark_delivered.assert_called_once_with("msg-99")

    @pytest.mark.asyncio
    async def test_webhook_block_stops_everything(self, host: ChatMixinHost) -> None:
        """When webhook blocks, broadcasting and piggyback should NOT run."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        # Blocking webhook
        endpoint = MagicMock(enabled=True, can_block=True)
        dispatcher = MagicMock()
        dispatcher.config.enabled = True
        dispatcher.config.endpoints = [endpoint]
        dispatcher._matches_event.return_value = True
        dispatcher._build_payload.return_value = {}
        dispatcher._dispatch_single = AsyncMock(return_value=MagicMock(decision="block"))
        dispatcher.get_blocking_decision.return_value = ("block", "Denied")
        host.webhook_dispatcher = dispatcher

        # These should NOT be called
        broadcaster = MagicMock()
        broadcaster.broadcast_event = AsyncMock()
        host.hook_broadcaster = broadcaster

        mgr = MagicMock()
        host.inter_session_msg_manager = mgr

        result = await host._fire_lifecycle("conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"})

        assert result is not None
        assert result["decision"] == "block"
        broadcaster.broadcast_event.assert_not_awaited()
        mgr.get_undelivered_messages.assert_not_called()

    @pytest.mark.asyncio
    async def test_workflow_block_stops_everything(self, host: ChatMixinHost) -> None:
        """When workflow blocks, webhooks, broadcasting, and piggyback should NOT run."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler(decision="block")

        # These should NOT be called
        dispatcher = MagicMock()
        host.webhook_dispatcher = dispatcher

        broadcaster = MagicMock()
        broadcaster.broadcast_event = AsyncMock()
        host.hook_broadcaster = broadcaster

        mgr = MagicMock()
        host.inter_session_msg_manager = mgr

        result = await host._fire_lifecycle("conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"})

        assert result is not None
        assert result["decision"] == "block"
        # Webhook dispatcher should never be touched
        dispatcher._matches_event.assert_not_called()
        broadcaster.broadcast_event.assert_not_awaited()
        mgr.get_undelivered_messages.assert_not_called()

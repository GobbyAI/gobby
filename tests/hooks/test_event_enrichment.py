"""Unit tests for EventEnricher piggyback message delivery.

Covers:
- BEFORE_AGENT piggyback delivery (the critical fix)
- SESSION_START exclusion
- P2P vs web_chat vs command_result grouping
- Sender label resolution with session storage
- Urgent priority tagging
- Fallback when session lookup fails
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from gobby.hooks.event_enrichment import _PIGGYBACK_EVENTS, EventEnricher
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(event_type: HookEventType, platform_session_id: str = "sess-abc") -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="ext-session-1",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={},
        metadata={"_platform_session_id": platform_session_id},
    )


def _make_msg(
    content: str = "hello",
    msg_id: str = "msg-1",
    message_type: str = "message",
    from_session: str = "from-1111-2222-3333-444444444444",
    priority: str = "normal",
) -> MagicMock:
    msg = MagicMock()
    msg.content = content
    msg.id = msg_id
    msg.message_type = message_type
    msg.from_session = from_session
    msg.priority = priority
    return msg


def _make_enricher(
    msgs: list | None = None,
    session_storage: MagicMock | None = None,
) -> EventEnricher:
    mgr = MagicMock()
    mgr.get_undelivered_messages.return_value = msgs or []
    return EventEnricher(
        session_storage=session_storage,
        injected_sessions=set(),
        inter_session_msg_manager=mgr,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPiggybackEventTypes:
    """Verify which event types trigger piggyback delivery."""

    def test_before_agent_in_piggyback_events(self) -> None:
        """BEFORE_AGENT must be in _PIGGYBACK_EVENTS."""
        assert HookEventType.BEFORE_AGENT in _PIGGYBACK_EVENTS

    def test_before_tool_in_piggyback_events(self) -> None:
        assert HookEventType.BEFORE_TOOL in _PIGGYBACK_EVENTS

    def test_after_tool_in_piggyback_events(self) -> None:
        assert HookEventType.AFTER_TOOL in _PIGGYBACK_EVENTS

    def test_piggyback_fires_on_before_agent(self) -> None:
        """Messages should be delivered on BEFORE_AGENT events."""
        msg = _make_msg(content="Turn-start message")
        enricher = _make_enricher(msgs=[msg])
        event = _make_event(HookEventType.BEFORE_AGENT)
        response = HookResponse()

        enricher.enrich(event, response)

        assert response.context is not None
        assert "Turn-start message" in response.context
        enricher._inter_session_msg_manager.mark_delivered.assert_called_once_with("msg-1")

    def test_piggyback_skips_session_start(self) -> None:
        """SESSION_START should NOT trigger piggyback delivery."""
        msg = _make_msg(content="Should not appear")
        enricher = _make_enricher(msgs=[msg])
        event = _make_event(HookEventType.SESSION_START)
        response = HookResponse()

        enricher.enrich(event, response)

        assert response.context is None or "Should not appear" not in response.context
        enricher._inter_session_msg_manager.get_undelivered_messages.assert_not_called()


class TestMessageGrouping:
    """Verify messages are grouped by type with correct headers."""

    def test_p2p_messages_show_sender_ref(self) -> None:
        """P2P messages should show sender session ref and P2P header."""
        session_storage = MagicMock()
        session_obj = MagicMock()
        session_obj.seq_num = 42
        session_storage.get.return_value = session_obj

        msg = _make_msg(content="Subtask done", message_type="message")
        enricher = _make_enricher(msgs=[msg], session_storage=session_storage)
        event = _make_event(HookEventType.BEFORE_TOOL)
        response = HookResponse()

        enricher.enrich(event, response)

        assert "[Pending P2P messages from other sessions]:" in response.context
        assert "Session #42: Subtask done" in response.context

    def test_web_chat_messages_labeled_separately(self) -> None:
        """Web chat messages should get their own header."""
        msg = _make_msg(content="User question", message_type="web_chat")
        enricher = _make_enricher(msgs=[msg])
        event = _make_event(HookEventType.BEFORE_TOOL)
        response = HookResponse()

        enricher.enrich(event, response)

        assert "[Pending messages from web chat user]:" in response.context
        assert "User question" in response.context

    def test_command_results_labeled(self) -> None:
        """Command results should get their own header."""
        msg = _make_msg(content="Command output", message_type="command_result")
        enricher = _make_enricher(msgs=[msg])
        event = _make_event(HookEventType.BEFORE_TOOL)
        response = HookResponse()

        enricher.enrich(event, response)

        assert "[Pending command results]:" in response.context
        assert "Command output" in response.context

    def test_mixed_message_types_grouped(self) -> None:
        """Messages of different types should be grouped under separate headers."""
        p2p_msg = _make_msg(content="P2P hello", msg_id="m1", message_type="message")
        chat_msg = _make_msg(content="Chat hello", msg_id="m2", message_type="web_chat")
        enricher = _make_enricher(msgs=[p2p_msg, chat_msg])
        event = _make_event(HookEventType.BEFORE_AGENT)
        response = HookResponse()

        enricher.enrich(event, response)

        assert "[Pending P2P messages from other sessions]:" in response.context
        assert "[Pending messages from web chat user]:" in response.context
        assert "P2P hello" in response.context
        assert "Chat hello" in response.context


class TestUrgentPriority:
    """Verify urgent messages are tagged."""

    def test_urgent_priority_tagged(self) -> None:
        """Messages with priority='urgent' should have [URGENT] prefix."""
        msg = _make_msg(content="Fix immediately", priority="urgent")
        enricher = _make_enricher(msgs=[msg])
        event = _make_event(HookEventType.BEFORE_TOOL)
        response = HookResponse()

        enricher.enrich(event, response)

        assert "[URGENT]" in response.context
        assert "Fix immediately" in response.context

    def test_normal_priority_not_tagged(self) -> None:
        """Normal priority messages should NOT have [URGENT] prefix."""
        msg = _make_msg(content="No rush", priority="normal")
        enricher = _make_enricher(msgs=[msg])
        event = _make_event(HookEventType.BEFORE_TOOL)
        response = HookResponse()

        enricher.enrich(event, response)

        assert "[URGENT]" not in response.context
        assert "No rush" in response.context


class TestSenderResolution:
    """Verify sender label resolution and fallbacks."""

    def test_sender_lookup_success(self) -> None:
        """Session storage lookup should produce 'Session #N:' label."""
        session_storage = MagicMock()
        session_obj = MagicMock()
        session_obj.seq_num = 7
        session_storage.get.return_value = session_obj

        msg = _make_msg(content="msg", from_session="aaaa-bbbb")
        enricher = _make_enricher(msgs=[msg], session_storage=session_storage)
        event = _make_event(HookEventType.BEFORE_TOOL)
        response = HookResponse()

        enricher.enrich(event, response)

        assert "Session #7:" in response.context

    def test_sender_lookup_failure_falls_back(self) -> None:
        """When session storage raises, fall back to truncated UUID."""
        session_storage = MagicMock()
        session_storage.get.side_effect = RuntimeError("DB closed")

        msg = _make_msg(content="msg", from_session="abcd1234-rest-of-uuid")
        enricher = _make_enricher(msgs=[msg], session_storage=session_storage)
        event = _make_event(HookEventType.BEFORE_TOOL)
        response = HookResponse()

        enricher.enrich(event, response)

        assert "Session abcd1234:" in response.context

    def test_sender_no_session_storage(self) -> None:
        """Without session storage, fall back to truncated UUID."""
        msg = _make_msg(content="msg", from_session="deadbeef-rest-of-uuid")
        enricher = _make_enricher(msgs=[msg], session_storage=None)
        event = _make_event(HookEventType.BEFORE_TOOL)
        response = HookResponse()

        enricher.enrich(event, response)

        assert "Session deadbeef:" in response.context

    def test_sender_no_from_session(self) -> None:
        """Messages with no from_session should have no sender prefix."""
        msg = _make_msg(content="anonymous msg", from_session=None)
        enricher = _make_enricher(msgs=[msg])
        event = _make_event(HookEventType.BEFORE_TOOL)
        response = HookResponse()

        enricher.enrich(event, response)

        assert "anonymous msg" in response.context
        assert "Session" not in response.context.split("anonymous")[0].split("\n")[-1]

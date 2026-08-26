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

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.hooks.event_enrichment import _PIGGYBACK_EVENTS, EventEnricher
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.storage import workspace_machine_scope
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(event_type: HookEventType, platform_session_id: str = "sess-abc") -> HookEvent:
    native_hook_types = {
        HookEventType.SESSION_START: "session-start",
        HookEventType.BEFORE_AGENT: "user-prompt-submit",
        HookEventType.BEFORE_TOOL: "pre-tool-use",
        HookEventType.AFTER_TOOL: "post-tool-use",
        HookEventType.SESSION_END: "session-end",
    }
    return HookEvent(
        event_type=event_type,
        session_id="ext-session-1",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={},
        metadata={
            "_platform_session_id": platform_session_id,
            "_native_hook_type": native_hook_types[event_type],
        },
    )


def _make_msg(
    content: str = "hello",
    msg_id: str = "msg-1",
    message_type: str = "message",
    from_session: str = "from-1111-2222-3333-444444444444",
    priority: str = "normal",
    metadata_json: str | None = None,
) -> MagicMock:
    msg = MagicMock()
    msg.content = content
    msg.id = msg_id
    msg.message_type = message_type
    msg.from_session = from_session
    msg.priority = priority
    msg.metadata_json = metadata_json
    return msg


def _make_enricher(
    msgs: list | None = None,
    session_manager: MagicMock | None = None,
    injected_sessions: set[str] | None = None,
) -> EventEnricher:
    mgr = MagicMock()
    mgr.get_undelivered_messages.return_value = msgs or []
    return EventEnricher(
        session_manager=session_manager,
        injected_sessions=injected_sessions if injected_sessions is not None else set(),
        inter_session_msg_manager=mgr,
    )


def test_session_end_releases_injection_marker() -> None:
    injected_sessions: set[str] = set()
    enricher = _make_enricher(injected_sessions=injected_sessions)

    enricher.enrich(_make_event(HookEventType.SESSION_START), HookResponse(decision="allow"))
    assert injected_sessions == {"sess-abc:claude"}

    enricher.enrich(_make_event(HookEventType.SESSION_END), HookResponse(decision="allow"))
    assert injected_sessions == set()


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
        enricher._inter_session_msg_manager.mark_delivered_batch.assert_called_once_with(
            ["msg-1"], "sess-abc"
        )

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
        session_manager = MagicMock()
        session_obj = MagicMock()
        session_obj.seq_num = 42
        session_manager.get.return_value = session_obj

        msg = _make_msg(content="Subtask done", message_type="message")
        enricher = _make_enricher(msgs=[msg], session_manager=session_manager)
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
        p2p_msg = _make_msg(
            content="P2P hello",
            msg_id="21000000-0000-4000-8000-000000000005",
            message_type="message",
        )
        chat_msg = _make_msg(content="Chat hello", msg_id="m2", message_type="web_chat")
        enricher = _make_enricher(msgs=[p2p_msg, chat_msg])
        event = _make_event(HookEventType.BEFORE_AGENT)
        response = HookResponse()

        enricher.enrich(event, response)

        assert "[Pending P2P messages from other sessions]:" in response.context
        assert "[Pending messages from web chat user]:" in response.context
        assert "P2P hello" in response.context
        assert "Chat hello" in response.context

    def test_completion_message_includes_run_task_type_and_signoff_context(self) -> None:
        """Turn-start pending injection includes durable signoff metadata."""
        msg = _make_msg(
            content="Review approved",
            message_type="completion_notification",
            metadata_json=(
                '{"run_id": "run-1", "task_id": "#12754", '
                '"completion_id": "run-1", "signoff_message": "Review approved"}'
            ),
        )
        enricher = _make_enricher(msgs=[msg])
        event = _make_event(HookEventType.BEFORE_AGENT)
        response = HookResponse()

        enricher.enrich(event, response)

        assert "Review approved" in response.context
        assert "type=completion_notification" in response.context
        assert "run_id=run-1" in response.context
        assert "task_id=#12754" in response.context
        assert "completion_id=run-1" in response.context
        assert "signoff=true" in response.context
        assert "from_session=from-1111-2222-3333-444444444444" in response.context


class TestMessageDeliveryOrdering:
    """Messages become delivered only after response attachment succeeds."""

    def test_formatting_failure_leaves_message_retryable(self) -> None:
        msg = _make_msg()
        enricher = _make_enricher([msg])
        enricher._resolve_sender_label = MagicMock(side_effect=RuntimeError("format failed"))
        response = HookResponse()

        enricher.enrich(_make_event(HookEventType.BEFORE_AGENT), response)

        enricher._inter_session_msg_manager.mark_delivered_batch.assert_not_called()
        assert response.context is None

    def test_mark_failure_warns_and_leaves_attached_message(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        msg = _make_msg()
        enricher = _make_enricher([msg])
        enricher._inter_session_msg_manager.mark_delivered_batch.side_effect = RuntimeError(
            "database unavailable"
        )
        response = HookResponse()

        with caplog.at_level("WARNING", logger="gobby.hooks.event_enrichment"):
            enricher.enrich(_make_event(HookEventType.BEFORE_AGENT), response)

        assert response.context is not None
        assert "hello" in response.context
        assert "Failed to mark piggyback messages delivered" in caplog.text


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
        session_manager = MagicMock()
        session_obj = MagicMock()
        session_obj.seq_num = 7
        session_manager.get.return_value = session_obj

        msg = _make_msg(content="msg", from_session="aaaa-bbbb")
        enricher = _make_enricher(msgs=[msg], session_manager=session_manager)
        event = _make_event(HookEventType.BEFORE_TOOL)
        response = HookResponse()

        enricher.enrich(event, response)

        assert "Session #7:" in response.context

    def test_sender_lookup_failure_falls_back(self) -> None:
        """When session storage raises, fall back to truncated UUID."""
        session_manager = MagicMock()
        session_manager.get.side_effect = RuntimeError("DB closed")

        msg = _make_msg(content="msg", from_session="abcd1234-rest-of-uuid")
        enricher = _make_enricher(msgs=[msg], session_manager=session_manager)
        event = _make_event(HookEventType.BEFORE_TOOL)
        response = HookResponse()

        enricher.enrich(event, response)

        assert "Session abcd1234:" in response.context

    def test_sender_no_session_storage(self) -> None:
        """Without session storage, fall back to truncated UUID."""
        msg = _make_msg(content="msg", from_session="deadbeef-rest-of-uuid")
        enricher = _make_enricher(msgs=[msg], session_manager=None)
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

        assert response.context is not None
        assert "anonymous msg" in response.context
        assert "Session" not in response.context.split("anonymous")[0].split("\n")[-1]


def test_grok_messages_enqueue_without_early_ack(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_id = "fe5f771f-dbc3-48b0-bd35-6a82ab18fdc1"
    LocalMachineManager(session_manager.db).upsert_seen(machine_id, TEST_USER_ID)
    monkeypatch.setattr(workspace_machine_scope, "require_machine_id", lambda: machine_id)
    session_id = session_manager.register_session(
        external_id="grok-message-recipient",
        machine_id=machine_id,
        source="grok",
        project_id=sample_project["id"],
    )
    message = _make_msg(content="queued for active channel", msg_id="message-queued")
    message_manager = MagicMock()
    message_manager.get_undelivered_messages.return_value = [message]
    enricher = EventEnricher(
        session_manager=session_manager,
        injected_sessions=set(),
        inter_session_msg_manager=message_manager,
    )
    event = _make_event(HookEventType.BEFORE_AGENT, platform_session_id=session_id)
    event.source = SessionSource.GROK
    responses = [
        HookResponse(decision="allow", context="workflow context"),
        HookResponse(decision="allow", context="workflow context"),
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(enricher.enrich, event, response) for response in responses]
        for future in futures:
            future.result()

    assert all(response.context == "workflow context" for response in responses)
    message_manager.mark_delivered_batch.assert_not_called()
    variables = SessionVariableManager(session_manager.db)
    briefing = variables.get_variables(session_id)["grok_pending_briefing"]
    assert len(briefing) == 1
    assert briefing[0]["id"] == "p2p:message-queued"
    assert "queued for active channel" in briefing[0]["text"]
    assert briefing[0]["message_ids"] == ["message-queued"]

    variables.set_variable(session_id, "grok_pending_briefing", [])
    variables.set_variable(
        session_id,
        "grok_pending_delivery",
        {"envelope_id": "claimed", "components": briefing},
    )
    enricher.enrich(event, HookResponse(decision="allow"))

    stored = variables.get_variables(session_id)
    assert stored["grok_pending_briefing"] == []
    assert stored["grok_pending_delivery"]["components"] == briefing

"""Tests for lossless bounded pending-message rendering."""

from __future__ import annotations

from dataclasses import dataclass

from gobby.hooks.pending_messages import (
    PENDING_MESSAGE_CONTEXT_BUDGET,
    PENDING_MESSAGE_INLINE_LIMIT,
    render_pending_messages,
)


@dataclass
class Message:
    id: str
    content: str
    message_type: str = "message"
    from_session: str = "sender-12345678"
    to_session: str = "recipient-12345678"
    priority: str = "normal"
    metadata_json: str | None = None


def _sender_label(session_id: str | None) -> str:
    return f"Session {session_id}: " if session_id else ""


def test_small_message_is_complete_and_large_message_is_referenced() -> None:
    small = Message(id="small-id", content="small sentinel")
    large = Message(id="large-id", content="large-sentinel-" + ("x" * 32_597))

    result = render_pending_messages([small, large], resolve_sender=_sender_label)

    assert result.context is not None
    assert "small sentinel" in result.context
    assert "large-sentinel-" not in result.context
    assert (
        "- Session sender-12345678: 32,612-character message; retrieve with "
        'gobby-agents.get_inter_session_message(message_id="large-id").'
    ) in result.context
    assert result.context.count("large-id") == 1
    assert result.represented_message_ids == ("small-id", "large-id")
    assert result.deferred_message_ids == ()
    assert len(result.context) <= PENDING_MESSAGE_CONTEXT_BUDGET


def test_inline_limit_is_lossless_at_boundary() -> None:
    inline = Message(id="inline", content="i" * PENDING_MESSAGE_INLINE_LIMIT)
    referenced = Message(id="referenced", content="r" * (PENDING_MESSAGE_INLINE_LIMIT + 1))

    result = render_pending_messages([inline, referenced], resolve_sender=_sender_label)

    assert result.context is not None
    assert inline.content in result.context
    assert referenced.content not in result.context
    assert 'get_inter_session_message(message_id="referenced")' in result.context


def test_aggregate_exhaustion_defers_current_and_later_messages_in_order() -> None:
    messages = [
        Message(id=f"m{index}", content=f"sentinel-{index}-" + ("x" * 1_850)) for index in range(5)
    ]

    result = render_pending_messages(messages, resolve_sender=_sender_label)

    assert result.context is not None
    assert len(result.context) <= PENDING_MESSAGE_CONTEXT_BUDGET
    assert result.represented_message_ids == ("m0", "m1", "m2")
    assert result.deferred_message_ids == ("m3", "m4")
    assert "sentinel-2-" in result.context
    assert "sentinel-3-" not in result.context


def test_mixed_types_and_priorities_keep_input_order_and_labels() -> None:
    messages = [
        Message(id="normal", content="first", message_type="message"),
        Message(id="urgent", content="second", message_type="web_chat", priority="urgent"),
        Message(id="high", content="third", message_type="message", priority="high"),
    ]

    result = render_pending_messages(messages, resolve_sender=_sender_label)

    assert result.context is not None
    assert result.context.index("first") < result.context.index("second")
    assert result.context.index("second") < result.context.index("third")
    assert "[URGENT]" in result.context
    assert "[PRIORITY: HIGH]" in result.context
    assert result.represented_message_ids == ("normal", "urgent", "high")


def test_self_origin_completion_omits_attribution_and_keeps_metadata() -> None:
    message = Message(
        id="self-completion",
        content="Expansion completed",
        message_type="completion_notification",
        from_session="session-self",
        to_session="session-self",
        metadata_json=('{"completion_id":"completion-1","run_id":"run-1","task_id":"#42"}'),
    )

    result = render_pending_messages([message], resolve_sender=_sender_label)

    assert result.context is not None
    assert result.context.startswith("[Pending completion notifications]:")
    assert "Session session-self:" not in result.context
    assert "from_session=" not in result.context
    assert "type=completion_notification" in result.context
    assert "completion_id=completion-1" in result.context
    assert "run_id=run-1" in result.context
    assert "task_id=#42" in result.context


def test_child_completion_keeps_sender_and_from_session_attribution() -> None:
    message = Message(
        id="child-completion",
        content="Child completed",
        message_type="completion_notification",
        from_session="session-child",
        to_session="session-parent",
        metadata_json='{"completion_id":"completion-2"}',
    )

    result = render_pending_messages([message], resolve_sender=_sender_label)

    assert result.context is not None
    assert result.context.startswith("[Pending completion notifications]:")
    assert "Session session-child: Child completed" in result.context
    assert "from_session=session-child" in result.context
    assert "completion_id=completion-2" in result.context

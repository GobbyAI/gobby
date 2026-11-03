"""Tests for lossless bounded pending-message rendering."""

from __future__ import annotations

from dataclasses import dataclass

from gobby.hooks.pending_messages import (
    PENDING_MESSAGE_CONTEXT_BUDGET,
    PENDING_MESSAGE_INLINE_LIMIT,
    render_pending_messages,
)
from gobby.storage.sessions import system_session_id


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
        'gobby-agents:get_inter_session_message(message_id="large-id").'
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


def test_render_pending_messages_shows_attachment_local_paths() -> None:
    local_path = "/var/gobby/files/shot.png"
    message = Message(
        id="photo-1",
        content="see this",
        message_type="telegram_message",
        metadata_json=(
            '{"attachments":[{"filename":"shot.png","content_type":"image/png",'
            '"local_path":"/var/gobby/files/shot.png"}]}'
        ),
    )

    result = render_pending_messages([message], resolve_sender=_sender_label)

    assert result.context is not None
    assert local_path in result.context


def test_every_injected_line_carries_its_message_id_regardless_of_length() -> None:
    short = Message(id="short-id", content="PROOF_REQUEST: rerun the gate")
    boundary = Message(id="boundary-id", content="b" * PENDING_MESSAGE_INLINE_LIMIT)
    long = Message(id="long-id", content="l" * (PENDING_MESSAGE_INLINE_LIMIT + 1))

    result = render_pending_messages([short, boundary, long], resolve_sender=_sender_label)

    assert result.context is not None
    lines = [line for line in result.context.splitlines() if line.startswith("- ")]
    assert len(lines) == 3
    assert lines[0] == (
        "- Session sender-12345678: PROOF_REQUEST: rerun the gate "
        "(message_id=short-id, type=message, from_session=sender-12345678)"
    )
    assert "(message_id=boundary-id, type=message" in lines[1]
    assert 'get_inter_session_message(message_id="long-id")' in lines[2]


def _telegram(metadata: str, *, from_session: str | None = None) -> Message:
    return Message(
        id="tg-1",
        content="are you there?",
        message_type="telegram_message",
        from_session=from_session or system_session_id(),
        metadata_json=metadata,
    )


def test_attached_telegram_message_says_typed_reply_is_mirrored() -> None:
    message = _telegram(
        '{"channel":"ops-dm","sender_username":"op","operator_comms":true,"wake_requested":true}'
    )

    context = render_pending_messages([message], resolve_sender=_sender_label).context

    assert context is not None
    assert context.startswith("[Pending Telegram messages]:")
    assert "- Telegram @op: are you there?" in context
    assert "attached to this Telegram conversation" in context
    assert "mirrored there automatically" in context
    assert "from_session=" not in context
    assert "send_operator_message" not in context
    assert "P2P" not in context


def test_unattached_telegram_message_names_the_channel_reply_path() -> None:
    message = _telegram('{"channel":"ops-dm","sender":"77"}')

    context = render_pending_messages([message], resolve_sender=_sender_label).context

    assert context is not None
    assert "- Telegram: are you there?" in context
    assert "not attached" in context
    assert 'gobby-communications:send_message(channel="ops-dm")' in context
    assert "mirrored there automatically" not in context
    assert "send_operator_message" not in context


def test_session_authored_telegram_type_keeps_session_attribution() -> None:
    message = _telegram('{"operator_comms":true}', from_session="sender-12345678")

    context = render_pending_messages([message], resolve_sender=_sender_label).context

    assert context is not None
    assert "Session sender-12345678: are you there?" in context
    assert "from_session=sender-12345678" in context
    assert "Telegram @" not in context
    assert "attached to this Telegram conversation" not in context

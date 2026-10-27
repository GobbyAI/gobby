"""Responder identity evidence: prompt contract and outbound session lookup."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from gobby.communications.chat_backend import recent_outbound_session_ids
from gobby.communications.models import CommsMessage


def _message(
    *,
    direction: Literal["inbound", "outbound"],
    session_id: str | None,
    conversation_id: str = "chat-42",
) -> CommsMessage:
    return CommsMessage(
        id=f"{direction}-{session_id}",
        channel_id="channel-1",
        direction=direction,
        content="hello",
        created_at=datetime.now(UTC),
        session_id=session_id,
        metadata_json={"platform_destination": conversation_id},
    )


def test_recent_outbound_session_ids_name_sending_sessions() -> None:
    messages = [
        _message(direction="outbound", session_id="sess-responder"),
        _message(direction="inbound", session_id="sess-user"),
        _message(direction="outbound", session_id="sess-responder"),
        _message(direction="outbound", session_id="sess-coordinator"),
        _message(direction="outbound", session_id=None),
        _message(direction="outbound", session_id="sess-other", conversation_id="other-chat"),
    ]

    assert recent_outbound_session_ids(messages, conversation_id="chat-42") == [
        "sess-responder",
        "sess-coordinator",
    ]

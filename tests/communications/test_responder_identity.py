"""Responder identity evidence: prompt contract and outbound session lookup."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from gobby.communications.chat_backend import recent_outbound_session_ids
from gobby.communications.models import CommsMessage

_PERSONA = (
    Path(__file__).resolve().parents[2]
    / "src/gobby/install/shared/workflows/agents/comms-agent.yaml"
)


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


def test_comms_agent_persona_states_its_identity() -> None:
    text = _PERSONA.read_text(encoding="utf-8")
    assert "communications responder for this chat" in text
    assert "connected to another session" in text
    assert "without that evidence" in text
    assert "comms_messages.session_id" in text


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

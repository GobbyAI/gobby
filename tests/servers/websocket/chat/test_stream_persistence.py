"""Web-chat message persistence metadata contracts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from gobby.servers.websocket.chat._stream_persistence import ChatStreamPersistence
from gobby.servers.websocket.chat.content_blocks import AssistantContentBlocks
from gobby.storage import chat_messages as cm_store
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import get_machine_id

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_persisted_first_user_message_promotes_web_chat_title(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    db_session = session_manager.register(
        external_id="web-chat-title",
        machine_id=get_machine_id(),
        source="codex",
        project_id=sample_project["id"],
        session_type="web_chat",
    )
    owner = SimpleNamespace(session_manager=session_manager, _chat_sessions={})
    persistence = ChatStreamPersistence(owner, "web-chat-title", AssistantContentBlocks())

    await persistence.persist_user_message(
        SimpleNamespace(db_session_id=db_session.id),
        "Please diagnose the websocket persistence regression",
        None,
    )

    promoted = session_manager.get(db_session.id)
    assert promoted is not None
    assert promoted.title == (
        f"test-project#{db_session.seq_num}: diagnose websocket persistence regression"
    )
    assert promoted.title_source == "heuristic"


@pytest.mark.asyncio
async def test_title_promotion_failure_does_not_abort_persisted_user_message(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    db_session = session_manager.register(
        external_id="web-chat-title-failure",
        machine_id=get_machine_id(),
        source="codex",
        project_id=sample_project["id"],
        session_type="web_chat",
    )
    owner = SimpleNamespace(session_manager=session_manager, _chat_sessions={})
    persistence = ChatStreamPersistence(
        owner,
        "web-chat-title-failure",
        AssistantContentBlocks(),
    )

    with patch(
        "gobby.servers.websocket.chat._stream_persistence.promote_heuristic_title",
        side_effect=RuntimeError("metadata unavailable"),
    ):
        await persistence.persist_user_message(
            SimpleNamespace(db_session_id=db_session.id),
            "Deliver this prompt despite metadata failure",
            None,
        )

    messages = cm_store.get_messages(session_manager.db, db_session.id)
    assert [(message["role"], message["content"]) for message in messages] == [
        ("user", "Deliver this prompt despite metadata failure")
    ]
    unchanged = session_manager.get(db_session.id)
    assert unchanged is not None
    assert unchanged.title_source == "provisional"

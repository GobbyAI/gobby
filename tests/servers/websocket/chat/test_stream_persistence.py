"""Web-chat message persistence metadata contracts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from gobby.servers.websocket.chat._stream_persistence import ChatStreamPersistence
from gobby.servers.websocket.chat.content_blocks import AssistantContentBlocks
from gobby.storage import chat_messages as cm_store
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import get_machine_id

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_persisted_first_user_message_leaves_provisional_title(
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
    before = session_manager.get(db_session.id)
    assert before is not None
    owner = SimpleNamespace(session_manager=session_manager, _chat_sessions={})
    persistence = ChatStreamPersistence(owner, "web-chat-title", AssistantContentBlocks())

    await persistence.persist_user_message(
        SimpleNamespace(db_session_id=db_session.id),
        "Please diagnose the websocket persistence regression",
        None,
    )

    messages = cm_store.get_messages(session_manager.db, db_session.id)
    assert [(message["role"], message["content"]) for message in messages] == [
        ("user", "Please diagnose the websocket persistence regression")
    ]
    after = session_manager.get(db_session.id)
    assert after is not None
    assert after.title == before.title == f"test-project#{after.seq_num}: Codex"
    assert after.title_source == "provisional"

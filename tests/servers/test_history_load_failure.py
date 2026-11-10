"""A failed history read withholds the prompt and keeps the injection owed (#25851).

Runs the real history loader and the real prompt hook. Only the message store fails, once.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from gobby.servers.chat_session_messages import ChatSessionMessagesMixin
from gobby.storage.hub.protocol import HubDatabase
from tests.adapters.test_context_paging import (
    _ChatHooks,
    _delivered_context,
    _register_session,
)

pytestmark = [pytest.mark.unit]

HISTORY = {"role": "user", "content_type": "text", "content": "the earlier question"}


class _HistoryHooks(_ChatHooks):
    """The chat hook surface with the real history loader, so a storage error reaches the prompt."""

    _message_manager: Any = None
    _message_manager_source_session_id: str | None = None
    _load_history_context = cast(Any, ChatSessionMessagesMixin._load_history_context)


@pytest.mark.asyncio
async def test_a_history_read_that_fails_once_withholds_the_prompt_and_the_retry_injects_history(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _register_session(temp_db, monkeypatch, "22000000-0000-4000-8000-0000000000d1")
    chat = _HistoryHooks(temp_db, session_id)
    chat._needs_history_injection = True
    manager = AsyncMock()
    manager.get_messages.side_effect = [OSError("history read failed"), [HISTORY]]
    chat._message_manager = manager
    chat._message_manager_source_session_id = None

    with pytest.raises(OSError, match="history read failed"):
        await chat.prompt("allow", [])
    assert chat._needs_history_injection is True

    context = _delivered_context(await chat.prompt("allow", []))
    assert context.count("<conversation-history>") == 1
    assert "the earlier question" in context
    assert chat._needs_history_injection is False

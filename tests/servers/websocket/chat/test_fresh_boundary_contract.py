"""Web chat exposes compact delivery without a clear-handoff continuation API."""

import pytest

from gobby.servers.websocket.chat._session import ChatSessionMixin
from gobby.servers.websocket.chat.session_registry import WebChatSessionRegistry


@pytest.mark.unit
def test_web_chat_has_no_clear_handoff_api() -> None:
    assert not hasattr(WebChatSessionRegistry, "clear_session")
    assert not hasattr(WebChatSessionRegistry, "bind_clear_lifecycle")
    assert not hasattr(ChatSessionMixin, "commit_clear_successor")

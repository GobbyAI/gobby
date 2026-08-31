"""Claude web-chat backend adapter."""

from __future__ import annotations

import shutil

from gobby.servers.chat_session import ChatSession
from gobby.servers.websocket.chat.backends.base import ProviderBackendHealth


class ClaudeWebChatBackend:
    """Trivial backend wrapper for Claude's existing ChatSession transport."""

    provider = "claude"

    def create_session(self, conversation_id: str) -> ChatSession:
        return ChatSession(conversation_id=conversation_id)

    @staticmethod
    def health() -> ProviderBackendHealth:
        return ProviderBackendHealth(
            provider="claude",
            available=shutil.which("claude") is not None,
            startup_error=None if shutil.which("claude") else "claude CLI not found in PATH",
        )


__all__ = ["ClaudeWebChatBackend"]

"""Claude web-chat backend adapter."""

from __future__ import annotations

import shutil

from gobby.agents.sandbox import SandboxConfig
from gobby.servers.chat_session import ChatSession
from gobby.servers.websocket.chat.backends.base import ProviderBackendHealth


class ClaudeWebChatBackend:
    """Trivial backend wrapper for Claude's existing ChatSession transport."""

    provider = "claude"

    def __init__(self, *, sandbox_config: SandboxConfig | None = None) -> None:
        self._sandbox_config = sandbox_config

    def create_session(self, conversation_id: str) -> ChatSession:
        return ChatSession(conversation_id=conversation_id, sandbox_config=self._sandbox_config)

    @staticmethod
    def health() -> ProviderBackendHealth:
        return ProviderBackendHealth(
            provider="claude",
            available=shutil.which("claude") is not None,
            startup_error=None if shutil.which("claude") else "claude CLI not found in PATH",
        )


__all__ = ["ClaudeWebChatBackend"]

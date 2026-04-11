"""Daemon-owned runtime manager for web-chat providers."""

from __future__ import annotations

from typing import Any

from gobby.adapters.codex_impl.client import CodexAppServerClient
from gobby.servers.chat_session import ChatSession
from gobby.servers.chat_session_base import ChatSessionProtocol
from gobby.servers.websocket.chat.provider_backends import (
    ClaudeWebChatBackend,
    CodexManagedChatSession,
    CodexWebChatBackend,
    GeminiManagedChatSession,
    GeminiWebChatBackend,
    ProviderBackendHealth,
)


class WebChatRuntimeManager:
    """Owns startup-managed provider backends for web chat."""

    def __init__(
        self,
        *,
        codex_client: CodexAppServerClient | None = None,
        gemini_default_model: str | None = None,
    ) -> None:
        self._claude_backend = ClaudeWebChatBackend()
        self._codex_backend = CodexWebChatBackend(client=codex_client)
        self._gemini_backend = GeminiWebChatBackend(default_model=gemini_default_model)

    @property
    def codex_client(self) -> CodexAppServerClient | None:
        """Expose the shared Codex app-server client."""
        return self._codex_backend._client

    async def start(self, *, background: bool = False) -> None:
        """Start daemon-owned provider backends."""
        if background:
            await self._codex_backend.start(background=True)
            await self._gemini_backend.start(background=True)
            return

        await self._codex_backend.start()
        await self._gemini_backend.start()

    async def stop(self) -> None:
        """Stop daemon-owned provider backends."""
        await self._gemini_backend.stop()
        await self._codex_backend.stop()

    def health(self, provider: str) -> ProviderBackendHealth:
        """Return provider backend health for picker availability and status."""
        if provider == "codex":
            return self._codex_backend.health()
        if provider == "gemini":
            return self._gemini_backend.health()
        if provider == "claude":
            return self._claude_backend.health()
        return ProviderBackendHealth(provider=provider, available=False, startup_error="unknown")

    def health_snapshot(self) -> dict[str, dict[str, Any]]:
        """Return all provider health states as plain dicts."""
        return {
            "claude": self.health("claude").to_dict(),
            "gemini": self.health("gemini").to_dict(),
            "codex": self.health("codex").to_dict(),
        }

    def create_session(
        self,
        *,
        provider: str,
        conversation_id: str,
        model: str | None = None,
    ) -> ChatSessionProtocol:
        """Create a provider-specific session wrapper for web chat."""
        if provider == "gemini":
            return GeminiManagedChatSession(
                conversation_id=conversation_id,
                _backend=self._gemini_backend,
                _model=model,
            )
        if provider == "codex":
            return CodexManagedChatSession(
                conversation_id=conversation_id,
                _backend=self._codex_backend,
                _model=model,
            )

        session: ChatSessionProtocol = self._claude_backend.create_session(conversation_id)
        if isinstance(session, ChatSession) and model:
            session._model = model
        return session

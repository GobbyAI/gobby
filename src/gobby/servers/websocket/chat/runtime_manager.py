"""Daemon-owned runtime manager for web-chat providers."""

from __future__ import annotations

from typing import Any

from gobby.adapters.codex_impl.client import CodexAppServerClient
from gobby.agents.sandbox import SandboxConfig, sandbox_config_for_provider
from gobby.servers.chat_session import ChatSession
from gobby.servers.chat_session_base import ChatSessionProtocol
from gobby.servers.websocket.chat.provider_backends import (
    ClaudeWebChatBackend,
    CodexManagedChatSession,
    CodexWebChatBackend,
    GeminiManagedChatSession,
    GeminiWebChatBackend,
    ProviderBackendHealth,
    QwenManagedChatSession,
    QwenWebChatBackend,
)


def _default_runtime_sandbox(provider: str, daemon_config: Any | None) -> SandboxConfig:
    """Resolve the runtime sandbox defaults for a provider."""
    config = sandbox_config_for_provider(provider, daemon_config)
    if config is not None:
        return config
    return SandboxConfig(enabled=False)


class WebChatRuntimeManager:
    """Owns startup-managed provider backends for web chat."""

    def __init__(
        self,
        *,
        codex_client: CodexAppServerClient | None = None,
        gemini_default_model: str | None = None,
        codex_transcript_retry_attempts: int = 5,
        codex_transcript_retry_delay_seconds: float = 0.1,
        daemon_config: Any | None = None,
    ) -> None:
        self._claude_backend = ClaudeWebChatBackend(
            sandbox_config=_default_runtime_sandbox("claude", daemon_config)
        )
        self._codex_backend = CodexWebChatBackend(
            client=codex_client,
            transcript_retry_attempts=codex_transcript_retry_attempts,
            transcript_retry_delay_seconds=codex_transcript_retry_delay_seconds,
            sandbox_config=_default_runtime_sandbox("codex", daemon_config),
        )
        self._gemini_backend = GeminiWebChatBackend(
            default_model=gemini_default_model,
            sandbox_config=_default_runtime_sandbox("gemini", daemon_config),
        )
        self._qwen_backend = QwenWebChatBackend(
            sandbox_config=_default_runtime_sandbox("qwen", daemon_config)
        )

    @property
    def codex_client(self) -> CodexAppServerClient | None:
        """Expose the shared Codex app-server client."""
        return self._codex_backend.client

    async def start(self, *, background: bool = False) -> None:
        """Start daemon-owned provider backends."""
        if background:
            await self._codex_backend.start(background=True)
            await self._gemini_backend.start(background=True)
            await self._qwen_backend.start(background=True)
            return

        await self._codex_backend.start()
        await self._gemini_backend.start()
        await self._qwen_backend.start()

    async def stop(self) -> None:
        """Stop daemon-owned provider backends."""
        await self._qwen_backend.stop()
        await self._gemini_backend.stop()
        await self._codex_backend.stop()

    def health(self, provider: str) -> ProviderBackendHealth:
        """Return provider backend health for picker availability and status."""
        if provider == "codex":
            return self._codex_backend.health()
        if provider == "gemini":
            return self._gemini_backend.health()
        if provider == "qwen":
            return self._qwen_backend.health()
        if provider == "claude":
            return self._claude_backend.health()
        return ProviderBackendHealth(provider=provider, available=False, startup_error="unknown")

    def health_snapshot(self) -> dict[str, dict[str, Any]]:
        """Return all provider health states as plain dicts."""
        return {
            "claude": self.health("claude").to_dict(),
            "gemini": self.health("gemini").to_dict(),
            "qwen": self.health("qwen").to_dict(),
            "codex": self.health("codex").to_dict(),
        }

    def create_session(
        self,
        *,
        provider: str,
        conversation_id: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ChatSessionProtocol:
        """Create a provider-specific session wrapper for web chat."""
        if provider == "gemini":
            return GeminiManagedChatSession(
                conversation_id=conversation_id,
                _backend=self._gemini_backend,
                _model=model,
                reasoning_effort=reasoning_effort,
            )
        if provider == "qwen":
            return QwenManagedChatSession(
                conversation_id=conversation_id,
                _backend=self._qwen_backend,
                _model=model,
                reasoning_effort=reasoning_effort,
            )
        if provider == "codex":
            return CodexManagedChatSession(
                conversation_id=conversation_id,
                _backend=self._codex_backend,
                _model=model,
                reasoning_effort=reasoning_effort,
                _transcript_retry_attempts=self._codex_backend.transcript_retry_attempts,
                _transcript_retry_delay_seconds=self._codex_backend.transcript_retry_delay_seconds,
            )

        session: ChatSessionProtocol = self._claude_backend.create_session(conversation_id)
        if isinstance(session, ChatSession) and model:
            session._model = model
        if isinstance(session, ChatSession):
            session.reasoning_effort = reasoning_effort
        return session

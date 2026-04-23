"""Compatibility imports for daemon-owned web-chat provider backends."""

from __future__ import annotations

from gobby.servers.websocket.chat.backends import (
    ClaudeWebChatBackend,
    CodexManagedChatSession,
    CodexWebChatBackend,
    GeminiManagedChatSession,
    GeminiWebChatBackend,
    ManagedChatSessionBase,
    ProviderBackendHealth,
    QwenManagedChatSession,
    QwenWebChatBackend,
)

__all__ = [
    "ClaudeWebChatBackend",
    "CodexManagedChatSession",
    "CodexWebChatBackend",
    "GeminiManagedChatSession",
    "GeminiWebChatBackend",
    "ManagedChatSessionBase",
    "ProviderBackendHealth",
    "QwenManagedChatSession",
    "QwenWebChatBackend",
]

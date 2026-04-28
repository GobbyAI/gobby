"""Daemon-owned web-chat provider backends."""

from __future__ import annotations

from typing import TYPE_CHECKING

from gobby.servers.websocket.chat.backends.base import (
    ManagedChatSessionBase,
    ProviderBackendHealth,
)
from gobby.servers.websocket.chat.backends.claude import ClaudeWebChatBackend
from gobby.servers.websocket.chat.backends.codex import (
    CodexManagedChatSession,
    CodexWebChatBackend,
)
from gobby.servers.websocket.chat.backends.gemini import (
    GeminiManagedChatSession,
    GeminiWebChatBackend,
)
from gobby.servers.websocket.chat.backends.qwen import (
    QwenManagedChatSession,
    QwenWebChatBackend,
)

if TYPE_CHECKING:
    from gobby.servers.websocket.chat.droid_backend import (
        DroidManagedChatSession,
        DroidWebChatBackend,
    )


def __getattr__(name: str) -> object:
    """Lazily expose Droid classes without re-entering the Droid module at import time."""
    if name == "DroidManagedChatSession":
        from gobby.servers.websocket.chat.droid_backend import DroidManagedChatSession

        globals()[name] = DroidManagedChatSession
        return DroidManagedChatSession
    if name == "DroidWebChatBackend":
        from gobby.servers.websocket.chat.droid_backend import DroidWebChatBackend

        globals()[name] = DroidWebChatBackend
        return DroidWebChatBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "ClaudeWebChatBackend",
    "CodexManagedChatSession",
    "CodexWebChatBackend",
    "DroidManagedChatSession",
    "DroidWebChatBackend",
    "GeminiManagedChatSession",
    "GeminiWebChatBackend",
    "ManagedChatSessionBase",
    "ProviderBackendHealth",
    "QwenManagedChatSession",
    "QwenWebChatBackend",
]

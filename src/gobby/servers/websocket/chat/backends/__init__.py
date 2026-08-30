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
from gobby.servers.websocket.chat.backends.grok import (
    GrokManagedChatSession,
    GrokWebChatBackend,
)
from gobby.servers.websocket.chat.backends.qwen import (
    QwenManagedChatSession,
    QwenWebChatBackend,
)

if TYPE_CHECKING:
    from gobby.servers.websocket.chat.backends.agy import (
        AgyManagedChatSession,
        AgyWebChatBackend,
    )
    from gobby.servers.websocket.chat.backends.droid import (
        DroidManagedChatSession,
        DroidWebChatBackend,
    )


def __getattr__(name: str) -> object:
    """Lazily expose Droid/AGY classes without re-entering those modules at import time."""
    if name == "DroidManagedChatSession":
        from gobby.servers.websocket.chat.backends.droid import DroidManagedChatSession

        globals()[name] = DroidManagedChatSession
        return DroidManagedChatSession
    if name == "DroidWebChatBackend":
        from gobby.servers.websocket.chat.backends.droid import DroidWebChatBackend

        globals()[name] = DroidWebChatBackend
        return DroidWebChatBackend
    if name == "AgyManagedChatSession":
        from gobby.servers.websocket.chat.backends.agy import AgyManagedChatSession

        globals()[name] = AgyManagedChatSession
        return AgyManagedChatSession
    if name == "AgyWebChatBackend":
        from gobby.servers.websocket.chat.backends.agy import AgyWebChatBackend

        globals()[name] = AgyWebChatBackend
        return AgyWebChatBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AgyManagedChatSession",
    "AgyWebChatBackend",
    "ClaudeWebChatBackend",
    "CodexManagedChatSession",
    "CodexWebChatBackend",
    "DroidManagedChatSession",
    "DroidWebChatBackend",
    "GrokManagedChatSession",
    "GrokWebChatBackend",
    "ManagedChatSessionBase",
    "ProviderBackendHealth",
    "QwenManagedChatSession",
    "QwenWebChatBackend",
]

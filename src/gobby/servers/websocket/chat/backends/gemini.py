"""Gemini daemon-owned web-chat backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from gobby.adapters.acp_client import ACPClient
from gobby.adapters.gemini import GeminiAdapter
from gobby.adapters.gemini_acp_client import GeminiACPClient
from gobby.servers.websocket.chat.backends.acp import ACPWebChatBackend
from gobby.servers.websocket.chat.backends.acp_session import ACPManagedChatSession

_GEMINI_TOOL_NAME_ADAPTER = GeminiAdapter()


@dataclass
class GeminiManagedChatSession(ACPManagedChatSession):
    """Web-chat session backed by the shared Gemini ACP backend."""

    provider: str = field(default="gemini", init=False)
    chat_mode: str = field(default="plan")

    def _tool_name_adapter(self) -> Any:
        return _GEMINI_TOOL_NAME_ADAPTER


class GeminiWebChatBackend(ACPWebChatBackend):
    """Shared daemon-owned Gemini ACP backend."""

    provider: ClassVar[str] = "gemini"
    display_name: ClassVar[str] = "Gemini"
    acp_client_cls: ClassVar[type[ACPClient]] = GeminiACPClient


__all__ = [
    "GeminiManagedChatSession",
    "GeminiWebChatBackend",
]

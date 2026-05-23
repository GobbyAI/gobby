"""Grok daemon-owned web-chat backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from gobby.adapters.acp_client import ACPClient
from gobby.adapters.grok import GrokAdapter
from gobby.adapters.grok_acp_client import GrokACPClient
from gobby.servers.websocket.chat.backends.acp import ACPWebChatBackend
from gobby.servers.websocket.chat.backends.acp_session import ACPManagedChatSession

_GROK_TOOL_NAME_ADAPTER = GrokAdapter()


@dataclass
class GrokManagedChatSession(ACPManagedChatSession):
    """Web-chat session backed by the shared Grok ACP backend."""

    provider: str = field(default="grok", init=False)
    chat_mode: str = field(default="plan")

    def _tool_name_adapter(self) -> Any:
        return _GROK_TOOL_NAME_ADAPTER


class GrokWebChatBackend(ACPWebChatBackend):
    """Shared daemon-owned Grok ACP backend."""

    provider: ClassVar[str] = "grok"
    display_name: ClassVar[str] = "Grok"
    acp_client_cls: ClassVar[type[ACPClient]] = GrokACPClient


__all__ = [
    "GrokManagedChatSession",
    "GrokWebChatBackend",
]

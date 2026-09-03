"""Qwen daemon-owned web-chat backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from gobby.adapters.acp_client import ACPClient
from gobby.adapters.acp_tool_names import normalize_acp_tool_name
from gobby.adapters.qwen_acp_client import QwenACPClient
from gobby.servers.websocket.chat.backends.acp import ACPWebChatBackend
from gobby.servers.websocket.chat.backends.acp_session import ACPManagedChatSession

_QWEN_BACKEND_START_TIMEOUT_SECONDS = 60.0


@dataclass
class QwenManagedChatSession(ACPManagedChatSession):
    """Web-chat session backed by the shared Qwen ACP backend."""

    provider: str = field(default="qwen", init=False)
    chat_mode: str = field(default="plan")

    def _tool_name_adapter(self) -> Any:
        return normalize_acp_tool_name


class QwenWebChatBackend(ACPWebChatBackend):
    """Shared daemon-owned Qwen ACP backend."""

    provider: ClassVar[str] = "qwen"
    display_name: ClassVar[str] = "Qwen"
    start_timeout_seconds: ClassVar[float] = _QWEN_BACKEND_START_TIMEOUT_SECONDS
    acp_client_cls: ClassVar[type[ACPClient]] = QwenACPClient


__all__ = [
    "QwenManagedChatSession",
    "QwenWebChatBackend",
]

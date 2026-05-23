"""Qwen daemon-owned web-chat backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from gobby.adapters.acp_client import ACPClient
from gobby.adapters.qwen import QwenAdapter
from gobby.adapters.qwen_acp_client import QwenACPClient
from gobby.servers.websocket.chat.backends.acp import ACPWebChatBackend
from gobby.servers.websocket.chat.backends.acp_session import ACPManagedChatSession
from gobby.servers.websocket.chat.local_openai_warmup import (
    ensure_qwen_local_openai_model_ready,
)

_QWEN_TOOL_NAME_ADAPTER = QwenAdapter()
# Qwen's ACP backend can spend extra time warming the local OpenAI-compatible model.
_QWEN_BACKEND_START_TIMEOUT_SECONDS = 60.0


@dataclass
class QwenManagedChatSession(ACPManagedChatSession):
    """Web-chat session backed by the shared Qwen ACP backend."""

    provider: str = field(default="qwen", init=False)
    chat_mode: str = field(default="plan")

    def _tool_name_adapter(self) -> Any:
        return _QWEN_TOOL_NAME_ADAPTER


class QwenWebChatBackend(ACPWebChatBackend):
    """Shared daemon-owned Qwen ACP backend."""

    provider: ClassVar[str] = "qwen"
    display_name: ClassVar[str] = "Qwen"
    start_timeout_seconds: ClassVar[float] = _QWEN_BACKEND_START_TIMEOUT_SECONDS
    acp_client_cls: ClassVar[type[ACPClient]] = QwenACPClient

    async def attach_session(
        self,
        session: ACPManagedChatSession,
        *,
        model: str | None = None,
    ) -> None:
        resolved_model = model or session._model or self._default_model
        await ensure_qwen_local_openai_model_ready(
            resolved_model,
            project_path=session.project_path,
        )
        await super().attach_session(session, model=model)


__all__ = [
    "QwenManagedChatSession",
    "QwenWebChatBackend",
]

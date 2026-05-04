"""Qwen daemon-owned web-chat backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gobby.adapters.gemini_acp_client import GeminiACPClient
from gobby.adapters.qwen import QwenAdapter
from gobby.agents.sandbox import SandboxConfig
from gobby.servers.websocket.chat.backends.gemini import (
    GeminiManagedChatSession,
    GeminiWebChatBackend,
)
from gobby.servers.websocket.chat.local_openai_warmup import (
    ensure_qwen_local_openai_model_ready,
)

_QWEN_TOOL_NAME_ADAPTER = QwenAdapter()
# Qwen's ACP backend can spend extra time warming the local OpenAI-compatible model.
_QWEN_BACKEND_START_TIMEOUT_SECONDS = 60.0


@dataclass
class QwenManagedChatSession(GeminiManagedChatSession):
    """Web-chat session backed by the shared Qwen ACP backend."""

    provider: str = field(default="qwen", init=False)
    chat_mode: str = field(default="plan")

    def _tool_name_adapter(self) -> Any:
        return _QWEN_TOOL_NAME_ADAPTER


class QwenWebChatBackend(GeminiWebChatBackend):
    """Shared daemon-owned Qwen ACP backend."""

    provider = "qwen"

    def __init__(
        self,
        *,
        client: GeminiACPClient | None = None,
        default_model: str | None = None,
        sandbox_config: SandboxConfig | None = None,
    ) -> None:
        super().__init__(
            client=client
            or GeminiACPClient(
                cli_name="qwen",
                display_name="Qwen",
                prompt_timeout_env="GOBBY_QWEN_ACP_PROMPT_TIMEOUT_SECONDS",
            ),
            default_model=default_model,
            provider="qwen",
            display_name="Qwen",
            sandbox_config=sandbox_config,
            start_timeout_seconds=_QWEN_BACKEND_START_TIMEOUT_SECONDS,
        )

    async def attach_session(
        self,
        session: GeminiManagedChatSession,
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

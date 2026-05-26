"""Chat message handling and streaming mixin."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEvent, HookEventType
from gobby.servers.chat_session_base import ChatSessionProtocol
from gobby.servers.websocket.chat._interaction_responses import ChatInteractionResponsesMixin
from gobby.servers.websocket.chat._message_ingress import ChatMessageIngressMixin
from gobby.servers.websocket.chat._pending_messages import ChatPendingMessagesMixin
from gobby.servers.websocket.chat._streaming import ChatStreamingMixin
from gobby.servers.websocket.chat_attachments import AttachmentSessionManager

if TYPE_CHECKING:
    from gobby.servers.websocket.chat_attachments import AttachmentDaemonConfig


class ChatMessagingMixin(
    ChatMessageIngressMixin,
    ChatStreamingMixin,
    ChatPendingMessagesMixin,
    ChatInteractionResponsesMixin,
):
    """Message processing methods for ChatMixin."""

    clients: dict[Any, dict[str, Any]]
    _chat_sessions: dict[str, ChatSessionProtocol]
    _active_chat_tasks: dict[str, asyncio.Task[None]]
    _pending_modes: dict[str, str]
    _pending_worktree_paths: dict[str, str]
    _pending_agents: dict[str, str]
    _pending_projects: dict[str, str]
    _pending_inject_contexts: dict[str, str]
    web_chat_session_registry: Any
    session_manager: AttachmentSessionManager | None
    daemon_config: AttachmentDaemonConfig | None

    if TYPE_CHECKING:

        async def _create_chat_session(
            self,
            conversation_id: str,
            model: str | None = None,
            project_id: str | None = None,
            resume_session_id: str | None = None,
            provider: str | None = None,
            reasoning_effort: str | None = None,
        ) -> ChatSessionProtocol: ...

        async def _send_error(
            self,
            websocket: Any,
            message: str,
            request_id: str | None = None,
            code: str = "ERROR",
        ) -> None: ...

        async def broadcast_session_event(
            self,
            event: str,
            session_id: str,
            **kwargs: Any,
        ) -> None: ...

        async def _fire_lifecycle(
            self,
            conversation_id: str,
            event_type: HookEventType,
            data: dict[str, Any],
        ) -> dict[str, Any] | None: ...

        async def _cancel_active_chat(self, conversation_id: str) -> None: ...

        async def _evaluate_blocking_webhooks(
            self,
            event: HookEvent,
        ) -> dict[str, Any] | None: ...

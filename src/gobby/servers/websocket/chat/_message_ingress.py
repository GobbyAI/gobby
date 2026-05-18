"""Ingress handling for web chat messages."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from gobby.servers.websocket.chat._attachment_preparation import (
    prepare_chat_attachments_or_error,
)
from gobby.servers.websocket.chat._message_validation import (
    ChatContent,
    as_optional_str,
    validate_chat_content,
)
from gobby.servers.websocket.chat_attachments import PreparedMessageAttachments

if TYPE_CHECKING:
    from gobby.servers.chat_session_base import ChatSessionProtocol

logger = logging.getLogger("gobby.servers.websocket.chat._messaging")

_SUPPORTED_PROVIDERS = {"claude", "codex", "gemini", "qwen", "droid"}


class ChatMessageIngressMixin:
    """Request ingress methods for ChatMixin."""

    clients: dict[Any, dict[str, Any]]
    _chat_sessions: dict[str, ChatSessionProtocol]
    _active_chat_tasks: dict[str, asyncio.Task[None]]
    _pending_inject_contexts: dict[str, str]
    web_chat_session_registry: Any

    if TYPE_CHECKING:

        async def _send_error(
            self,
            websocket: Any,
            message: str,
            request_id: str | None = None,
            code: str = "ERROR",
        ) -> None: ...

        async def _cancel_active_chat(self, conversation_id: str) -> None: ...

        async def _stream_chat_response(
            self,
            websocket: Any,
            conversation_id: str,
            content: ChatContent,
            model: str | None,
            request_id: str = "",
            project_id: str | None = None,
            inject_context: str | None = None,
            provider: str | None = None,
            reasoning_effort: str | None = None,
            tts_enabled: bool | None = None,
            attachments: PreparedMessageAttachments | None = None,
        ) -> None: ...

    async def _handle_chat_message(self, websocket: Any, data: dict[str, Any]) -> None:
        """Handle a chat_message payload from the web UI."""
        raw_content = data.get("content", "")
        content_blocks = data.get("content_blocks")
        conversation_id = data.get("conversation_id") or str(uuid4())
        model = data.get("model")
        request_id = data.get("request_id", "")
        message_id = as_optional_str(data.get("message_id"))
        project_id = data.get("project_id")
        provider = data.get("provider")
        reasoning_effort = data.get("reasoning_effort")

        if provider is not None and provider not in _SUPPORTED_PROVIDERS:
            await self._send_error(
                websocket, f"Invalid provider '{provider}'", request_id=request_id
            )
            return

        client_info = self.clients.get(websocket)
        if not client_info:
            logger.warning("Chat message from unregistered client")
            await self._send_error(
                websocket,
                "Client is not registered for chat messages",
                request_id=request_id,
                code="UNREGISTERED_CLIENT",
            )
            return

        prepared_attachments = await prepare_chat_attachments_or_error(
            self,
            websocket,
            data.get("attachments"),
            conversation_id=conversation_id,
            message_id=message_id,
            request_id=request_id,
            send_error=self._send_error,
        )
        if prepared_attachments is None:
            return

        validated_content, content_error = validate_chat_content(
            raw_content,
            content_blocks,
            has_attachments=bool(prepared_attachments.records),
        )
        if content_error:
            await self._send_error(websocket, content_error, request_id=request_id)
            return
        assert validated_content is not None

        client_info["conversation_id"] = conversation_id
        self._apply_tts_intent(conversation_id, data.get("tts_enabled"))
        inject_context = self._build_inject_context(
            conversation_id,
            data.get("inject_context"),
            prepared_attachments,
        )

        await self._cancel_active_chat(conversation_id)

        task = asyncio.create_task(
            self._stream_chat_response(
                websocket,
                conversation_id,
                validated_content,
                model,
                request_id,
                project_id,
                inject_context=inject_context,
                provider=provider,
                reasoning_effort=reasoning_effort,
                tts_enabled=(
                    data.get("tts_enabled") if isinstance(data.get("tts_enabled"), bool) else None
                ),
                attachments=prepared_attachments,
            )
        )
        task.add_done_callback(self._on_chat_task_done)
        registry = getattr(self, "web_chat_session_registry", None)
        if registry is not None:
            registry.track_active_task(conversation_id, task)
        else:
            self._active_chat_tasks[conversation_id] = task

    def _apply_tts_intent(self, conversation_id: str, tts_enabled: Any) -> None:
        """Apply the request's TTS toggle before stream scheduling."""
        voice_enabled = getattr(self, "_voice_enabled", None)
        if not isinstance(tts_enabled, bool) or not isinstance(voice_enabled, dict):
            return

        voice_enabled[conversation_id] = tts_enabled
        start_voice_warmup = getattr(self, "start_voice_warmup", None)
        if tts_enabled and callable(start_voice_warmup):
            try:
                start_voice_warmup(want_stt=False, want_tts=True)
            except Exception:
                logger.debug("TTS warmup start from chat intent failed", exc_info=True)

    def _build_inject_context(
        self,
        conversation_id: str,
        explicit_inject_context: Any,
        prepared_attachments: PreparedMessageAttachments,
    ) -> str | None:
        """Combine pending, explicit, and attachment context for SDK injection."""
        pending_inject_contexts = getattr(self, "_pending_inject_contexts", {})
        pending_inject_context = pending_inject_contexts.get(conversation_id)
        inject_parts = [
            value
            for value in [pending_inject_context, explicit_inject_context]
            if isinstance(value, str) and value.strip()
        ]
        if prepared_attachments.prompt_context:
            inject_parts.append(prepared_attachments.prompt_context)
        return "\n\n".join(inject_parts) if inject_parts else None

    def _on_chat_task_done(self, task: asyncio.Task[None]) -> None:
        """Log unhandled exceptions from chat tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Unhandled exception in chat task", exc_info=exc)

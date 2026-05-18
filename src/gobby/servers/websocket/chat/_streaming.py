"""Chat response streaming orchestration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from gobby.servers.chat_session_base import ChatSessionProtocol
from gobby.servers.websocket.chat._session import _resolve_git_branch
from gobby.servers.websocket.chat._stream_events import (
    ChatStreamEventHandler,
    ChatStreamEventState,
)
from gobby.servers.websocket.chat._stream_persistence import ChatStreamPersistence
from gobby.servers.websocket.chat._stream_transport import ChatStreamTransport
from gobby.servers.websocket.chat.content_blocks import AssistantContentBlocks
from gobby.servers.websocket.chat.local_openai_warmup import LocalOpenAIModelWarmupError
from gobby.servers.websocket.chat_attachments import PreparedMessageAttachments

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection

    from gobby.servers.websocket.chat._message_validation import ChatContent

logger = logging.getLogger(__name__)


class ChatStreamingMixin:
    """Stream ChatSession responses to web chat clients."""

    _chat_sessions: dict[str, ChatSessionProtocol]
    _active_chat_tasks: dict[str, asyncio.Task[None]]
    clients: dict[ServerConnection, dict[str, Any]]
    web_chat_session_registry: Any

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

    @staticmethod
    def _classify_chat_error(exc: Exception) -> tuple[str, str]:
        """Return (user_message, error_code) for a chat exception."""
        msg = str(exc).lower()
        if "rate_limit" in msg or "429" in msg:
            return "Rate limited by Claude API. Please wait and try again.", "RATE_LIMITED"
        if "auth" in msg or "401" in msg or "403" in msg or "api_key" in msg:
            return "Authentication error with Claude API.", "AUTH_ERROR"
        if isinstance(exc, TimeoutError) or "timeout" in msg:
            return "Request timed out. Please try again.", "TIMEOUT"
        if "connection" in msg:
            return "Connection to Claude API failed. Please try again.", "CONNECTION_ERROR"
        exc_type = type(exc).__name__
        return f"An error occurred ({exc_type}). Check daemon logs for details.", "INTERNAL_ERROR"

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
    ) -> None:
        """Stream a ChatSession response to the client. Runs as a cancellable task."""
        assistant_blocks = AssistantContentBlocks()
        state = ChatStreamEventState(assistant_message_id=f"assistant-{uuid4().hex[:12]}")
        transport = ChatStreamTransport(self, websocket, conversation_id, request_id)
        persistence = ChatStreamPersistence(self, conversation_id, assistant_blocks)
        tts_pipeline = self._create_optional_tts_pipeline(conversation_id, tts_enabled)
        event_handler = ChatStreamEventHandler(
            self,
            conversation_id,
            transport,
            persistence,
            assistant_blocks,
            state,
            tts_pipeline,
        )

        gen: AsyncIterator[Any] | None = None
        try:
            session = self._chat_sessions.get(conversation_id)
            if session is None:
                session = await self._start_chat_session(
                    conversation_id,
                    model,
                    project_id,
                    provider,
                    reasoning_effort,
                    state.assistant_message_id,
                    transport,
                    persistence,
                )
                if session is None:
                    return

            if reasoning_effort is not None:
                session.reasoning_effort = reasoning_effort

            await self._maybe_switch_model(
                session,
                conversation_id,
                model,
                state.assistant_message_id,
                transport,
                persistence,
            )
            session._tool_approval_callback = event_handler.emit_pending_approval
            self._clear_pending_inject_context(conversation_id)

            await persistence.persist_user_message(session, content, attachments)
            await persistence.set_status(session, "active")

            gen = session.send_message(self._content_with_inject_context(content, inject_context))
            async for event in gen:
                if not await event_handler.handle_event(event, session):
                    break

        except asyncio.CancelledError:
            try:
                await transport.send_direct(
                    transport.base_msg(
                        type="chat_stream",
                        message_id=state.assistant_message_id,
                        conversation_id=conversation_id,
                        content="",
                        done=True,
                        interrupted=True,
                    )
                )
            except (ConnectionClosed, ConnectionClosedError):
                pass

        except (ConnectionClosed, ConnectionClosedError):
            logger.debug("Client disconnected during chat stream for %s", conversation_id)

        except Exception as exc:
            logger.exception("Chat error for conversation %s", conversation_id)
            error_msg, error_code = self._classify_chat_error(exc)
            try:
                await transport.send_direct(
                    transport.base_msg(
                        type="chat_error",
                        message_id=state.assistant_message_id,
                        conversation_id=conversation_id,
                        error=error_msg,
                        code=error_code,
                    )
                )
            except (ConnectionClosed, ConnectionClosedError):
                pass

        finally:
            await self._close_generator(gen)
            self._clear_active_task(conversation_id)

    def _create_optional_tts_pipeline(
        self,
        conversation_id: str,
        tts_enabled: bool | None,
    ) -> Any:
        """Create a TTS pipeline for this response when voice mode is enabled."""
        try:
            if tts_enabled is not False and hasattr(self, "_create_tts_pipeline"):
                if tts_enabled is True:
                    voice_enabled = getattr(self, "_voice_enabled", None)
                    if isinstance(voice_enabled, dict):
                        voice_enabled[conversation_id] = True
                return self._create_tts_pipeline(conversation_id)
        except Exception:
            logger.debug("TTS pipeline creation failed", exc_info=True)
        return None

    async def _start_chat_session(
        self,
        conversation_id: str,
        model: str | None,
        project_id: str | None,
        provider: str | None,
        reasoning_effort: str | None,
        assistant_message_id: str,
        transport: ChatStreamTransport,
        persistence: ChatStreamPersistence,
    ) -> ChatSessionProtocol | None:
        """Create a ChatSession and send the request-scoped session_info frame."""
        try:
            session = await self._create_chat_session(
                conversation_id,
                model=model,
                project_id=project_id,
                provider=provider,
                reasoning_effort=reasoning_effort,
            )
            session_info_msg = transport.base_msg(
                type="session_info",
                conversation_id=conversation_id,
            )
            db_sid = getattr(session, "db_session_id", None)
            if db_sid:
                session_info_msg["db_session_id"] = db_sid
            ref = persistence.session_ref()
            if ref:
                session_info_msg["session_ref"] = ref
            branch, wt_path = await _resolve_git_branch(getattr(session, "project_path", None))
            if branch:
                session_info_msg["current_branch"] = branch
            if wt_path:
                session_info_msg["worktree_path"] = wt_path
            session_info_msg["agent_name"] = (
                getattr(session, "_pending_agent_name", None) or "default"
            )
            await transport.send_direct(session_info_msg)
            return session
        except Exception as exc:
            logger.exception(
                "Failed to start chat session for conversation %s",
                conversation_id,
            )
            error_message = "Failed to start chat session. Please try again."
            if isinstance(exc, LocalOpenAIModelWarmupError):
                error_message = str(exc)
            error_payload = transport.base_msg(
                type="chat_error",
                message_id=assistant_message_id,
                conversation_id=conversation_id,
                error=error_message,
            )
            if logger.isEnabledFor(logging.DEBUG):
                error_payload["error_detail"] = f"{type(exc).__name__}: {exc}"
            await transport.send_direct(error_payload)
            return None

    async def _maybe_switch_model(
        self,
        session: ChatSessionProtocol,
        conversation_id: str,
        model: str | None,
        assistant_message_id: str,
        transport: ChatStreamTransport,
        persistence: ChatStreamPersistence,
    ) -> None:
        """Switch an active session's model when the request asks for it."""
        if not (model and session.model and model != session.model):
            return

        old_model = session.model
        try:
            await session.switch_model(model)
            await persistence.persist_model_switch(session, model)
            await transport.send_direct(
                {
                    "type": "model_switched",
                    "conversation_id": conversation_id,
                    "old_model": old_model,
                    "new_model": model,
                }
            )
        except Exception as exc:
            logger.warning("Failed to switch model to %s: %s", model, exc)
            await transport.send_direct(
                transport.base_msg(
                    type="chat_error",
                    message_id=assistant_message_id,
                    conversation_id=conversation_id,
                    error="Failed to switch model. The previous model is still active.",
                )
            )

    @staticmethod
    def _content_with_inject_context(
        content: ChatContent,
        inject_context: str | None,
    ) -> ChatContent:
        """Append invisible gobby-context text for the backend SDK."""
        if not inject_context or not isinstance(inject_context, str):
            return content
        if isinstance(content, str):
            return f"{content}\n\n<gobby-context>\n{inject_context}\n</gobby-context>"
        return content + [
            {
                "type": "text",
                "text": f"\n\n<gobby-context>\n{inject_context}\n</gobby-context>",
            }
        ]

    def _clear_pending_inject_context(self, conversation_id: str) -> None:
        pending_inject_contexts = getattr(self, "_pending_inject_contexts", None)
        if isinstance(pending_inject_contexts, dict):
            pending_inject_contexts.pop(conversation_id, None)

    async def _close_generator(self, gen: AsyncIterator[Any] | None) -> None:
        """Close the async generator in the stream task to avoid anyio scope mismatches."""
        if gen is None:
            return
        aclose = getattr(gen, "aclose", None)
        if aclose is None:
            return
        try:
            await aclose()
        except Exception:
            pass

    def _clear_active_task(self, conversation_id: str) -> None:
        registry = getattr(self, "web_chat_session_registry", None)
        if registry is not None:
            registry.clear_active_task(conversation_id, asyncio.current_task())
        else:
            self._active_chat_tasks.pop(conversation_id, None)

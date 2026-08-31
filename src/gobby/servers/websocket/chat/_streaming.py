"""Chat response streaming orchestration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import httpx
from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from gobby.providers.capabilities.apply import apply_speed
from gobby.providers.capabilities.models import SpeedMode
from gobby.servers.chat_session_base import ChatSessionProtocol
from gobby.servers.chat_stream_transport import ChatStreamTransport
from gobby.servers.websocket.chat._session_binding import _normalize_runtime_chat_mode
from gobby.servers.websocket.chat._session_runtime import _resolve_git_branch
from gobby.servers.websocket.chat._stream_events import (
    ChatStreamEventHandler,
    ChatStreamEventState,
)
from gobby.servers.websocket.chat._stream_persistence import ChatStreamPersistence
from gobby.servers.websocket.chat._stream_transport import WebSocketChatStreamTransport
from gobby.servers.websocket.chat.content_blocks import AssistantContentBlocks
from gobby.servers.websocket.chat.local_openai_warmup import LocalOpenAIModelWarmupError
from gobby.servers.websocket.chat_attachments import PreparedMessageAttachments

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection

    from gobby.servers.websocket.chat._message_validation import ChatContent

logger = logging.getLogger(__name__)


async def send_session_info(
    session: ChatSessionProtocol,
    conversation_id: str,
    transport: ChatStreamTransport,
    *,
    session_ref: str | None,
) -> None:
    """Send the authoritative identity and capability frame for a chat session."""
    session_info_msg = transport.base_msg(
        type="session_info",
        conversation_id=conversation_id,
    )
    db_sid = getattr(session, "db_session_id", None)
    if db_sid:
        session_info_msg["db_session_id"] = db_sid
    if session_ref:
        session_info_msg["session_ref"] = session_ref
    branch, wt_path = await _resolve_git_branch(getattr(session, "project_path", None))
    if branch:
        session_info_msg["current_branch"] = branch
    if wt_path:
        session_info_msg["worktree_path"] = wt_path
    session_info_msg["agent_name"] = getattr(session, "_pending_agent_name", None) or "default"
    session_info_msg["plan_auto_switch"] = bool(getattr(session, "plan_auto_switch", True))
    session_chat_mode = getattr(session, "chat_mode", None)
    if isinstance(session_chat_mode, str) and session_chat_mode:
        session_info_msg["chat_mode"] = session_chat_mode
    available_commands = getattr(session, "available_commands", None)
    if isinstance(available_commands, list):
        session_info_msg["available_commands"] = available_commands
    await transport.send_direct(session_info_msg)


class ChatStreamingMixin:
    """Stream ChatSession responses to web chat clients."""

    _chat_sessions: dict[str, ChatSessionProtocol]
    _active_chat_tasks: dict[str, asyncio.Task[None]]
    _pending_agents: dict[str, str]
    _pending_modes: dict[str, str]
    _pending_projects: dict[str, str]
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
        chain: list[BaseException] = []
        seen: set[int] = set()
        current: BaseException | None = exc
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            chain.append(current)
            current = current.__cause__ or current.__context__

        status_code = None
        for error in chain:
            status_code = getattr(error, "status_code", None)
            if status_code is None:
                status_code = getattr(getattr(error, "response", None), "status_code", None)
            if status_code is not None:
                break
        if status_code == 429:
            return "Rate limited by Claude API. Please wait and try again.", "RATE_LIMITED"
        if status_code in {401, 403}:
            return "Authentication error with Claude API.", "AUTH_ERROR"
        if any(isinstance(error, (TimeoutError, httpx.TimeoutException)) for error in chain):
            return "Request timed out. Please try again.", "TIMEOUT"
        if any(
            isinstance(error, (ConnectionError, ConnectionClosed, httpx.NetworkError))
            for error in chain
        ):
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
        speed_mode: str = "standard",
        tts_enabled: bool | None = None,
        attachments: PreparedMessageAttachments | None = None,
    ) -> None:
        """Stream a ChatSession response to the client. Runs as a cancellable task."""
        transport = WebSocketChatStreamTransport(
            self,
            websocket,
            conversation_id,
            request_id,
        )
        await self._run_chat_turn(
            conversation_id=conversation_id,
            content=content,
            model=model,
            transport=transport,
            project_id=project_id,
            inject_context=inject_context,
            provider=provider,
            reasoning_effort=reasoning_effort,
            speed_mode=speed_mode,
            tts_enabled=tts_enabled,
            attachments=attachments,
            websocket=websocket,
        )

    async def _run_chat_turn(
        self,
        *,
        conversation_id: str,
        content: ChatContent,
        model: str | None,
        transport: ChatStreamTransport,
        project_id: str | None = None,
        inject_context: str | None = None,
        provider: str | None = None,
        reasoning_effort: str | None = None,
        speed_mode: str = "standard",
        tts_enabled: bool | None = None,
        attachments: PreparedMessageAttachments | None = None,
        websocket: Any | None = None,
    ) -> None:
        """Run one ChatSession turn through a caller-supplied stream transport."""
        assistant_blocks = AssistantContentBlocks()
        state = ChatStreamEventState(assistant_message_id=f"assistant-{uuid4().hex[:12]}")
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
        session: ChatSessionProtocol | None = None
        restore_model: str | None = None
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
            cast(Any, session)._chat_stream_transport = transport

            if websocket is not None:
                client_info = self.clients.get(websocket)
                if client_info is not None:
                    client_info["conversation_id"] = conversation_id
                    client_info["project_id"] = getattr(session, "project_id", None)

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
            request_parameters: Mapping[str, object] = {}
            base_model = session.model
            if base_model is not None:
                from gobby.app_context import get_app_context

                context = get_app_context()
                resolver = (
                    getattr(context, "provider_capability_resolver", None)
                    if context is not None
                    else None
                )
                if resolver is not None:
                    resolution = resolver.resolve_route(
                        session.provider,
                        base_model,
                        SpeedMode(speed_mode),
                        "app-server" if session.provider == "codex" else "tool-chat",
                    )
                    application = apply_speed(resolution, model=base_model)
                    request_parameters = application.request_parameters
                    if application.model is not None and application.model != base_model:
                        await session.switch_model(application.model)
                        restore_model = base_model
                elif speed_mode == "fast":
                    raise RuntimeError("Provider capability resolver unavailable")
            elif speed_mode == "fast":
                raise ValueError("Fast speed requires a resolved provider model")
            session._tool_approval_callback = event_handler.emit_pending_approval
            self._clear_pending_inject_context(conversation_id)

            await persistence.persist_user_message(session, content, attachments)
            await persistence.set_status(session, "active")

            message_content = self._content_with_inject_context(content, inject_context)
            if request_parameters:
                gen = session.send_message(
                    message_content,
                    request_parameters=request_parameters,
                )
            else:
                gen = session.send_message(message_content)
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
            if session is not None and restore_model is not None:
                try:
                    await session.switch_model(restore_model)
                except Exception:
                    logger.exception(
                        "Failed to restore request-scoped model %s for conversation %s",
                        restore_model,
                        conversation_id,
                    )
            if session is not None and not state.completed:
                await persistence.persist_current_assistant(session)
                await persistence.set_status(session, "paused")
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
            await send_session_info(
                session,
                conversation_id,
                transport,
                session_ref=persistence.session_ref(),
            )
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
            messaging_logger = logging.getLogger("gobby.servers.websocket.chat._messaging")
            if logger.isEnabledFor(logging.DEBUG) or messaging_logger.isEnabledFor(logging.DEBUG):
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

    async def reset_chat_session(self, conversation_id: str) -> bool:
        """Stop and remove one cached ChatSession runtime."""
        session = self._chat_sessions.pop(conversation_id, None)
        if session is None:
            return False
        await session.stop()
        return True

    async def configure_chat_session(
        self,
        conversation_id: str,
        *,
        chat_mode: str,
        agent_name: str,
        project_id: str,
    ) -> None:
        """Apply authoritative surface context before the next chat turn."""
        normalized_mode = _normalize_runtime_chat_mode(chat_mode)
        if normalized_mode not in {"normal", "bypass", "plan"}:
            raise ValueError(f"Unsupported chat mode: {chat_mode}")

        session = self._chat_sessions.get(conversation_id)
        if session is not None:
            current_agent = getattr(session, "_pending_agent_name", None) or "default"
            current_project = getattr(session, "project_id", None)
            current_mode = _normalize_runtime_chat_mode(getattr(session, "chat_mode", None))
            if (
                current_agent == agent_name
                and current_project == project_id
                and current_mode == normalized_mode
            ):
                return
            await self.reset_chat_session(conversation_id)

        self._pending_modes[conversation_id] = normalized_mode
        self._pending_agents[conversation_id] = agent_name
        self._pending_projects[conversation_id] = project_id

    def _clear_active_task(self, conversation_id: str) -> None:
        registry = getattr(self, "web_chat_session_registry", None)
        if registry is not None:
            registry.clear_active_task(conversation_id, asyncio.current_task())
        else:
            self._active_chat_tasks.pop(conversation_id, None)

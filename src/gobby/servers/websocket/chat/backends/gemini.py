"""Gemini daemon-owned web-chat backend."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gobby.adapters.gemini import GeminiAdapter
from gobby.adapters.gemini_acp_client import GeminiACPClient, StreamEvent
from gobby.agents.sandbox import SandboxConfig
from gobby.llm.claude_models import (
    ChatEvent,
    DoneEvent,
    TextChunk,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from gobby.servers.chat_session_helpers import PendingApproval, build_compaction_context
from gobby.servers.gemini_permissions import GeminiWebChatPermissionsMixin
from gobby.servers.websocket.chat.backends.base import (
    _BACKEND_START_TIMEOUT_SECONDS,
    ManagedChatSessionBase,
    ProviderBackendHealth,
    _error_message,
    _extract_text,
    _log_upstream_error_event,
)

logger = logging.getLogger(__name__)

# GeminiAdapter is stateless w.r.t. tool-name normalization; share one instance
# instead of constructing a new adapter on every tool call.
_GEMINI_TOOL_NAME_ADAPTER = GeminiAdapter()


@dataclass
class GeminiManagedChatSession(
    GeminiWebChatPermissionsMixin,
    ManagedChatSessionBase,
):
    """Web-chat session backed by the shared Gemini ACP backend."""

    provider: str = field(default="gemini", init=False)
    chat_mode: str = field(default="plan")
    _pending_question: dict[str, Any] | None = field(default=None, repr=False)
    _pending_answer_event: asyncio.Event | None = field(default=None, repr=False)
    _pending_answers: dict[str, str] | None = field(default=None, repr=False)
    _pending_approval: PendingApproval | None = field(default=None, repr=False)
    _pending_approval_event: asyncio.Event | None = field(default=None, repr=False)
    _pending_approval_decision: str | None = field(default=None, repr=False)
    _plan_approved: bool = field(default=False, repr=False)
    _plan_feedback: str | None = field(default=None, repr=False)
    _is_first_turn: bool = field(default=True, repr=False)

    def _web_chat_source(self) -> str:
        return f"{self.provider}_web_chat"

    def _provider_label(self) -> str:
        return self.provider.capitalize()

    def _tool_name_adapter(self) -> Any:
        return _GEMINI_TOOL_NAME_ADAPTER

    async def send_message(self, content: str | list[dict[str, Any]]) -> AsyncIterator[ChatEvent]:
        if not self._connected:
            await self.start(model=self._model)

        async with self._lock:
            self.last_activity = datetime.now(UTC)

            prompt = _extract_text(content) if isinstance(content, list) else content
            context_parts: list[str] = []

            if self._is_first_turn and self.system_prompt_override:
                context_parts.append(self.system_prompt_override)

            session_ref = (
                f"#{self.seq_num}" if self.seq_num else (self.db_session_id or self.conversation_id)
            )
            context_parts.append(
                build_compaction_context(
                    session_ref=session_ref,
                    project_id=self.project_id,
                    cwd=self.project_path,
                    source=self._web_chat_source(),
                )
            )

            plan_ctx = self._pop_plan_mode_context()
            if plan_ctx:
                context_parts.append(plan_ctx)
            deferred_context = self._consume_deferred_context()
            if deferred_context:
                context_parts.append(deferred_context)

            if self._on_before_agent:
                resp = await self._on_before_agent(
                    {
                        "prompt": prompt,
                        "source": self._web_chat_source(),
                    }
                )
                if resp and resp.get("context"):
                    context_parts.append(str(resp["context"]))

            full_prompt = prompt
            if context_parts:
                full_prompt = f"{'\n\n'.join(context_parts)}\n\n{prompt}"

            self._is_first_turn = False
            saw_content_delta = False
            pending_tool_calls: dict[str, dict[str, Any]] = {}

            try:
                async for stream_event in self._backend.send_message(self, full_prompt):
                    if stream_event.event_type == "init":
                        self.sdk_session_id = (
                            stream_event.data.get("session_id")
                            or stream_event.data.get("sessionId")
                            or self.sdk_session_id
                        )
                        model_name = stream_event.data.get("model")
                        if isinstance(model_name, str) and model_name:
                            self._model = model_name
                    elif stream_event.event_type == "content_delta":
                        saw_content_delta = True

                    if stream_event.event_type in {"tool_call", "call_tool"}:
                        chat_event = self._translate_event(
                            stream_event,
                            allow_message_fallback=not saw_content_delta,
                        )
                        if isinstance(chat_event, ToolCallEvent):
                            pending_tool_calls[chat_event.tool_call_id] = {
                                "tool_name": chat_event.tool_name,
                                "tool_input": chat_event.arguments,
                            }
                            await self._apply_pre_tool_lifecycle(
                                chat_event.tool_name,
                                chat_event.arguments,
                            )
                        if chat_event is not None:
                            yield chat_event
                        continue

                    if stream_event.event_type == "tool_result":
                        chat_event = self._translate_event(
                            stream_event,
                            allow_message_fallback=not saw_content_delta,
                        )
                        if isinstance(chat_event, ToolResultEvent):
                            pending = pending_tool_calls.pop(chat_event.tool_call_id, {})
                            tool_input = pending.get("tool_input")
                            tool_input_payload: dict[str, Any] = (
                                tool_input if isinstance(tool_input, dict) else {}
                            )
                            await self._apply_post_tool_lifecycle(
                                str(pending.get("tool_name", "")),
                                tool_input_payload,
                                chat_event.result if chat_event.success else chat_event.error,
                            )
                        if chat_event is not None:
                            yield chat_event
                        continue

                    chat_event = self._translate_event(
                        stream_event,
                        allow_message_fallback=not saw_content_delta,
                    )
                    if chat_event is not None:
                        yield chat_event

                yield DoneEvent(
                    tool_calls_count=0,
                    sdk_session_id=self.sdk_session_id,
                    context_window=self._resolve_context_window(),
                )
            except Exception as exc:
                logger.error(
                    "%s managed session %s error: %s",
                    self._provider_label(),
                    self.conversation_id,
                    exc,
                    exc_info=True,
                )
                yield TextChunk(content=f"Generation failed: {exc}")
                yield DoneEvent(tool_calls_count=0, context_window=self._resolve_context_window())

    def _translate_event(
        self,
        event: StreamEvent,
        *,
        allow_message_fallback: bool = True,
    ) -> ChatEvent | None:
        if event.event_type == "content_delta":
            content = event.data.get("content", "")
            if content:
                return TextChunk(content=content)
            return None

        if event.event_type == "message" and allow_message_fallback:
            role = event.data.get("role", "")
            content = event.data.get("content", "")
            if role == "assistant" and content:
                return TextChunk(content=content)
            return None

        if event.event_type == "thinking_delta":
            content = event.data.get("content", "")
            if content:
                return ThinkingEvent(content=content)
            return None

        if event.event_type == "tool_call" or event.event_type == "call_tool":
            tool_name = event.data.get("tool_name") or event.data.get("name")
            if not tool_name:
                return None

            normalized_name = self._tool_name_adapter().normalize_tool_name(tool_name)

            tool_input = event.data.get("tool_input") or event.data.get("arguments") or {}
            mcp_server = event.data.get("mcp_server") or event.data.get("server_name")
            call_id = event.data.get("call_id") or event.data.get("id") or "unknown"

            return ToolCallEvent(
                tool_call_id=call_id,
                tool_name=normalized_name,
                server_name=mcp_server or self.provider,
                arguments=tool_input,
            )

        if event.event_type == "tool_result":
            call_id = event.data.get("call_id") or event.data.get("id") or "unknown"
            success = event.data.get("success", True)
            result = event.data.get("result") or event.data.get("output")
            error = event.data.get("error")

            return ToolResultEvent(
                tool_call_id=call_id,
                success=success,
                result=result,
                error=error,
            )

        if event.event_type == "error":
            _log_upstream_error_event(self.provider, self, event.data)
            message = event.data.get("message", "Unknown error")
            return TextChunk(content=f"Error: {message}")

        return None

    async def interrupt(self) -> None:
        logger.debug(
            "%s interrupt requested for %s (no-op)",
            self._provider_label(),
            self.conversation_id,
        )


class GeminiWebChatBackend:
    """Shared daemon-owned Gemini ACP backend."""

    provider = "gemini"

    def __init__(
        self,
        *,
        client: GeminiACPClient | None = None,
        default_model: str | None = None,
        provider: str = "gemini",
        display_name: str = "Gemini",
        sandbox_config: SandboxConfig | None = None,
        start_timeout_seconds: float = _BACKEND_START_TIMEOUT_SECONDS,
    ) -> None:
        self.provider = provider
        self._display_name = display_name
        self._sandbox_config = sandbox_config
        self._start_timeout_seconds = start_timeout_seconds
        # Gemini CLI's ACP bootstrap currently hangs on macOS when launched with
        # the daemon's full-process Seatbelt flags. Keep daemon-owned ACP
        # startup unsandboxed and let Gemini's own tool sandboxing handle tool
        # execution inside interactive sessions.
        self._client = client or GeminiACPClient(
            cli_name=provider,
            display_name=display_name,
        )
        self._health = ProviderBackendHealth(provider=self.provider, available=False)
        self._default_model = default_model
        self._startup_task: asyncio.Task[None] | None = None

    async def _start_inner(self) -> None:
        if self._client.is_started:
            self._health = ProviderBackendHealth(provider=self.provider, available=True)
            return

        try:
            await asyncio.wait_for(
                self._client.start(
                    auto_session=False,
                    model=self._default_model,
                ),
                timeout=self._start_timeout_seconds,
            )
        except Exception as exc:
            startup_error = _error_message(exc)
            if isinstance(exc, TimeoutError) and startup_error == "TimeoutError":
                startup_error = (
                    f"Timed out starting {self._display_name} ACP backend after "
                    f"{self._start_timeout_seconds:.1f}s"
                )
            try:
                await self._client.stop()
            except Exception:
                logger.debug(
                    "%s backend cleanup after failed startup", self._display_name, exc_info=True
                )
            self._health = ProviderBackendHealth(
                provider=self.provider,
                available=False,
                startup_error=startup_error,
            )
            logger.warning("%s ACP backend startup failed: %s", self._display_name, startup_error)
            return

        self._health = ProviderBackendHealth(provider=self.provider, available=True)

    async def start(self, *, background: bool = False) -> None:
        if self._health.available:
            return
        if self._startup_task and not self._startup_task.done():
            if not background:
                await self._startup_task
            return

        self._startup_task = asyncio.create_task(self._start_inner())
        if not background:
            await self._startup_task

    async def stop(self) -> None:
        if self._startup_task and not self._startup_task.done():
            self._startup_task.cancel()
            try:
                await self._startup_task
            except asyncio.CancelledError:
                pass
        self._startup_task = None
        if self._client.is_started:
            await self._client.stop()
        self._health = ProviderBackendHealth(provider=self.provider, available=False)

    def health(self) -> ProviderBackendHealth:
        return self._health

    async def attach_session(
        self,
        session: GeminiManagedChatSession,
        *,
        model: str | None = None,
    ) -> None:
        if model:
            session._model = model
        elif not session._model:
            session._model = self._default_model

        await self.start()
        if not self._health.available:
            raise RuntimeError(
                self._health.startup_error or f"{self._display_name} backend unavailable"
            )

        session_id = session.sdk_session_id or session.resume_session_id
        cwd = session.project_path or "."
        if session_id:
            session_info = await self._client.load_session(
                session_id,
                model=session._model,
                cwd=cwd,
                reasoning_effort=session.reasoning_effort,
            )
        else:
            session_info = await self._client.create_session(
                model=session._model,
                cwd=cwd,
                reasoning_effort=session.reasoning_effort,
            )

        resolved_session_id = (
            session_info.get("sessionId")
            or session_info.get("session_id")
            or self._client.session_id
            or session_id
        )
        if isinstance(resolved_session_id, str) and resolved_session_id:
            session.sdk_session_id = resolved_session_id
        session._connected = True
        session.last_activity = datetime.now(UTC)

    async def detach_session(self, session: GeminiManagedChatSession) -> None:
        session._connected = False

    async def send_message(
        self,
        session: GeminiManagedChatSession,
        prompt: str,
    ) -> AsyncIterator[StreamEvent]:
        if not self._health.available:
            raise RuntimeError(
                self._health.startup_error or f"{self._display_name} backend unavailable"
            )
        if not session.sdk_session_id:
            raise RuntimeError(f"{self._display_name} session missing sessionId")

        async for event in self._client.send(
            prompt,
            session_id=session.sdk_session_id,
            model=session._model,
            reasoning_effort=session.reasoning_effort,
        ):
            yield event

    async def switch_model(self, session: GeminiManagedChatSession, new_model: str) -> None:
        session._model = new_model
        session._connected = False


__all__ = [
    "GeminiManagedChatSession",
    "GeminiWebChatBackend",
]

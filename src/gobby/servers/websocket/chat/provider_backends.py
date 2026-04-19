"""Daemon-owned provider backends and managed web-chat session wrappers."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gobby.adapters.codex_impl.client import CodexAppServerClient
from gobby.adapters.gemini import GeminiAdapter
from gobby.adapters.gemini_acp_client import GeminiACPClient, StreamEvent
from gobby.adapters.qwen import QwenAdapter
from gobby.agents.sandbox import (
    CodexSandboxResolver,
    SandboxConfig,
)
from gobby.llm.claude_models import (
    ChatEvent,
    DoneEvent,
    TextChunk,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from gobby.servers.chat_session import ChatSession
from gobby.servers.chat_session_helpers import (
    _BASH_WRITE_PATTERNS,
    _PLAN_FILE_PATTERN,
    PendingApproval,
    build_compaction_context,
)
from gobby.servers.gemini_permissions import GeminiWebChatPermissionsMixin
from gobby.servers.tool_approvals import (
    DEFAULT_GLOBAL_APPROVAL_RULES,
    find_out_of_repo_write_path,
    get_global_approval_rules,
    is_tool_auto_allowed,
    load_project_approval_rules_async,
    normalize_approved_tool_keys,
)
from gobby.servers.websocket.chat.local_openai_warmup import (
    ensure_qwen_local_openai_model_ready,
)
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.storage.config_store import ConfigStore

logger = logging.getLogger(__name__)

_BACKEND_START_TIMEOUT_SECONDS = 15.0
_CODEX_TRANSCRIPT_RETRY_ATTEMPTS = 5
_CODEX_TRANSCRIPT_RETRY_DELAY_SECONDS = 0.1
_CODEX_WEB_CHAT_APPROVAL_POLICY = "on-request"

# GeminiAdapter is stateless w.r.t. tool-name normalization; share one instance
# instead of constructing a new adapter on every tool call.
_GEMINI_TOOL_NAME_ADAPTER = GeminiAdapter()
_QWEN_TOOL_NAME_ADAPTER = QwenAdapter()


def _error_message(exc: BaseException) -> str:
    """Return a compact non-empty error string for startup health."""
    message = str(exc).strip()
    return message or exc.__class__.__name__


def _extract_text(content: str | list[dict[str, Any]]) -> str:
    """Extract a plain-text prompt from text blocks."""
    if isinstance(content, str):
        return content

    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _extract_codex_delta(params: dict[str, Any]) -> str:
    """Extract a text delta from Codex notification params."""
    delta = params.get("delta")
    if isinstance(delta, str) and delta:
        return delta

    item = params.get("item")
    if isinstance(item, dict):
        content = item.get("content")
        if isinstance(content, str) and content:
            return content
        if isinstance(content, list):
            chunks: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text") or block.get("delta") or ""
                    if isinstance(text, str) and text:
                        chunks.append(text)
            if chunks:
                return "".join(chunks)

    return ""


def _log_upstream_error_event(
    provider: str,
    session: ManagedChatSessionBase,
    payload: dict[str, Any],
) -> None:
    """Emit structured diagnostics for upstream managed-session errors."""
    message = payload.get("message", "Unknown error")
    logger.warning(
        "Managed %s upstream error for conversation=%s db_session=%s sdk_session=%s model=%s: %s",
        provider,
        session.conversation_id,
        session.db_session_id,
        session.sdk_session_id,
        session.model,
        message,
        extra={
            "provider": provider,
            "conversation_id": session.conversation_id,
            "db_session_id": session.db_session_id,
            "sdk_session_id": session.sdk_session_id,
            "model": session.model,
            "raw_upstream_event": payload,
        },
    )


@dataclass(slots=True)
class ProviderBackendHealth:
    """Availability state for a provider backend."""

    provider: str
    available: bool
    startup_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "available": self.available,
            "startup_error": self.startup_error,
        }


@dataclass
class ManagedChatSessionBase:
    """Common protocol fields for backend-managed web-chat sessions."""

    conversation_id: str
    provider: str
    chat_mode: str
    _backend: Any = field(default=None, repr=False)
    db_session_id: str | None = None
    seq_num: int | None = None
    project_id: str | None = None
    project_path: str | None = None
    message_index: int = 0
    system_prompt_override: str | None = None
    resume_session_id: str | None = None
    sdk_session_id: str | None = field(default=None, repr=False)
    last_activity: datetime = field(default_factory=lambda: datetime.now(UTC))
    _connected: bool = field(default=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _model: str | None = field(default=None, repr=False)
    reasoning_effort: str | None = field(default=None, repr=False)
    _tool_approval_config: Any | None = field(default=None, repr=False)
    _tool_approval_callback: Any | None = field(default=None, repr=False)
    _session_manager_ref: Any | None = field(default=None, repr=False)
    _on_mode_persist: Callable[[str], None] | None = field(default=None, repr=False)
    _on_approved_tools_persist: Callable[[set[str]], None] | None = field(default=None, repr=False)
    _approved_tools: set[str] = field(default_factory=set, repr=False)
    _plan_file_path: str | None = field(default=None, repr=False)
    _last_plan_content: str | None = field(default=None, repr=False)
    _pending_plan_content: str | None = field(default=None, repr=False)
    _pending_plan_allowed_prompts: list[str] | None = field(default=None, repr=False)
    _pending_post_plan_mode: str | None = field(default=None, repr=False)
    _pending_agent_name: str | None = field(default=None, repr=False)
    _plan_approval_completed: bool = field(default=False, repr=False)
    _context_window_overrides: dict[str, int] = field(default_factory=dict, repr=False)
    _accumulated_output_tokens: int = field(default=0, repr=False)
    _message_manager_source_session_id: str | None = field(default=None, repr=False)
    _needs_history_injection: bool = field(default=False, repr=False)
    _message_manager: Any | None = field(default=None, repr=False)
    _config: Any | None = field(default=None, repr=False)
    _deferred_contexts: list[str] = field(default_factory=list, repr=False)
    _on_before_agent: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = field(
        default=None, repr=False
    )
    _on_pre_tool: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = field(
        default=None, repr=False
    )
    _on_post_tool: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = field(
        default=None, repr=False
    )
    _on_pre_compact: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = field(
        default=None, repr=False
    )
    _on_stop: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = field(
        default=None, repr=False
    )
    _on_mode_changed: Callable[[str, str], Awaitable[None]] | None = field(default=None, repr=False)
    _on_plan_ready: Callable[[str | None, dict[str, Any]], Awaitable[None]] | None = field(
        default=None, repr=False
    )

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def model(self) -> str | None:
        return self._model

    async def start(self, model: str | None = None) -> None:
        await self._backend.attach_session(self, model=model)

    async def drain_pending_response(self) -> None:
        return None

    async def stop(self) -> None:
        await self._backend.detach_session(self)

    async def switch_model(self, new_model: str) -> None:
        await self._backend.switch_model(self, new_model)

    def set_chat_mode(self, mode: str) -> None:
        self.chat_mode = mode
        if self._on_mode_persist:
            try:
                self._on_mode_persist(mode)
            except Exception:
                logger.debug("Failed to persist chat mode", exc_info=True)

    async def sync_sdk_permission_mode(self) -> None:
        return None

    @property
    def has_pending_plan(self) -> bool:
        return False

    def provide_plan_decision(self, decision: str) -> None:
        return None

    def approve_plan(self) -> None:
        return None

    def set_plan_feedback(self, feedback: str) -> None:
        return None

    @property
    def has_pending_question(self) -> bool:
        return False

    def provide_answer(self, answers: dict[str, Any]) -> None:
        return None

    @property
    def has_pending_approval(self) -> bool:
        return False

    def provide_approval(self, decision: str) -> None:
        return None

    def _queue_deferred_context(self, response: dict[str, Any] | None) -> None:
        """Persist lifecycle context for the next prompt when it can't be injected live."""
        if not isinstance(response, dict):
            return
        context = response.get("context")
        if isinstance(context, str) and context.strip():
            self._deferred_contexts.append(context.strip())

    def _consume_deferred_context(self) -> str | None:
        """Return and clear queued lifecycle context fragments."""
        if not self._deferred_contexts:
            return None
        merged = "\n\n".join(part for part in self._deferred_contexts if part)
        self._deferred_contexts.clear()
        return merged or None

    async def _apply_pre_tool_lifecycle(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Run managed BEFORE_TOOL hooks and queue any returned context."""
        if self._on_pre_tool is None:
            return None
        response = await self._on_pre_tool({"tool_name": tool_name, "tool_input": tool_input})
        self._queue_deferred_context(response)
        return response

    async def _apply_post_tool_lifecycle(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_response: Any,
    ) -> dict[str, Any] | None:
        """Run managed AFTER_TOOL hooks and queue any returned context."""
        if self._on_post_tool is None:
            return None
        response = await self._on_post_tool(
            {
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_response": tool_response,
            }
        )
        self._queue_deferred_context(response)
        return response


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

    def _tool_name_adapter(self) -> GeminiAdapter:
        return _QWEN_TOOL_NAME_ADAPTER if self.provider == "qwen" else _GEMINI_TOOL_NAME_ADAPTER

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
                            await self._apply_post_tool_lifecycle(
                                str(pending.get("tool_name", "")),
                                (
                                    pending.get("tool_input")
                                    if isinstance(pending.get("tool_input"), dict)
                                    else {}
                                ),
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

                yield DoneEvent(tool_calls_count=0, sdk_session_id=self.sdk_session_id)
            except Exception as exc:
                logger.error(
                    "%s managed session %s error: %s",
                    self._provider_label(),
                    self.conversation_id,
                    exc,
                    exc_info=True,
                )
                yield TextChunk(content=f"Generation failed: {exc}")
                yield DoneEvent(tool_calls_count=0)

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

            # Normalize tool name for rule enforcement
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


@dataclass
class QwenManagedChatSession(GeminiManagedChatSession):
    """Web-chat session backed by the shared Qwen ACP backend."""

    provider: str = field(default="qwen", init=False)
    chat_mode: str = field(default="plan")


@dataclass
class CodexManagedChatSession(
    GeminiWebChatPermissionsMixin,
    ManagedChatSessionBase,
):
    """Web-chat session backed by the shared Codex app-server backend."""

    provider: str = field(default="codex", init=False)
    chat_mode: str = field(default="plan")
    _thread_id: str | None = field(default=None, repr=False)
    _turn_id: str | None = field(default=None, repr=False)
    _transcript_path: str | None = field(default=None, repr=False)
    _transcript_retry_attempts: int = field(
        default=_CODEX_TRANSCRIPT_RETRY_ATTEMPTS,
        repr=False,
    )
    _transcript_retry_delay_seconds: float = field(
        default=_CODEX_TRANSCRIPT_RETRY_DELAY_SECONDS,
        repr=False,
    )
    _pending_approval: PendingApproval | None = field(default=None, repr=False)
    _pending_approval_event: asyncio.Event | None = field(default=None, repr=False)
    _pending_approval_decision: str | None = field(default=None, repr=False)
    _plan_approved: bool = field(default=False, repr=False)
    _plan_feedback: str | None = field(default=None, repr=False)

    async def send_message(self, content: str | list[dict[str, Any]]) -> AsyncIterator[ChatEvent]:
        if not self._connected:
            await self.start(model=self._model)

        prompt = _extract_text(content)
        context_parts: list[str] = []
        if self.system_prompt_override:
            context_parts.append(self.system_prompt_override)

        session_ref = (
            f"#{self.seq_num}" if self.seq_num else (self.db_session_id or self.conversation_id)
        )
        context_parts.append(
            build_compaction_context(
                session_ref=session_ref,
                project_id=self.project_id,
                cwd=self.project_path,
                source="codex_web_chat",
            )
        )
        plan_ctx = self._pop_plan_mode_context()
        if plan_ctx:
            context_parts.append(plan_ctx)
        deferred_context = self._consume_deferred_context()
        if deferred_context:
            context_parts.append(deferred_context)

        if self._on_before_agent:
            resp = await self._on_before_agent({"prompt": prompt, "source": "codex_web_chat"})
            if resp and resp.get("context"):
                context_parts.append(str(resp["context"]))

        context_prefix = "\n\n".join(part for part in context_parts if part)

        async with self._lock:
            self.last_activity = datetime.now(UTC)
            self.message_index += 1
            async for event in self._backend.send_message(
                self,
                prompt,
                context_prefix=context_prefix or None,
            ):
                yield event

    async def interrupt(self) -> None:
        await self._backend.interrupt(self)

    async def _get_transcript_offset(self) -> int:
        if not self._transcript_path:
            return 0

        def _stat_size() -> int:
            try:
                return os.path.getsize(self._transcript_path or "")
            except OSError:
                return 0

        return await asyncio.to_thread(_stat_size)

    async def _get_transcript_assistant_text_since(self, offset: int) -> str:
        if not self._transcript_path:
            return ""

        def _read_assistant_text() -> str:
            try:
                with open(self._transcript_path or "", encoding="utf-8") as handle:
                    handle.seek(offset)
                    parser = CodexTranscriptParser(session_id=self._thread_id)
                    parsed = parser.parse_lines(handle.readlines())
            except OSError:
                return ""

            assistant_chunks = [
                message.content.strip()
                for message in parsed
                if message.role == "assistant" and message.content.strip()
            ]
            return "\n\n".join(assistant_chunks)

        for _ in range(self._transcript_retry_attempts):
            assistant_text = await asyncio.to_thread(_read_assistant_text)
            if assistant_text:
                return assistant_text
            await asyncio.sleep(self._transcript_retry_delay_seconds)
        return ""


class ClaudeWebChatBackend:
    """Trivial backend wrapper for Claude's existing ChatSession transport."""

    provider = "claude"

    def __init__(self, *, sandbox_config: SandboxConfig | None = None) -> None:
        self._sandbox_config = sandbox_config

    def create_session(self, conversation_id: str) -> ChatSession:
        return ChatSession(conversation_id=conversation_id, sandbox_config=self._sandbox_config)

    @staticmethod
    def health() -> ProviderBackendHealth:
        return ProviderBackendHealth(
            provider="claude",
            available=shutil.which("claude") is not None,
            startup_error=None if shutil.which("claude") else "claude CLI not found in PATH",
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
    ) -> None:
        self.provider = provider
        self._display_name = display_name
        self._sandbox_config = sandbox_config
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
                timeout=_BACKEND_START_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            startup_error = _error_message(exc)
            if isinstance(exc, TimeoutError) and startup_error == "TimeoutError":
                startup_error = (
                    f"Timed out starting {self._display_name} ACP backend after "
                    f"{_BACKEND_START_TIMEOUT_SECONDS:.1f}s"
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


class CodexWebChatBackend:
    """Shared daemon-owned Codex app-server backend."""

    provider = "codex"

    def __init__(
        self,
        *,
        client: CodexAppServerClient | None = None,
        transcript_retry_attempts: int = _CODEX_TRANSCRIPT_RETRY_ATTEMPTS,
        transcript_retry_delay_seconds: float = _CODEX_TRANSCRIPT_RETRY_DELAY_SECONDS,
        sandbox_config: SandboxConfig | None = None,
    ) -> None:
        self._client = client
        self._sandbox_config = sandbox_config
        self._health = ProviderBackendHealth(
            provider=self.provider,
            available=False,
            startup_error="Codex app-server client not configured",
        )
        self._sessions_by_thread: dict[str, CodexManagedChatSession] = {}
        self._startup_task: asyncio.Task[None] | None = None
        self.transcript_retry_attempts = transcript_retry_attempts
        self.transcript_retry_delay_seconds = transcript_retry_delay_seconds

    @property
    def client(self) -> CodexAppServerClient | None:
        """Expose the shared Codex app-server client for callers."""
        return self._client

    async def _start_inner(self) -> None:
        if self._client is None:
            self._health = ProviderBackendHealth(
                provider=self.provider,
                available=False,
                startup_error="codex CLI not found in PATH",
            )
            return

        if self._client.is_connected:
            self._health = ProviderBackendHealth(provider=self.provider, available=True)
            return

        try:
            await asyncio.wait_for(
                self._client.start(),
                timeout=_BACKEND_START_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            try:
                await self._client.stop()
            except Exception:
                logger.debug("Codex backend cleanup after failed startup", exc_info=True)
            self._health = ProviderBackendHealth(
                provider=self.provider,
                available=False,
                startup_error=_error_message(exc),
            )
            logger.warning("Codex backend startup failed: %s", exc)
            return

        self._client.register_approval_handler(self.handle_approval_request)
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
        if self._client and self._client.is_connected:
            await self._client.stop()
        self._health = ProviderBackendHealth(provider=self.provider, available=False)

    def health(self) -> ProviderBackendHealth:
        return self._health

    async def attach_session(
        self,
        session: CodexManagedChatSession,
        *,
        model: str | None = None,
    ) -> None:
        if model:
            session._model = model

        await self.start()
        if not self._health.available or self._client is None:
            raise RuntimeError(self._health.startup_error or "Codex backend unavailable")

        if session._thread_id:
            thread = await self._client.resume_thread(session._thread_id)
        elif session.resume_session_id:
            thread = await self._client.resume_thread(session.resume_session_id)
        else:
            thread = await self._client.start_thread(
                cwd=session.project_path or ".",
                model=session._model,
                approval_policy=_CODEX_WEB_CHAT_APPROVAL_POLICY,
                sandbox=CodexSandboxResolver.thread_sandbox_policy(self._sandbox_config),
            )

        session._thread_id = thread.id
        session.sdk_session_id = thread.id
        session._transcript_path = getattr(thread, "path", None)
        session._connected = True
        session.last_activity = datetime.now(UTC)
        self._sessions_by_thread[thread.id] = session

    async def detach_session(self, session: CodexManagedChatSession) -> None:
        session._connected = False
        session._turn_id = None
        if session._thread_id:
            self._sessions_by_thread.pop(session._thread_id, None)

    @staticmethod
    def _decline_response(method: str) -> dict[str, Any]:
        if method == "mcpServer/elicitation/request":
            return {"action": "cancel", "content": None, "_meta": None}
        return {"decision": "decline"}

    @staticmethod
    def _accept_response(method: str) -> dict[str, Any]:
        if method == "mcpServer/elicitation/request":
            return {"action": "accept", "content": None, "_meta": None}
        return {"decision": "accept"}

    @staticmethod
    def _extract_tool_args(payload: dict[str, Any]) -> dict[str, Any]:
        for key in ("tool_input", "toolArgs", "arguments", "input", "params"):
            value = payload.get(key)
            if isinstance(value, dict):
                return dict(value)
        return {}

    @staticmethod
    def _compose_mcp_tool_name(server_name: str, tool_name: str) -> str:
        """Return the canonical MCP tool name used by shared lifecycle logic."""
        return f"mcp__{server_name}__{tool_name}"

    @staticmethod
    def _extract_mcp_tool_name(message: Any) -> str | None:
        if not isinstance(message, str):
            return None
        match = re.search(r'run tool "([^"]+)"', message)
        if not match:
            return None
        tool_name = match.group(1).strip()
        return tool_name or None

    @staticmethod
    def _extract_completed_item_payload(params: dict[str, Any]) -> dict[str, Any]:
        """Return the best-effort tool item payload from an item/completed event."""
        item = params.get("item")
        if isinstance(item, dict):
            return item

        toolish_fields = {
            "type",
            "itemType",
            "name",
            "toolName",
            "tool_name",
            "arguments",
            "toolArgs",
            "tool_input",
            "input",
            "output",
            "result",
            "toolResult",
        }
        if any(field in params for field in toolish_fields):
            return params

        return {}

    @staticmethod
    def _looks_like_tool_item(item: dict[str, Any]) -> bool:
        """Identify completed Codex items that represent tool execution."""
        item_type = item.get("type") or item.get("itemType")
        if item_type in {"commandExecution", "fileChange", "mcpToolCall"}:
            return True

        if any(
            isinstance(item.get(tool_type), dict)
            for tool_type in ("commandExecution", "fileChange", "mcpToolCall")
        ):
            return True

        toolish_fields = (
            "name",
            "toolName",
            "tool_name",
            "arguments",
            "toolArgs",
            "tool_input",
            "input",
            "output",
            "result",
            "toolResult",
        )
        return any(field in item for field in toolish_fields)

    def _build_completed_tool_lifecycle_payload(
        self,
        params: dict[str, Any],
    ) -> tuple[str, dict[str, Any], Any] | None:
        """Normalize completed Codex tool items into managed lifecycle payloads."""
        item = self._extract_completed_item_payload(params)
        if not item or not self._looks_like_tool_item(item):
            return None

        item_type = item.get("type") or item.get("itemType") or ""
        nested_payload = item.get(item_type)

        item_data: dict[str, Any] = {}
        if isinstance(nested_payload, dict):
            item_data.update(nested_payload)
        item_data.update(item)

        raw_tool_name = (
            item_data.get("tool_name") or item_data.get("toolName") or item_data.get("name")
        )
        if not raw_tool_name and item_type == "mcpToolCall":
            server_name = item_data.get("server") or item_data.get("serverName")
            mcp_tool = item_data.get("tool") or item_data.get("toolName") or item_data.get("name")
            if (
                isinstance(server_name, str)
                and server_name
                and isinstance(mcp_tool, str)
                and mcp_tool
            ):
                raw_tool_name = self._compose_mcp_tool_name(server_name, mcp_tool)

        if isinstance(raw_tool_name, str) and raw_tool_name:
            from gobby.hooks.normalization import normalize_tool_fields

            normalized = normalize_tool_fields({"tool_name": raw_tool_name})
            tool_name = str(normalized.get("tool_name", raw_tool_name))
        elif item_type == "commandExecution":
            tool_name = "Bash"
        elif item_type == "fileChange":
            tool_name = "Write"
        else:
            return None

        tool_input = self._extract_tool_args(item_data)
        tool_response = (
            item_data.get("tool_response")
            or item_data.get("tool_result")
            or item_data.get("output")
            or item_data.get("result")
        )
        return tool_name, tool_input, tool_response

    def _translate_approval_request(
        self,
        method: str,
        params: dict[str, Any],
    ) -> tuple[str | None, dict[str, Any]]:
        if method == "mcpServer/elicitation/request":
            meta = params.get("_meta")
            if not isinstance(meta, dict) or meta.get("codex_approval_kind") != "mcp_tool_call":
                return None, {}
            server_name = params.get("serverName")
            tool_name = self._extract_mcp_tool_name(params.get("message"))
            elicitation_data = (
                dict(meta.get("tool_params", {}))
                if isinstance(meta.get("tool_params"), dict)
                else {}
            )
            if (
                isinstance(server_name, str)
                and server_name
                and isinstance(tool_name, str)
                and tool_name
            ):
                elicitation_data["server_name"] = server_name
                elicitation_data["tool_name"] = tool_name
                return "mcp__gobby__call_tool", elicitation_data
            return None, {}

        item_type = method.removeprefix("item/").removesuffix("/requestApproval")
        nested_payload = params.get(item_type)
        payload: dict[str, Any] = {}
        if isinstance(nested_payload, dict):
            payload.update(nested_payload)
        payload.update(params)

        if item_type == "commandExecution":
            command = payload.get("parsedCmd") or payload.get("command") or ""
            if isinstance(command, str):
                return "Bash", {"command": command}
            return "Bash", {}

        if item_type == "fileChange":
            changes = payload.get("changes")
            input_data: dict[str, Any] = {}
            if isinstance(changes, list):
                input_data["changes"] = changes
                if changes and isinstance(changes[0], dict):
                    first = changes[0]
                    for key in ("file_path", "path", "target_path"):
                        value = first.get(key)
                        if isinstance(value, str) and value:
                            input_data["file_path"] = value
                            break
            return "Write", input_data

        if item_type == "mcpToolCall":
            server_name = payload.get("serverName")
            raw_name = payload.get("tool_name") or payload.get("toolName") or payload.get("name")
            input_data = self._extract_tool_args(payload)
            if (
                isinstance(server_name, str)
                and server_name
                and isinstance(raw_name, str)
                and raw_name
            ):
                input_data["server_name"] = server_name
                input_data["tool_name"] = raw_name
                return "mcp__gobby__call_tool", input_data
            return None, input_data

        return None, {}

    @staticmethod
    def _global_rules_for_session(session: CodexManagedChatSession) -> list[str]:
        session_manager = getattr(session, "_session_manager_ref", None)
        db = getattr(session_manager, "db", None) if session_manager else None
        if db is None:
            return list(DEFAULT_GLOBAL_APPROVAL_RULES)
        return get_global_approval_rules(ConfigStore(db))

    async def handle_approval_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str) or not thread_id:
            return self._decline_response(method)

        session = self._sessions_by_thread.get(thread_id)
        if session is None:
            return self._decline_response(method)

        tool_name, input_data = self._translate_approval_request(method, params)
        if not tool_name:
            return self._decline_response(method)

        lifecycle_response = await session._apply_pre_tool_lifecycle(tool_name, input_data)
        if isinstance(lifecycle_response, dict) and lifecycle_response.get("decision") == "block":
            return self._decline_response(method)

        out_of_repo_path = find_out_of_repo_write_path(
            tool_name,
            input_data,
            project_path=session.project_path,
        )
        if out_of_repo_path:
            return self._decline_response(method)

        if session.chat_mode == "plan":
            if tool_name in {"Write", "Edit", "NotebookEdit"}:
                file_path = input_data.get("file_path", "")
                if (
                    not isinstance(file_path, str)
                    or not file_path
                    or not _PLAN_FILE_PATTERN.match(file_path)
                ):
                    return self._decline_response(method)
            elif tool_name == "Bash" and _BASH_WRITE_PATTERNS.search(
                str(input_data.get("command", ""))
            ):
                return self._decline_response(method)

        if session.chat_mode == "bypass":
            return self._accept_response(method)

        if is_tool_auto_allowed(
            tool_name,
            input_data,
            session_rules=normalize_approved_tool_keys(session._approved_tools),
            project_rules=await load_project_approval_rules_async(session.project_path),
            global_rules=self._global_rules_for_session(session),
        ):
            return self._accept_response(method)

        approval = await session._wait_for_tool_approval(tool_name, input_data)
        if isinstance(approval, dict) and approval.get("decision") == "accept":
            return self._accept_response(method)
        return self._decline_response(method)

    async def send_message(
        self,
        session: CodexManagedChatSession,
        prompt: str,
        *,
        context_prefix: str | None = None,
    ) -> AsyncIterator[ChatEvent]:
        if not self._health.available or self._client is None:
            raise RuntimeError(self._health.startup_error or "Codex backend unavailable")
        if not session._thread_id:
            raise RuntimeError("Codex session missing threadId")

        event_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        turn_completed = asyncio.Event()
        saw_text_output = False
        transcript_offset = await session._get_transcript_offset()

        def _matches(params: dict[str, Any]) -> bool:
            thread_id = params.get("threadId")
            if (
                isinstance(thread_id, str)
                and session._thread_id
                and thread_id != session._thread_id
            ):
                return False
            turn_id = params.get("turnId")
            if isinstance(turn_id, str) and session._turn_id and turn_id != session._turn_id:
                return False
            turn = params.get("turn")
            if isinstance(turn, dict):
                turn_identifier = turn.get("id")
                if (
                    isinstance(turn_identifier, str)
                    and session._turn_id
                    and turn_identifier != session._turn_id
                ):
                    return False
            return True

        def _enqueue(method: str, params: dict[str, Any]) -> None:
            if _matches(params):
                event_queue.put_nowait((method, params))

        event_methods = [
            "turn/started",
            "turn/completed",
            "thread/closed",
            "agent/messageDelta",
            "item/agentMessage/delta",
            "item/completed",
        ]
        for method in event_methods:
            self._client.add_notification_handler(method, _enqueue)

        try:
            turn = await self._client.start_turn(
                session._thread_id,
                prompt,
                context_prefix=context_prefix,
                model=session._model,
                effort=session.reasoning_effort,
            )
            session._turn_id = turn.id or session._turn_id

            while not turn_completed.is_set():
                try:
                    method, params = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                except TimeoutError:
                    continue

                if method in {"agent/messageDelta", "item/agentMessage/delta"}:
                    delta = _extract_codex_delta(params)
                    if delta:
                        saw_text_output = True
                        yield TextChunk(content=delta)
                    continue

                if method == "item/completed":
                    payload = self._build_completed_tool_lifecycle_payload(params)
                    if payload is not None:
                        tool_name, tool_input, tool_response = payload
                        await session._apply_post_tool_lifecycle(
                            tool_name,
                            tool_input,
                            tool_response,
                        )
                    continue

                if method == "turn/started":
                    turn_id = params.get("turnId")
                    if not turn_id:
                        turn_data = params.get("turn")
                        if isinstance(turn_data, dict):
                            turn_id = turn_data.get("id")
                    if isinstance(turn_id, str) and turn_id:
                        session._turn_id = turn_id
                    continue

                if method == "thread/closed":
                    session._turn_id = None
                    yield DoneEvent(tool_calls_count=0)
                    turn_completed.set()
                    continue

                if method == "turn/completed":
                    usage = params.get("usage", {})
                    if not isinstance(usage, dict):
                        usage = {}
                    session._turn_id = None
                    if not saw_text_output:
                        fallback_text = await session._get_transcript_assistant_text_since(
                            transcript_offset
                        )
                        if fallback_text:
                            yield TextChunk(content=fallback_text)

                    yield DoneEvent(
                        tool_calls_count=0,
                        input_tokens=int(usage.get("input_tokens", 0)),
                        output_tokens=int(usage.get("output_tokens", 0)),
                        sdk_session_id=session.sdk_session_id,
                    )
                    turn_completed.set()
        except Exception as exc:
            logger.error("Codex managed session %s error: %s", session.conversation_id, exc)
            yield TextChunk(content=f"Error: {exc}")
            yield DoneEvent(tool_calls_count=0, sdk_session_id=session.sdk_session_id)
        finally:
            for method in event_methods:
                self._client.remove_notification_handler(method, _enqueue)

    async def interrupt(self, session: CodexManagedChatSession) -> None:
        if not self._client or not session._thread_id or not session._turn_id:
            return
        await self._client.interrupt_turn(session._thread_id, session._turn_id)
        session._turn_id = None

    async def switch_model(self, session: CodexManagedChatSession, new_model: str) -> None:
        session._model = new_model

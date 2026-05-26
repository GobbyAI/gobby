"""Shared types and helpers for daemon-owned web-chat provider backends."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gobby.hooks.normalization import normalize_tool_fields
from gobby.llm.claude_models import resolve_context_window

logger = logging.getLogger(__name__)

_BACKEND_START_TIMEOUT_SECONDS = 15.0


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

    def add_output_tokens(self, tokens: int) -> int:
        self._accumulated_output_tokens += max(0, tokens)
        return self._accumulated_output_tokens

    def set_accumulated_output_tokens(self, tokens: int) -> None:
        self._accumulated_output_tokens = max(0, tokens)

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

    def _resolve_context_window(self) -> int | None:
        """Resolve context window metadata for this provider/model pair."""
        return resolve_context_window(
            self._model,
            None,
            overrides=self._context_window_overrides or None,
            provider=self.provider,
        )

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
        payload = {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_response": tool_response,
        }
        normalized_payload = normalize_tool_fields(dict(payload))
        for key in ("mcp_server", "mcp_tool"):
            value = normalized_payload.get(key)
            if isinstance(value, str) and value:
                payload[key] = value
        response = await self._on_post_tool(payload)
        self._queue_deferred_context(response)
        return response


__all__ = [
    "ManagedChatSessionBase",
    "ProviderBackendHealth",
]

"""
Protocol definition for polymorphic chat sessions.

ChatSession (Claude SDK) implements this protocol, allowing the
WebSocket layer to work polymorphically with future session types
(e.g. managed provider-backed sessions) without isinstance checks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from gobby.llm.claude_models import ChatEvent


@runtime_checkable
class ChatSessionProtocol(Protocol):
    """Shared interface for chat session implementations."""

    # Identity
    conversation_id: str
    db_session_id: str | None
    seq_num: int | None
    project_id: str | None
    project_path: str | None
    message_index: int
    chat_mode: str
    system_prompt_override: str | None
    resume_session_id: str | None
    last_activity: datetime
    provider: str
    reasoning_effort: str | None

    # Lifecycle callbacks
    _on_before_agent: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None
    _on_pre_tool: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None
    _on_post_tool: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None
    _on_pre_compact: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None
    _on_stop: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None
    _on_mode_changed: Callable[[str, str], Awaitable[None]] | None
    _on_plan_ready: Callable[[str | None, dict[str, Any]], Awaitable[None]] | None

    # Optional attrs set dynamically by WebSocket session control
    _tool_approval_config: Any
    _tool_approval_callback: Callable[..., Any] | None
    _session_manager_ref: Any
    _on_mode_persist: Callable[[str], None] | None
    _on_approved_tools_persist: Callable[[set[str]], None] | None
    _approved_tools: set[str]
    _plan_file_path: str | None
    _pending_agent_name: str | None
    _plan_approval_completed: bool
    _context_window_overrides: dict[str, int]
    _accumulated_output_tokens: int
    _message_manager_source_session_id: str | None
    _needs_history_injection: bool
    _message_manager: Any
    _config: Any

    @property
    def is_connected(self) -> bool: ...

    @property
    def model(self) -> str | None: ...

    async def start(self, model: str | None = None) -> None: ...

    def send_message(self, content: str | list[dict[str, Any]]) -> AsyncIterator[ChatEvent]: ...

    async def interrupt(self) -> None: ...

    async def drain_pending_response(self) -> None: ...

    async def stop(self) -> None: ...

    async def switch_model(self, new_model: str) -> None: ...

    def set_chat_mode(self, mode: str) -> None: ...

    async def sync_sdk_permission_mode(self) -> None: ...

    # Plan approval
    @property
    def has_pending_plan(self) -> bool: ...

    def provide_plan_decision(self, decision: str) -> None: ...

    def approve_plan(self) -> None: ...

    def set_plan_feedback(self, feedback: str) -> None: ...

    # Ask-user / tool approval
    @property
    def has_pending_question(self) -> bool: ...

    def provide_answer(self, answers: dict[str, Any]) -> None: ...

    @property
    def has_pending_approval(self) -> bool: ...

    def provide_approval(self, decision: str) -> None: ...

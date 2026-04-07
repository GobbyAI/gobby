"""
Codex CLI-backed web chat session via app-server protocol.

CodexCLIChatSession wraps CodexAppServerClient with the ChatSessionProtocol
interface, normalizing Codex app-server notifications into the standard
ChatEvent stream (TextChunk, ToolCallEvent, ToolResultEvent, DoneEvent).

Mirrors the structure of ChatSession (Claude SDK) but uses the Codex
subprocess and thread model.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gobby.adapters.codex_impl.adapter import CodexAdapter
from gobby.adapters.codex_impl.client import CodexAppServerClient
from gobby.adapters.codex_impl.types import CodexThread
from gobby.llm.claude_models import (
    ChatEvent,
    DoneEvent,
    TextChunk,
    ToolCallEvent,
    ToolResultEvent,
)
from gobby.servers.chat_session_helpers import (
    PendingApproval,
    build_compaction_context,
)
from gobby.servers.codex_chat_session_permissions import CodexChatSessionPermissionsMixin

logger = logging.getLogger(__name__)


@dataclass
class CodexCLIChatSession(CodexChatSessionPermissionsMixin):
    """Codex CLI-backed web chat session via app-server protocol.

    Implements ChatSessionProtocol by wrapping CodexAppServerClient.
    Normalizes Codex app-server notifications (thread.started, turn.started,
    item/agentMessage/delta, item.completed, turn.completed) into the
    standard ChatEvent stream.
    """

    provider: str = field(default="codex", init=False)

    # --- ChatSessionProtocol identity ---
    conversation_id: str = ""
    db_session_id: str | None = field(default=None)
    seq_num: int | None = field(default=None)
    project_id: str | None = field(default=None)
    project_path: str | None = field(default=None)
    message_index: int = field(default=0)
    last_activity: datetime = field(default_factory=lambda: datetime.now(UTC))
    chat_mode: str = field(default="plan")
    system_prompt_override: str | None = field(default=None)
    resume_session_id: str | None = field(default=None)
    sdk_session_id: str | None = field(default=None, repr=False)

    # --- Codex internals ---
    _client: CodexAppServerClient | None = field(default=None, repr=False)
    _adapter: CodexAdapter | None = field(default=None, repr=False)
    _thread: CodexThread | None = field(default=None, repr=False)
    _thread_id: str | None = field(default=None, repr=False)
    _current_turn_id: str | None = field(default=None, repr=False)
    _connected: bool = field(default=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _model: str | None = field(default=None, repr=False)

    # --- Pending state ---
    _pending_question: dict[str, Any] | None = field(default=None, repr=False)
    _pending_answer_event: asyncio.Event | None = field(default=None, repr=False)
    _pending_answers: dict[str, str] | None = field(default=None, repr=False)
    _pending_approval: PendingApproval | None = field(default=None, repr=False)
    _pending_approval_event: asyncio.Event | None = field(default=None, repr=False)
    _pending_approval_decision: str | None = field(default=None, repr=False)
    _approved_tools: set[str] = field(default_factory=set, repr=False)
    _plan_approved: bool = field(default=False, repr=False)
    _plan_feedback: str | None = field(default=None, repr=False)
    _plan_file_path: str | None = field(default=None, repr=False)
    _tool_approval_config: Any | None = field(default=None, repr=False)
    _tool_approval_callback: Any | None = field(default=None, repr=False)
    _on_approved_tools_persist: Callable[[set[str]], None] | None = field(default=None, repr=False)
    _pending_agent_name: str | None = field(default=None, repr=False)
    _session_manager_ref: Any | None = field(default=None, repr=False)
    _plan_approval_completed: bool = field(default=False, repr=False)
    _context_window_overrides: dict[str, int] = field(default_factory=dict, repr=False)
    _accumulated_output_tokens: int = field(default=0, repr=False)
    _accumulated_cost_usd: float = field(default=0.0, repr=False)
    _message_manager_source_session_id: str | None = field(default=None, repr=False)
    _needs_history_injection: bool = field(default=False, repr=False)
    _message_manager: Any | None = field(default=None, repr=False)
    _is_first_turn: bool = field(default=True, repr=False)

    # --- Lifecycle callbacks ---
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
    _on_mode_persist: Callable[[str], None] | None = field(default=None, repr=False)
    _on_plan_ready: Callable[[str | None, dict[str, Any]], Awaitable[None]] | None = field(
        default=None, repr=False
    )

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self, model: str | None = None) -> None:
        """Start the CodexAppServerClient and initialise a thread."""
        if not CodexAdapter.is_codex_available():
            raise RuntimeError("Codex CLI not found in PATH")

        self._model = model

        # Resolve CWD
        cwd = self.project_path or str(Path.cwd())

        # Create client and adapter
        self._client = CodexAppServerClient()
        self._adapter = CodexAdapter()

        # Register approval handler before connecting
        self._client.register_approval_handler(self._handle_codex_approval)

        await self._client.start()

        # Start or resume thread
        if self.resume_session_id:
            self._thread = await self._client.resume_thread(self.resume_session_id)
        elif self._thread_id:
            self._thread = await self._client.resume_thread(self._thread_id)
        else:
            self._thread = await self._client.start_thread(
                cwd=cwd,
                model=model,
            )

        self.sdk_session_id = self._thread.id
        self._connected = True
        self.last_activity = datetime.now(UTC)
        logger.debug(
            f"CodexCLIChatSession {self.conversation_id} started (thread={self._thread.id})"
        )

    async def stop(self) -> None:
        """Stop the CodexAppServerClient and clean up."""
        if self._adapter:
            self._adapter = None
        if self._client:
            try:
                await self._client.stop()
            except Exception as e:
                logger.debug(
                    f"CodexCLIChatSession {self.conversation_id} stop error (expected): {e}",
                )
            finally:
                self._client = None
                self._connected = False
                self._thread = None
                logger.debug(f"CodexCLIChatSession {self.conversation_id} stopped")

    # ------------------------------------------------------------------ #
    # Messaging
    # ------------------------------------------------------------------ #

    async def send_message(self, content: str | list[dict[str, Any]]) -> AsyncIterator[ChatEvent]:
        """Send a user message and yield streaming ChatEvent instances.

        Translates Codex app-server notifications into the standard ChatEvent
        types (TextChunk, ToolCallEvent, ToolResultEvent, DoneEvent).
        """
        if not self._client or not self._connected or not self._thread:
            raise RuntimeError("CodexCLIChatSession not connected. Call start() first.")

        async with self._lock:
            self.last_activity = datetime.now(UTC)

            # Extract prompt text
            if isinstance(content, list):
                prompt_parts = [
                    block.get("text", "") for block in content if block.get("type") == "text"
                ]
                prompt = "\n".join(prompt_parts) or str(content)
            else:
                prompt = content

            # Build context prefix
            context_parts: list[str] = []

            # System prompt on first turn
            if self._is_first_turn and self.system_prompt_override:
                context_parts.append(self.system_prompt_override)

            # Environment context
            session_ref = (
                f"#{self.seq_num}" if self.seq_num else (self.db_session_id or self.conversation_id)
            )
            context_parts.append(
                build_compaction_context(
                    session_ref=session_ref,
                    project_id=self.project_id,
                    cwd=self.project_path,
                    source="codex_cli_web_chat",
                )
            )

            # Plan mode context
            plan_ctx = self._consume_plan_mode_context()
            if plan_ctx:
                context_parts.append(plan_ctx)

            # Fire before_agent callback
            if self._on_before_agent:
                resp = await self._on_before_agent(
                    {"prompt": prompt, "source": "codex_cli_web_chat"}
                )
                if resp and resp.get("context"):
                    context_parts.append(resp["context"])

            context_prefix = "\n\n".join(context_parts) if context_parts else None

            # Set up event collection
            tool_calls_count = 0
            turn_completed = asyncio.Event()
            event_queue: asyncio.Queue[ChatEvent | None] = asyncio.Queue()

            def _on_fire_and_forget_error(task: asyncio.Task[Any]) -> None:
                if not task.cancelled() and task.exception():
                    logger.error(f"Fire-and-forget task failed: {task.exception()}")

            def _on_delta(method: str, params: dict[str, Any]) -> None:
                delta = params.get("delta", "")
                if delta:
                    event_queue.put_nowait(TextChunk(content=delta))

            def _on_item_started(method: str, params: dict[str, Any]) -> None:
                nonlocal tool_calls_count
                item = params.get("item", {})
                item_type = item.get("type", "")
                if item_type in CodexAdapter.TOOL_ITEM_TYPES:
                    tool_calls_count += 1
                    tool_name = (
                        self._adapter.normalize_tool_name(item_type) if self._adapter else item_type
                    )
                    event_queue.put_nowait(
                        ToolCallEvent(
                            tool_call_id=item.get("id", ""),
                            tool_name=tool_name,
                            server_name="",
                            arguments=item.get("metadata", {}),
                        )
                    )

            def _on_item_completed(method: str, params: dict[str, Any]) -> None:
                item = params.get("item", {})
                item_type = item.get("type", "")
                if item_type in CodexAdapter.TOOL_ITEM_TYPES:
                    tool_name = (
                        self._adapter.normalize_tool_name(item_type) if self._adapter else item_type
                    )
                    content_text = item.get("content", "") or item.get("output", "")
                    is_error = item.get("status") == "failed"
                    event_queue.put_nowait(
                        ToolResultEvent(
                            tool_call_id=item.get("id", ""),
                            success=not is_error,
                            result=str(content_text) if not is_error else None,
                            error=str(content_text) if is_error else None,
                        )
                    )
                    # Fire post-tool callback
                    if self._on_post_tool:
                        post_tool_awaitable = self._on_post_tool(
                            {
                                "tool_name": tool_name,
                                "tool_input": item.get("metadata", {}),
                            }
                        )
                        t = asyncio.ensure_future(post_tool_awaitable)
                        t.add_done_callback(_on_fire_and_forget_error)

            def _on_turn_completed(method: str, params: dict[str, Any]) -> None:
                turn_completed.set()

            # Register notification handlers
            self._client.add_notification_handler("item/agentMessage/delta", _on_delta)
            self._client.add_notification_handler("item/started", _on_item_started)
            self._client.add_notification_handler("item/completed", _on_item_completed)
            self._client.add_notification_handler("turn/completed", _on_turn_completed)

            try:
                # Start the turn
                turn = await self._client.start_turn(
                    thread_id=self._thread.id,
                    prompt=prompt,
                    context_prefix=context_prefix,
                )
                self._current_turn_id = turn.id
                self._is_first_turn = False

                # Stream events until turn completes
                while not turn_completed.is_set():
                    try:
                        event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                        if event is not None:
                            yield event
                    except TimeoutError:
                        continue

                # Drain remaining events
                while not event_queue.empty():
                    event = event_queue.get_nowait()
                    if event is not None:
                        yield event

                # Emit DoneEvent
                yield DoneEvent(
                    tool_calls_count=tool_calls_count,
                    sdk_session_id=self.sdk_session_id,
                )

            except Exception as e:
                logger.error(
                    f"CodexCLIChatSession {self.conversation_id} error: {e}",
                    exc_info=True,
                )
                yield TextChunk(content=f"Generation failed: {e}")
                yield DoneEvent(tool_calls_count=tool_calls_count)
            finally:
                self._current_turn_id = None
                self._client.remove_notification_handler("item/agentMessage/delta", _on_delta)
                self._client.remove_notification_handler("item/started", _on_item_started)
                self._client.remove_notification_handler("item/completed", _on_item_completed)
                self._client.remove_notification_handler("turn/completed", _on_turn_completed)

    # ------------------------------------------------------------------ #
    # Approval handler
    # ------------------------------------------------------------------ #

    async def _handle_codex_approval(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Handle approval requests from Codex.

        Normalises tool name, checks plan mode blocking, fires lifecycle
        hooks, and returns {"decision": "accept"/"decline"}.
        """
        item_type = params.get("type", method.split("/")[-1] if "/" in method else method)
        tool_name = self._adapter.normalize_tool_name(item_type) if self._adapter else item_type
        input_data = params.get("arguments", params.get("metadata", {}))
        return await self._check_tool_permission(tool_name, input_data)

    # ------------------------------------------------------------------ #
    # Interrupt / drain
    # ------------------------------------------------------------------ #

    async def interrupt(self) -> None:
        """Interrupt the current turn."""
        if self._client and self._connected and self._thread and self._current_turn_id:
            try:
                await self._client.interrupt_turn(self._thread.id, self._current_turn_id)
            except Exception as e:
                logger.warning(f"CodexCLIChatSession {self.conversation_id} interrupt error: {e}")

    async def drain_pending_response(self) -> None:
        """No-op -- Codex app-server protocol has no stale buffer to drain."""

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #

    @property
    def model(self) -> str | None:
        """The current model for this session."""
        return self._model

    @property
    def is_connected(self) -> bool:
        """Whether the session is currently connected."""
        return self._connected

    async def switch_model(self, new_model: str) -> None:
        """Switch model for the next turn."""
        self._model = new_model

    async def sync_sdk_permission_mode(self) -> None:
        """No-op -- permission mode is enforced via approval callbacks."""

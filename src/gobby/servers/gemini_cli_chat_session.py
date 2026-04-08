"""
Chat session backed by GeminiACPClient for persistent multi-turn conversations.

Each GeminiCLIChatSession wraps a GeminiACPClient that manages a
``gemini --acp`` subprocess. Sessions are keyed by conversation_id
(stable across WebSocket reconnections) rather than ephemeral client_id.

Uses the same ChatEvent types as ChatSession (TextChunk, ToolCallEvent,
ToolResultEvent, DoneEvent) so the WebSocket layer is polymorphic.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gobby.adapters.gemini_acp_client import GeminiACPClient, StreamEvent
from gobby.llm.claude_models import (
    ChatEvent,
    DoneEvent,
    TextChunk,
)
from gobby.servers.chat_session_helpers import (
    PendingApproval,
    build_compaction_context,
)
from gobby.servers.gemini_cli_chat_session_permissions import (
    GeminiCLIChatSessionPermissionsMixin,
)

logger = logging.getLogger(__name__)


@dataclass
class GeminiCLIChatSession(GeminiCLIChatSessionPermissionsMixin):
    """
    A persistent chat session backed by GeminiACPClient.

    Maintains conversation context across messages via Gemini ACP.
    Sessions survive WebSocket disconnections and are identified by
    conversation_id.
    """

    provider: str = field(default="gemini", init=False)

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

    # Gemini internals
    _client: GeminiACPClient | None = field(default=None, repr=False)
    _connected: bool = field(default=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _model: str | None = field(default=None, repr=False)

    # Pending state
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
    _config: Any | None = field(default=None, repr=False)
    _is_first_turn: bool = field(default=True, repr=False)

    # Lifecycle callbacks -- set by ChatMixin to bridge hooks to workflow engine
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

    async def start(self, model: str | None = None) -> None:
        """Start the GeminiACPClient subprocess.

        Args:
            model: Optional model name (stored but not passed to Gemini ACP).

        Raises:
            FileNotFoundError: If the Gemini CLI binary is not found.
        """
        self._model = model

        # Resolve CWD
        if self.project_path:
            cwd = self.project_path
        else:
            cwd = str(Path.cwd())

        # Create and start the ACP client
        self._client = GeminiACPClient(cwd=cwd)

        await self._client.start(session_id=self.resume_session_id)

        self._connected = True
        self.last_activity = datetime.now(UTC)
        logger.debug(f"GeminiCLIChatSession {self.conversation_id} started (cwd={cwd})")

    async def send_message(self, content: str | list[dict[str, Any]]) -> AsyncIterator[ChatEvent]:
        """Send a user message and yield streaming ChatEvent instances.

        Translates Gemini ACP StreamEvent objects to the same ChatEvent
        types that ChatSession yields (TextChunk, DoneEvent).

        Args:
            content: Plain text or a list of content blocks.

        Yields:
            ChatEvent instances matching the ChatSession protocol.

        Raises:
            RuntimeError: If the session is not started.
        """
        if not self._client or not self._connected:
            raise RuntimeError("GeminiCLIChatSession not connected. Call start() first.")

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
                    source="gemini_web_chat",
                )
            )

            # Plan mode context
            plan_ctx = self._pop_plan_mode_context()
            if plan_ctx:
                context_parts.append(plan_ctx)

            # Fire before_agent callback
            if self._on_before_agent:
                resp = await self._on_before_agent(
                    {
                        "prompt": prompt,
                        "source": "gemini_web_chat",
                    }
                )
                if resp and resp.get("context"):
                    context_parts.append(resp["context"])

            # Prepend context to prompt if we have any
            if context_parts:
                context_prefix = "\n\n".join(context_parts)
                full_prompt = f"{context_prefix}\n\n{prompt}"
            else:
                full_prompt = prompt

            self._is_first_turn = False

            try:
                async for stream_event in self._client.send(full_prompt):
                    chat_event = self._translate_event(stream_event)
                    if chat_event is not None:
                        yield chat_event

                # Emit final DoneEvent
                yield DoneEvent(
                    tool_calls_count=0,
                    sdk_session_id=self.sdk_session_id,
                )

            except Exception as e:
                logger.error(
                    f"GeminiCLIChatSession {self.conversation_id} error: {e}",
                    exc_info=True,
                )
                yield TextChunk(content=f"Generation failed: {e}")
                yield DoneEvent(tool_calls_count=0)

    @staticmethod
    def _translate_event(event: StreamEvent) -> ChatEvent | None:
        """Translate a Gemini ACP StreamEvent to a ChatEvent.

        Args:
            event: A normalized StreamEvent from GeminiACPClient.

        Returns:
            A ChatEvent, or None if the event should be skipped.
        """
        if event.event_type == "content_delta":
            content = event.data.get("content", "")
            if content:
                return TextChunk(content=content)
            return None

        if event.event_type == "error":
            msg = event.data.get("message", "Unknown error")
            return TextChunk(content=f"Error: {msg}")

        # init, result, message (non-delta) are not emitted as ChatEvents
        return None

    async def interrupt(self) -> None:
        """Interrupt the current response stream.

        Gemini ACP does not support mid-stream interruption, so this
        is a best-effort stop of the subprocess.
        """
        # No interrupt support in ACP protocol; stop and restart would
        # be needed for hard interruption. Log and no-op for now.
        logger.debug(f"GeminiCLIChatSession {self.conversation_id} interrupt requested (no-op)")

    async def drain_pending_response(self) -> None:
        """Drain stale events after interrupt. Gemini ACP handles cleanup internally."""
        pass

    async def stop(self) -> None:
        """Stop the GeminiACPClient and clean up."""
        if self._client:
            try:
                await self._client.stop()
            except Exception as e:
                logger.debug(
                    f"GeminiCLIChatSession {self.conversation_id} stop error (expected): {e}",
                )
            finally:
                self._client = None
                self._connected = False
                logger.debug(f"GeminiCLIChatSession {self.conversation_id} stopped")

    @property
    def model(self) -> str | None:
        """The current model for this session."""
        return self._model

    async def switch_model(self, new_model: str) -> None:
        """Switch model. Applied on the next send() call."""
        self._model = new_model

    @property
    def is_connected(self) -> bool:
        """Whether the session is currently connected."""
        return self._connected

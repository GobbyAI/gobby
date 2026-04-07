"""
Chat session backed by ClaudeCLI for persistent multi-turn conversations.

Each CLIChatSession wraps a ClaudeCLI subprocess session that streams
JSON events from ``claude --output-format stream-json``. Sessions are
keyed by conversation_id (stable across WebSocket reconnections) rather
than ephemeral client_id.

Uses the same ChatEvent types as ChatSession / CodexChatSession
(TextChunk, ThinkingEvent, DoneEvent) so the WebSocket layer is
polymorphic.

Unlike the SDK-backed ChatSession, lifecycle hooks (pre_tool, post_tool,
etc.) are NOT wired here — they arrive through HTTP hook requests from
the Claude CLI subprocess. Approval resolution lives in
PendingInteractionManager, not in this class.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from gobby.llm.claude_cli import ClaudeCLI, CLISession
from gobby.llm.claude_models import (
    ChatEvent,
    DoneEvent,
    TextChunk,
    ThinkingEvent,
)
from gobby.llm.stream_json_parser import (
    ContentBlockDelta,
    ResultEvent,
    StreamEvent,
)

logger = logging.getLogger(__name__)


def _extract_text(content: list[dict[str, Any]]) -> str:
    """Extract text from multimodal content blocks.

    Filters for ``{"type": "text", "text": "..."}`` blocks and joins
    them with newlines, discarding image and other block types.
    """
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def _stream_to_chat_event(event: StreamEvent) -> ChatEvent | None:
    """Convert a StreamEvent from the CLI JSON stream to a ChatEvent.

    Maps:
    - ContentBlockDelta(block_type="text")    -> TextChunk
    - ContentBlockDelta(block_type="thinking") -> ThinkingEvent
    - ResultEvent                              -> DoneEvent

    Returns None for unrecognised or irrelevant event types (e.g.
    InitEvent, ErrorEvent) which are handled at a different layer.
    """
    if isinstance(event, ContentBlockDelta):
        if event.block_type == "text":
            return TextChunk(content=event.text)
        elif event.block_type == "thinking":
            return ThinkingEvent(content=event.text)
    elif isinstance(event, ResultEvent):
        return DoneEvent(
            tool_calls_count=0,
            cost_usd=event.cost_usd,
            input_tokens=event.input_tokens,
            output_tokens=event.output_tokens,
        )
    return None


class CLIChatSession:
    """Claude CLI-backed web chat session implementing ChatSessionProtocol.

    Delegates to :class:`ClaudeCLI` / :class:`CLISession` for subprocess
    management and stream parsing.  The WebSocket layer treats this
    identically to :class:`ChatSession` (SDK) and
    :class:`CodexChatSession` via the :class:`ChatSessionProtocol`.

    Key differences from the SDK-backed ChatSession:

    * No SDK hooks — lifecycle events (pre_tool, post_tool, etc.) arrive
      through HTTP hook requests from the Claude CLI subprocess.
    * Approval resolution lives in PendingInteractionManager, so
      ``has_pending_question``, ``has_pending_approval``, and
      ``has_pending_plan`` always return False here.
    * ``drain_pending_response`` is a no-op — CLI sessions don't buffer
      stale events after an interrupt.
    """

    provider: str = "claude"

    def __init__(
        self,
        conversation_id: str,
        model: str | None = None,
        session_id: str | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        # Identity / protocol fields
        self.conversation_id = conversation_id
        self.db_session_id: str | None = None
        self.seq_num: int | None = None
        self.project_id: str | None = None
        self.project_path: str | None = None
        self.message_index: int = 0
        self.chat_mode: str = "code"
        self.system_prompt_override: str | None = None
        self.resume_session_id: str | None = None
        self.last_activity: datetime = datetime.now(UTC)

        # Lifecycle callbacks (set by WS layer — unused by CLI sessions,
        # but must exist for ChatSessionProtocol conformance)
        self._on_before_agent: (
            Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None
        ) = None
        self._on_pre_tool: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = (
            None
        )
        self._on_post_tool: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = (
            None
        )
        self._on_pre_compact: (
            Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None
        ) = None
        self._on_stop: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = None
        self._on_mode_changed: Callable[[str, str], Awaitable[None]] | None = None
        self._on_plan_ready: Callable[[str | None, dict[str, Any]], Awaitable[None]] | None = None

        # Optional attrs set dynamically by WebSocket session control
        self._tool_approval_config: Any = None
        self._tool_approval_callback: Callable[..., Any] | None = None
        self._session_manager_ref: Any = None
        self._on_mode_persist: Callable[[str], None] | None = None
        self._on_approved_tools_persist: Callable[[set[str]], None] | None = None
        self._approved_tools: set[str] = set()
        self._plan_file_path: str | None = None
        self._pending_agent_name: str | None = None
        self._plan_approval_completed: bool = False
        self._context_window_overrides: dict[str, int] = {}
        self._accumulated_output_tokens: int = 0
        self._accumulated_cost_usd: float = 0.0
        self._message_manager_source_session_id: str | None = None
        self._needs_history_injection: bool = False
        self._message_manager: Any = None

        # CLI internals
        self._model = model
        self._session_id = session_id
        self._env_overrides = env_overrides or {}
        self._cli = ClaudeCLI()
        self._cli_session: CLISession | None = None
        self._connected = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """Whether the CLI subprocess session is alive."""
        return self._connected

    @property
    def model(self) -> str | None:
        """The current model for this session."""
        return self._model

    @property
    def has_pending_question(self) -> bool:
        """Always False — handled by PendingInteractionManager."""
        return False

    @property
    def has_pending_approval(self) -> bool:
        """Always False — handled by PendingInteractionManager."""
        return False

    @property
    def has_pending_plan(self) -> bool:
        """Always False — handled by PendingInteractionManager."""
        return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, model: str | None = None) -> None:
        """Start the Claude CLI subprocess session.

        Creates a :class:`CLISession` via :meth:`ClaudeCLI.session` and
        connects to the subprocess.

        Args:
            model: Optional model override (e.g. ``"opus"``). Falls back
                to the model passed at construction time.
        """
        self._cli_session = self._cli.session(
            session_id=self._session_id,
            model=model or self._model,
            env_overrides=self._env_overrides,
        )
        await self._cli_session.start()
        self._connected = True
        self.last_activity = datetime.now(UTC)
        logger.debug(f"CLIChatSession {self.conversation_id} started")

    async def send_message(self, content: str | list[dict[str, Any]]) -> AsyncIterator[ChatEvent]:
        """Send a user message and yield streaming ChatEvent instances.

        Translates the CLI subprocess's :class:`StreamEvent` objects to
        the shared :class:`ChatEvent` union used by the WebSocket layer.

        Args:
            content: Plain text prompt or list of multimodal content
                blocks (``[{"type": "text", "text": "..."}]``).

        Yields:
            :class:`TextChunk`, :class:`ThinkingEvent`, or
            :class:`DoneEvent` instances.
        """
        if not self._cli_session or not self._connected:
            raise RuntimeError("CLIChatSession not connected. Call start() first.")

        self.last_activity = datetime.now(UTC)

        text = content if isinstance(content, str) else _extract_text(content)
        async for event in self._cli_session.send(text):
            chat_event = _stream_to_chat_event(event)
            if chat_event is not None:
                yield chat_event

    async def interrupt(self) -> None:
        """Interrupt the current CLI response stream."""
        if self._cli_session:
            try:
                await self._cli_session.interrupt()
            except Exception as e:
                logger.warning(f"CLIChatSession {self.conversation_id} interrupt error: {e}")

    async def drain_pending_response(self) -> None:
        """No-op — CLI sessions don't have a pending response buffer."""

    async def stop(self) -> None:
        """Stop the CLI subprocess and clean up."""
        if self._cli_session:
            try:
                await self._cli_session.stop()
            except Exception as e:
                logger.debug(f"CLIChatSession {self.conversation_id} stop error (expected): {e}")
            finally:
                self._cli_session = None
                self._connected = False
                logger.debug(f"CLIChatSession {self.conversation_id} stopped")

    async def switch_model(self, new_model: str) -> None:
        """Switch model for the next message.

        CLI sessions apply the model on the next ``send`` call, so this
        just updates the stored model name.
        """
        self._model = new_model

    def set_chat_mode(self, mode: str) -> None:
        """Set the chat mode (e.g. ``"code"``, ``"plan"``)."""
        self.chat_mode = mode

    # ------------------------------------------------------------------
    # Interaction stubs (delegated to PendingInteractionManager)
    # ------------------------------------------------------------------

    def provide_answer(self, answers: dict[str, str]) -> None:
        """No-op — handled by PendingInteractionManager."""

    def provide_approval(self, decision: str) -> None:
        """No-op — handled by PendingInteractionManager."""

    def provide_plan_decision(self, decision: str) -> None:
        """No-op — handled by PendingInteractionManager."""

    def approve_plan(self) -> None:
        """No-op — handled by PendingInteractionManager."""

    def set_plan_feedback(self, feedback: str) -> None:
        """No-op — handled by PendingInteractionManager."""

    async def sync_sdk_permission_mode(self) -> None:
        """No-op — permissions handled via hook hold-open, not SDK sync."""

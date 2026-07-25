"""
Session message processor.

Handles asynchronous, incremental processing of session transcripts.
Tracks file offsets and updates the database with new messages.

Supports two transcript formats:
- JSONL: Incremental line-by-line processing with byte offset tracking (Claude, Codex)
- JSON: Full-file parsing with mtime-based change detection (Qwen session files)
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from weakref import WeakValueDictionary

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager
    from gobby.storage.context_usage_snapshot import ContextUsageSnapshot
    from gobby.storage.sessions import SessionManager

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.sessions.context_usage import snapshot_from_token_usage, snapshot_from_window_metadata
from gobby.sessions.message_stats import MessageStats
from gobby.sessions.processor_lifecycle import ProcessorLifecycleMixin
from gobby.sessions.processor_stats import ProcessorStatsMixin
from gobby.sessions.processor_transcripts import ProcessorTranscriptMixin
from gobby.sessions.processor_types import WebSocketServer
from gobby.sessions.processor_usage import ProcessorUsageMixin
from gobby.sessions.transcript_index import TranscriptIndexAppender
from gobby.sessions.transcript_renderer import RenderState
from gobby.sessions.transcripts.base import TokenUsage, TranscriptParser
from gobby.sessions.transcripts.codex import CodexNestedExecOutcome
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.token_events import TokenEventStore
from gobby.storage.unmodeled_observations import UnmodeledObservationStore
from gobby.telemetry.instruments import inc_counter

logger = logging.getLogger(__name__)


class SessionMessageProcessor(
    ProcessorLifecycleMixin,
    ProcessorStatsMixin,
    ProcessorUsageMixin,
    ProcessorTranscriptMixin,
):
    """
    Processes session transcripts in the background.

    - Watches active session transcript files
    - incrementally reads new content
    - parses messages using TranscriptParser
    - stores normalized messages in the database
    """

    def __init__(
        self,
        db: HubDatabase,
        poll_interval: float = 2.0,
        websocket_server: "WebSocketServer | None" = None,
        session_manager: "SessionManager | None" = None,
        hook_manager: "HookManager | None" = None,
        run_db: Callable[..., Awaitable[Any]] | None = None,
    ):
        self.db = db
        self.poll_interval = poll_interval
        self.websocket_server: WebSocketServer | None = websocket_server
        self.session_manager: SessionManager | None = session_manager
        self._hook_manager: Any | None = hook_manager
        self._run_db = run_db or asyncio.to_thread

        # Track active sessions: session_id -> transcript_path
        self._active_sessions: dict[str, str] = {}
        self._processing_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

        # Track parsers: session_id -> TranscriptParser
        self._parsers: dict[str, TranscriptParser] = {}
        self._session_sources: dict[str, str] = {}

        # Track transcript identity and mtime to detect replacement or rollback.
        self._transcript_file_state: dict[str, tuple[int, int, int]] = {}

        # Track byte offsets and message indices per session (in-memory)
        self._byte_offsets: dict[str, int] = {}
        self._message_indices: dict[str, int] = {}

        # Track render state for incremental rendering per session
        self._render_states: dict[str, RenderState] = {}
        self._index_appenders: dict[str, TranscriptIndexAppender] = {}
        self._observation_store = UnmodeledObservationStore(db)

        # Incremental stat accumulators per session
        self._stats: dict[str, MessageStats] = {}
        self._stats_hydration_skipped: set[str] = set()

        self._running = False
        self._task: asyncio.Task[None] | None = None

    def set_hook_manager(self, hook_manager: "HookManager | None") -> None:
        """Wire the hook manager after application services finish starting."""
        self._hook_manager = hook_manager

    def _inc_counter(self, name: str) -> None:
        inc_counter(name)

    def _new_token_event_store(self) -> TokenEventStore:
        return TokenEventStore(self.db)

    def _snapshot_from_token_usage(
        self,
        *,
        source: str | None,
        context_window: int | None,
        usage: TokenUsage,
        model: str | None,
    ) -> "ContextUsageSnapshot | None":
        return snapshot_from_token_usage(
            source=source,
            context_window=context_window,
            usage=usage,
            model=model,
        )

    def _snapshot_from_window_metadata(
        self,
        *,
        source: str | None,
        context_window: int | None,
        model: str | None,
    ) -> "ContextUsageSnapshot | None":
        return snapshot_from_window_metadata(
            source=source,
            context_window=context_window,
            model=model,
        )

    @staticmethod
    def _build_codex_exec_outcome_event(
        session: dict[str, Any],
        outcome: CodexNestedExecOutcome,
    ) -> HookEvent | None:
        """Build one synthetic Bash completion from a correlated rollout result."""
        external_id = session.get("external_id")
        if not isinstance(external_id, str) or not external_id.strip():
            logger.warning(
                "Skipping Codex synthesized tool event without external_id",
                extra={
                    "platform_session_id": session.get("platform_session_id"),
                    "tool_name": "Bash",
                    "phase": "end",
                },
            )
            return None

        data: dict[str, Any] = {
            "tool_name": "Bash",
            "tool_input": {"command": outcome.command},
            "tool_output": dict(outcome.result),
            "call_id": outcome.identity,
            "item_id": outcome.identity,
            "raw_json": outcome.raw_json,
        }
        normalize_tool_fields(data)

        metadata: dict[str, Any] = {"_codex_transcript_exec_outcome": True}
        platform_session_id = session.get("platform_session_id")
        if isinstance(platform_session_id, str) and platform_session_id:
            metadata["_platform_session_id"] = platform_session_id

        return HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id=external_id,
            source=SessionSource.CODEX,
            timestamp=outcome.timestamp,
            data=data,
            machine_id=session.get("machine_id"),
            project_id=session.get("project_id"),
            metadata=metadata,
        )

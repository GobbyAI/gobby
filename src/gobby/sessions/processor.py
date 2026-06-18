"""
Session message processor.

Handles asynchronous, incremental processing of session transcripts.
Tracks file offsets and updates the database with new messages.

Supports two transcript formats:
- JSONL: Incremental line-by-line processing with byte offset tracking (Claude, Codex)
- JSON: Full-file parsing with mtime-based change detection (Gemini native session files)
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

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
from gobby.sessions.transcripts.base import ParsedToolEvent, TokenUsage, TranscriptParser
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.token_events import TokenEventStore
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
    ):
        self.db = db
        self.poll_interval = poll_interval
        self.websocket_server: WebSocketServer | None = websocket_server
        self.session_manager: SessionManager | None = session_manager
        self._hook_manager: Any | None = hook_manager

        # Track active sessions: session_id -> transcript_path
        self._active_sessions: dict[str, str] = {}

        # Track parsers: session_id -> TranscriptParser
        self._parsers: dict[str, TranscriptParser] = {}

        # Track last mtime for JSON file sessions (mtime-based change detection)
        self._last_mtime: dict[str, float] = {}

        # Track byte offsets and message indices per session (in-memory)
        self._byte_offsets: dict[str, int] = {}
        self._message_indices: dict[str, int] = {}

        # Track render state for incremental rendering per session
        self._render_states: dict[str, RenderState] = {}
        self._index_appenders: dict[str, TranscriptIndexAppender] = {}

        # Incremental stat accumulators per session
        self._stats: dict[str, MessageStats] = {}
        self._stats_hydration_skipped: set[str] = set()

        self._running = False
        self._task: asyncio.Task[None] | None = None

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
    def _build_codex_hook_event(
        session: dict[str, Any],
        tool_event: ParsedToolEvent,
    ) -> HookEvent | None:
        """Build a Codex tool HookEvent from a parsed transcript lifecycle record."""
        if not tool_event.tool:
            return None
        if tool_event.phase == "begin":
            event_type = HookEventType.BEFORE_TOOL
        elif tool_event.phase == "end":
            event_type = HookEventType.AFTER_TOOL
        else:
            return None

        server = tool_event.server or "codex"
        data: dict[str, Any] = {
            "tool_name": f"mcp__{server}__{tool_event.tool}",
            "tool_input": dict(tool_event.arguments),
        }
        if tool_event.call_id:
            data["call_id"] = tool_event.call_id
            data["item_id"] = tool_event.call_id
        if tool_event.raw_json:
            data["raw_json"] = tool_event.raw_json
        if event_type == HookEventType.AFTER_TOOL:
            if tool_event.result is not None:
                data["tool_output"] = tool_event.result
            if tool_event.error is not None:
                data["tool_error"] = tool_event.error
                data["is_error"] = True
            if tool_event.duration_ns is not None:
                data["duration_ns"] = tool_event.duration_ns

        normalize_tool_fields(data)
        metadata: dict[str, Any] = {"_codex_synthesized_tool_event": True}
        platform_session_id = session.get("platform_session_id")
        if isinstance(platform_session_id, str) and platform_session_id:
            metadata["_platform_session_id"] = platform_session_id

        external_id = session.get("external_id")
        if not isinstance(external_id, str) or not external_id.strip():
            logger.warning(
                "Skipping Codex synthesized tool event without external_id",
                extra={
                    "platform_session_id": platform_session_id,
                    "tool_name": data.get("tool_name"),
                    "phase": tool_event.phase,
                },
            )
            return None

        return HookEvent(
            event_type=event_type,
            session_id=external_id,
            source=SessionSource.CODEX,
            timestamp=tool_event.timestamp,
            data=data,
            machine_id=session.get("machine_id"),
            project_id=session.get("project_id"),
            metadata=metadata,
        )

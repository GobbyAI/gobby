"""
Session message processor.

Handles asynchronous, incremental processing of session transcripts.
Tracks file offsets and updates the database with new messages.

Supports two transcript formats:
- JSONL: Incremental line-by-line processing with byte offset tracking (Claude, Codex)
- JSON: Full-file parsing with mtime-based change detection (Gemini native session files)
"""

import asyncio
import inspect
import json
import logging
import os
from typing import TYPE_CHECKING, Any, Protocol

import aiofiles

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager
    from gobby.storage.sessions import SessionManager

    class WebSocketServer(Protocol):
        async def broadcast(self, message: dict[str, Any]) -> None: ...

        async def feed_attached_session_tts(
            self,
            session_id: str,
            rendered: dict[str, Any],
            *,
            complete: bool = False,
        ) -> None: ...


from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.sessions.transcript_renderer import RenderState, render_incremental
from gobby.sessions.transcripts import get_parser
from gobby.sessions.transcripts.base import ParsedMessage, ParsedToolEvent, TranscriptParser
from gobby.sessions.transcripts.gemini import GeminiTranscriptParser
from gobby.storage.database import DatabaseProtocol
from gobby.telemetry.instruments import inc_counter

logger = logging.getLogger(__name__)


class SessionMessageProcessor:
    """
    Processes session transcripts in the background.

    - Watches active session transcript files
    - incrementally reads new content
    - parses messages using TranscriptParser
    - stores normalized messages in the database
    """

    def __init__(
        self,
        db: DatabaseProtocol,
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

        # Incremental stat accumulators per session
        self._stats: dict[str, dict[str, Any]] = {}

        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def _feed_attached_session_tts(
        self,
        session_id: str,
        rendered: dict[str, Any],
        *,
        complete: bool,
    ) -> None:
        """Feed opt-in TTS without suppressing transcript broadcasts."""
        if self.websocket_server is None:
            return
        tts_feed = getattr(self.websocket_server, "feed_attached_session_tts", None)
        if not callable(tts_feed):
            return
        try:
            result = tts_feed(session_id, rendered, complete=complete)
            if inspect.isawaitable(result):
                await result
        except Exception:
            inc_counter("tts_feed_failures_total")
            logger.warning(
                "Attached-session TTS feed failed for session %s",
                session_id,
                exc_info=True,
            )

    async def _broadcast_rendered_session_message(
        self,
        session_id: str,
        rendered: dict[str, Any],
        *,
        complete: bool,
    ) -> None:
        if self.websocket_server is None:
            return
        await self._feed_attached_session_tts(session_id, rendered, complete=complete)
        await self.websocket_server.broadcast(
            {
                "type": "session_message",
                "session_id": session_id,
                "message": rendered,
                "complete": complete,
            }
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

    def _accumulate_stats(self, session_id: str, messages: list[Any]) -> dict[str, Any]:
        """Accumulate incremental stats from parsed messages."""
        stats = self._stats.get(
            session_id,
            {
                "message_count": 0,
                "turn_count": 0,
                "tool_call_count": 0,
                "last_assistant_content": None,
            },
        )
        for msg in messages:
            stats["message_count"] = stats.get("message_count", 0) + 1
            if msg.role == "assistant" and msg.content_type == "text":
                stats["turn_count"] = stats.get("turn_count", 0) + 1
                if isinstance(msg.content, str) and msg.content.strip():
                    stats["last_assistant_content"] = msg.content.strip()[-500:]
            if msg.tool_name:
                stats["tool_call_count"] = stats.get("tool_call_count", 0) + 1
        self._stats[session_id] = stats
        return stats

    async def start(self) -> None:
        """Start the processing loop."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("SessionMessageProcessor started")

    async def stop(self) -> None:
        """Stop the processing loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("SessionMessageProcessor stopped")

    def register_session(
        self, session_id: str, transcript_path: str, source: str = "claude"
    ) -> None:
        """
        Register a session for monitoring.

        The transcript file may not exist yet at registration time — Codex CLI
        writes its rollout shortly after session_start fires, so the file
        appears a second or so later. Register the session anyway; the poll
        loop's ``_process_session`` gate (``if not os.path.exists``) already
        handles missing files gracefully, and once the file appears the next
        poll picks it up from byte zero.

        Args:
            session_id: Session ID
            transcript_path: Absolute path to the transcript JSONL file
            source: CLI source name (default: "claude")
        """
        if session_id in self._active_sessions:
            return

        self._active_sessions[session_id] = transcript_path
        self._parsers[session_id] = get_parser(
            source,
            session_id=session_id,
            transcript_path=transcript_path,
        )
        if os.path.exists(transcript_path):
            logger.debug(f"Registered session {session_id} for processing ({source})")
        else:
            logger.debug(
                f"Registered session {session_id} for processing ({source}); "
                f"transcript not yet on disk, poll loop will catch it: {transcript_path}"
            )

    async def flush_session(self, session_id: str) -> None:
        """Force an immediate processing pass for a single session.

        Useful when stats need to be up-to-date before reading them
        (e.g., at SESSION_END before completing an agent run).
        """
        transcript_path = self._active_sessions.get(session_id)
        if transcript_path:
            await self._process_session(session_id, transcript_path)

    def unregister_session(self, session_id: str) -> None:
        """Stop monitoring a session."""
        if session_id in self._active_sessions:
            del self._active_sessions[session_id]
            if session_id in self._parsers:
                del self._parsers[session_id]
            self._last_mtime.pop(session_id, None)
            self._stats.pop(session_id, None)
            self._byte_offsets.pop(session_id, None)
            self._message_indices.pop(session_id, None)
            logger.debug(f"Unregistered session {session_id}")
        # Always clean render state (may exist even if session wasn't fully registered)
        self._render_states.pop(session_id, None)

    async def _loop(self) -> None:
        """Main processing loop."""
        while self._running:
            try:
                await self._process_all_sessions()
            except Exception as e:
                logger.error(f"Error in SessionMessageProcessor loop: {e}")

            await asyncio.sleep(self.poll_interval)

    async def _process_all_sessions(self) -> None:
        """Process all registered sessions."""
        # Create list copy to avoid concurrent modification issues
        sessions = list(self._active_sessions.items())

        for session_id, transcript_path in sessions:
            try:
                await self._process_session(session_id, transcript_path)
            except Exception as e:
                logger.error(f"Failed to process session {session_id}: {e}", exc_info=True)

    async def _process_session(self, session_id: str, transcript_path: str) -> None:
        """
        Process a single session.

        Dispatches to format-specific processing based on file extension:
        - .json: Full-file parsing with mtime change detection (Gemini native)
        - .jsonl/.ndjson/other: Incremental line-by-line with byte offset tracking
        """
        if not os.path.exists(transcript_path):
            return

        if transcript_path.endswith(".json"):
            await self._process_json_session(session_id, transcript_path)
            return

        # Get current processing state (in-memory)
        last_offset = self._byte_offsets.get(session_id, 0)
        last_index = self._message_indices.get(session_id, -1)

        # Read new content
        new_lines = []
        valid_offset = last_offset

        try:
            # Note: synchronous file I/O for simplicity; could use aiofiles if blocking is an issue
            # but reading incremental logs is usually fast.
            with open(transcript_path, encoding="utf-8") as f:
                # Seek to last known position
                f.seek(last_offset)

                # Read line by line
                while True:
                    line = f.readline()
                    if not line:
                        break

                    # Only process complete lines
                    if line.endswith("\n"):
                        new_lines.append(line)
                        valid_offset = f.tell()
                    else:
                        # Incomplete line (write in progress), stop reading
                        break

        except Exception as e:
            logger.error(f"Error reading transcript {transcript_path}: {e}")
            return

        if not new_lines:
            return

        # Parse new lines
        parser = self._parsers.get(session_id)
        if not parser:
            return

        parsed_records = parser.parse_lines(new_lines, start_index=last_index + 1)

        # Split mixed records: ParsedMessage feeds stats/rendering. Codex MCP
        # lifecycle records are parsed for transcript fidelity, while native
        # Codex MCP hooks now own workflow lifecycle dispatch.
        parsed_messages: list[ParsedMessage] = [
            r for r in parsed_records if isinstance(r, ParsedMessage)
        ]

        # Always advance the byte offset once the lines are read; otherwise
        # a re-tail would reprocess already-consumed transcript lines.
        self._byte_offsets[session_id] = valid_offset

        if not parsed_messages:
            return

        # Compute incremental stats (no DB message writes)
        stats = self._accumulate_stats(session_id, parsed_messages)

        # Write stats to sessions table
        if self.session_manager:
            self.session_manager.touch(session_id)
            self.session_manager.update_stats(
                session_id,
                message_count=stats.get("message_count", 0),
                turn_count=stats.get("turn_count", 0),
                tool_call_count=stats.get("tool_call_count", 0),
                last_assistant_content=stats.get("last_assistant_content"),
            )
            # Extract and store model
            for msg in parsed_messages:
                if msg.model:
                    self.session_manager.update_model(session_id, msg.model)
                    break

        # Render incrementally and broadcast
        render_state = self._render_states.get(session_id, RenderState())
        completed, render_state = render_incremental(
            parsed_messages, render_state, session_id=session_id
        )
        self._render_states[session_id] = render_state

        if self.websocket_server:
            for rendered_msg in completed:
                await self._broadcast_rendered_session_message(
                    session_id,
                    rendered_msg.to_dict(),
                    complete=True,
                )
            # Broadcast in-progress turn for live updates (upsert by ID)
            if render_state.current_message:
                await self._broadcast_rendered_session_message(
                    session_id,
                    render_state.current_message.to_dict(),
                    complete=False,
                )

        # Update in-memory state
        new_last_index = parsed_messages[-1].index
        self._message_indices[session_id] = new_last_index

        logger.debug(f"Processed {len(parsed_messages)} messages for {session_id}")

    async def _process_json_session(self, session_id: str, transcript_path: str) -> None:
        """
        Process a JSON session file (e.g., Gemini native format).

        Uses mtime to detect changes, reads the entire file, and parses
        all messages. Only stores messages newer than last_message_index.
        """
        try:
            current_mtime = os.path.getmtime(transcript_path)
        except OSError:
            return

        # Skip if file hasn't changed since last poll
        last_mtime = self._last_mtime.get(session_id, 0.0)
        if current_mtime <= last_mtime:
            return

        # Read and parse the entire file
        try:
            async with aiofiles.open(transcript_path, encoding="utf-8") as f:
                raw = await f.read()
            data = json.loads(raw)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Error reading JSON transcript {transcript_path}: {e}")
            return

        if not isinstance(data, dict):
            logger.warning(f"JSON transcript is not an object: {transcript_path}")
            return

        parser = self._parsers.get(session_id)
        if not parser or not isinstance(parser, GeminiTranscriptParser):
            logger.warning(f"No GeminiTranscriptParser for JSON session {session_id}")
            return

        all_messages = parser.parse_session_json(data)
        if not all_messages:
            self._last_mtime[session_id] = current_mtime
            return

        # Get current state to find only new messages (in-memory)
        last_index = self._message_indices.get(session_id, -1)

        new_messages = [m for m in all_messages if m.index > last_index]

        if not new_messages:
            self._last_mtime[session_id] = current_mtime
            return

        # Compute incremental stats (no DB message writes)
        stats = self._accumulate_stats(session_id, new_messages)

        # Write stats and keep session alive
        if self.session_manager:
            self.session_manager.touch(session_id)
            self.session_manager.update_stats(
                session_id,
                message_count=stats.get("message_count", 0),
                turn_count=stats.get("turn_count", 0),
                tool_call_count=stats.get("tool_call_count", 0),
                last_assistant_content=stats.get("last_assistant_content"),
            )
            for msg in new_messages:
                if msg.model:
                    self.session_manager.update_model(session_id, msg.model)
                    break

        # Render incrementally and broadcast
        render_state = self._render_states.get(session_id, RenderState())
        completed, render_state = render_incremental(
            new_messages, render_state, session_id=session_id
        )
        self._render_states[session_id] = render_state

        if self.websocket_server:
            for rendered_msg in completed:
                await self._broadcast_rendered_session_message(
                    session_id,
                    rendered_msg.to_dict(),
                    complete=True,
                )
            if render_state.current_message:
                await self._broadcast_rendered_session_message(
                    session_id,
                    render_state.current_message.to_dict(),
                    complete=False,
                )

        # Update in-memory state
        new_last_index = new_messages[-1].index
        self._message_indices[session_id] = new_last_index

        # Track mtime for change detection
        self._last_mtime[session_id] = current_mtime

        logger.debug(f"Processed {len(new_messages)} JSON messages for {session_id}")

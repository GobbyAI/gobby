"""
Session message processor.

Handles asynchronous, incremental processing of session transcripts.
Tracks file offsets and updates the database with new messages.

Supports two transcript formats:
- JSONL: Incremental line-by-line processing with byte offset tracking (Claude, Codex)
- JSON: Full-file parsing with mtime-based change detection (Gemini native session files)
"""

import asyncio
import json
import logging
import os
from datetime import UTC
from typing import TYPE_CHECKING, Any

import aiofiles

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager
    from gobby.servers.websocket.server import WebSocketServer
    from gobby.storage.sessions import SessionManager

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.sessions.transcript_renderer import RenderState, render_incremental
from gobby.sessions.transcripts import get_parser
from gobby.sessions.transcripts.base import ParsedMessage, ParsedToolEvent, TranscriptParser
from gobby.sessions.transcripts.gemini import GeminiTranscriptParser
from gobby.storage.database import DatabaseProtocol

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
        self._hook_manager: HookManager | None = hook_manager

        # Track active sessions: session_id -> transcript_path
        self._active_sessions: dict[str, str] = {}

        # Track session source (claude/codex/...) for hook synthesis
        self._sources: dict[str, str] = {}

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
        poll picks it up from byte zero. Skipping registration here means the
        Codex MCP-call synthesis path never sees the rollout at all.

        Args:
            session_id: Session ID
            transcript_path: Absolute path to the transcript JSONL file
            source: CLI source name (default: "claude")
        """
        if session_id in self._active_sessions:
            return

        self._active_sessions[session_id] = transcript_path
        self._sources[session_id] = source
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
            self._sources.pop(session_id, None)
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

        # Split mixed records: ParsedMessage feeds stats/rendering;
        # ParsedToolEvent feeds the rule engine via HookManager.
        parsed_messages: list[ParsedMessage] = [
            r for r in parsed_records if isinstance(r, ParsedMessage)
        ]
        tool_events: list[ParsedToolEvent] = [
            r for r in parsed_records if isinstance(r, ParsedToolEvent)
        ]

        # Always advance the byte offset once the lines are read; otherwise
        # a re-tail would re-fire HookEvents for already-processed lines.
        self._byte_offsets[session_id] = valid_offset

        if tool_events:
            await self._dispatch_tool_events(session_id, tool_events)

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
                await self.websocket_server.broadcast(
                    {
                        "type": "session_message",
                        "session_id": session_id,
                        "message": rendered_msg.to_dict(),
                    }
                )
            # Broadcast in-progress turn for live updates (upsert by ID)
            if render_state.current_message:
                await self.websocket_server.broadcast(
                    {
                        "type": "session_message",
                        "session_id": session_id,
                        "message": render_state.current_message.to_dict(),
                    }
                )

        # Update in-memory state
        new_last_index = parsed_messages[-1].index
        self._message_indices[session_id] = new_last_index

        logger.debug(f"Processed {len(parsed_messages)} messages for {session_id}")

    async def _dispatch_tool_events(self, session_id: str, events: list[ParsedToolEvent]) -> None:
        """Synthesize BEFORE_TOOL/AFTER_TOOL HookEvents from rollout records.

        Codex CLI's experimental hooks fire PreToolUse/PostToolUse for shell
        tools but not MCP tool calls; this synthesis closes that gap so rules
        like ``inject-task-creation-on-schema`` evaluate against the same
        events on Codex that they see on Claude.
        """
        if self._hook_manager is None:
            return

        # Only Codex rollout parsers emit ParsedToolEvent records today. Other
        # sources, including Droid, rely on native CLI hooks rather than
        # synthesized transcript tool hooks, so they intentionally fall through.
        source = self._sources.get(session_id)
        if source != "codex":
            return

        # ``HookEvent.session_id`` on the wire is the *external_id* (Codex
        # thread UUID) — HookManager's session_lookup treats it that way and
        # resolves via the composite UNIQUE index on the sessions table:
        # (external_id, machine_id, source, project_id). Passing the platform
        # UUID as session_id would miss the index and fall through to
        # auto-register-as-new-session, creating a phantom duplicate row
        # that receives all rule effects instead of the real row.
        # Load the registered session and rehydrate the full composite so
        # the resolver lands on the exact row deterministically.
        session_context = self._load_session_context(session_id)
        if session_context is None:
            logger.debug(
                "Skipping Codex tool synthesis for %s: session row not resolvable",
                session_id,
            )
            return

        for event in events:
            try:
                hook_event = self._build_codex_hook_event(session_context, event)
            except Exception as exc:
                logger.warning(
                    "Failed to build hook event from Codex tool record (%s): %s",
                    event.phase,
                    exc,
                )
                continue
            if hook_event is None:
                continue
            try:
                await asyncio.to_thread(self._hook_manager.handle, hook_event)
            except Exception as exc:
                logger.warning(
                    "HookManager rejected synthesized %s for session %s: %s",
                    hook_event.event_type,
                    session_id,
                    exc,
                )

    def _load_session_context(self, session_id: str) -> dict[str, Any] | None:
        """Rehydrate the composite-key context HookManager needs to resolve
        the synthesized event back to the correct platform session row."""
        if self.session_manager is None:
            return None
        try:
            session = self.session_manager.get(session_id)
        except Exception:
            return None
        if session is None:
            return None
        external_id = getattr(session, "external_id", None)
        if not isinstance(external_id, str) or not external_id:
            return None
        return {
            "external_id": external_id,
            "machine_id": getattr(session, "machine_id", None),
            "project_id": getattr(session, "project_id", None),
            "platform_session_id": session_id,
        }

    @staticmethod
    def _build_codex_hook_event(
        session_context: dict[str, Any],
        event: ParsedToolEvent,
    ) -> HookEvent | None:
        if not event.server or not event.tool:
            return None

        tool_name = f"mcp__{event.server}__{event.tool}"
        data: dict[str, Any] = {
            "tool_name": tool_name,
            "tool_input": event.arguments,
            "server_name": event.server,
            "mcp_server": event.server,
            "mcp_tool": event.tool,
        }
        if event.call_id:
            data["call_id"] = event.call_id
            data["tool_use_id"] = event.call_id
            data["item_id"] = event.call_id
        if event.phase == "end":
            if event.error is not None:
                data["error"] = event.error
                data["is_error"] = True
            if event.result is not None:
                data["tool_response"] = event.result
            if event.duration_ns is not None:
                data["duration_ns"] = event.duration_ns

        normalize_tool_fields(data)

        timestamp = (
            event.timestamp if event.timestamp.tzinfo else event.timestamp.replace(tzinfo=UTC)
        )

        machine_id = session_context.get("machine_id")
        project_id = session_context.get("project_id")

        return HookEvent(
            event_type=(
                HookEventType.BEFORE_TOOL if event.phase == "begin" else HookEventType.AFTER_TOOL
            ),
            session_id=session_context["external_id"],
            source=SessionSource.CODEX,
            timestamp=timestamp,
            data=data,
            machine_id=machine_id if isinstance(machine_id, str) else None,
            project_id=project_id if isinstance(project_id, str) else None,
            metadata={
                "_synthesized_from": "codex_rollout",
                "_platform_session_id": session_context.get("platform_session_id"),
            },
        )

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
                await self.websocket_server.broadcast(
                    {
                        "type": "session_message",
                        "session_id": session_id,
                        "message": rendered_msg.to_dict(),
                    }
                )
            if render_state.current_message:
                await self.websocket_server.broadcast(
                    {
                        "type": "session_message",
                        "session_id": session_id,
                        "message": render_state.current_message.to_dict(),
                    }
                )

        # Update in-memory state
        new_last_index = new_messages[-1].index
        self._message_indices[session_id] = new_last_index

        # Track mtime for change detection
        self._last_mtime[session_id] = current_mtime

        logger.debug(f"Processed {len(new_messages)} JSON messages for {session_id}")

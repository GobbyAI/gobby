"""
Session message processor.

Handles asynchronous, incremental processing of session transcripts.
Tracks file offsets and updates the database with new messages.

Supports two transcript formats:
- JSONL: Incremental line-by-line processing with byte offset tracking (Claude, Codex, Cursor)
- JSON: Full-file parsing with mtime-based change detection (Gemini native session files)
"""

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING

import aiofiles

if TYPE_CHECKING:
    from gobby.servers.websocket.server import WebSocketServer
    from gobby.storage.sessions import LocalSessionManager

from gobby.sessions.transcripts import get_parser
from gobby.sessions.transcripts.base import TranscriptParser
from gobby.sessions.transcripts.gemini import GeminiTranscriptParser
from gobby.storage.database import DatabaseProtocol
from gobby.storage.session_messages import LocalSessionMessageManager
from gobby.storage.session_transcripts import (
    LocalSessionTranscriptManager,
    TranscriptSnapshotThrottle,
)

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
        session_manager: "LocalSessionManager | None" = None,
    ):
        self.db = db
        self.message_manager = LocalSessionMessageManager(db)
        self.poll_interval = poll_interval
        self.websocket_server: WebSocketServer | None = websocket_server
        self.session_manager: LocalSessionManager | None = session_manager

        # Transcript blob storage
        self.transcript_manager = LocalSessionTranscriptManager(db)
        self._snapshot_throttle = TranscriptSnapshotThrottle()

        # Track active sessions: session_id -> transcript_path
        self._active_sessions: dict[str, str] = {}

        # Track parsers: session_id -> TranscriptParser
        self._parsers: dict[str, TranscriptParser] = {}

        # Track last mtime for JSON file sessions (mtime-based change detection)
        self._last_mtime: dict[str, float] = {}

        self._running = False
        self._task: asyncio.Task[None] | None = None

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

        Args:
            session_id: Session ID
            transcript_path: Absolute path to the transcript JSONL file
            source: CLI source name (default: "claude")
        """
        if session_id in self._active_sessions:
            return

        if not os.path.exists(transcript_path):
            logger.warning(f"Transcript file not found: {transcript_path}")
            # We still register it, hoping it appears later (or we could fail)
            # For now, let's assume it might be created shortly.

        self._active_sessions[session_id] = transcript_path
        self._parsers[session_id] = get_parser(source)
        logger.debug(f"Registered session {session_id} for processing ({source})")

    def unregister_session(self, session_id: str) -> None:
        """Stop monitoring a session."""
        if session_id in self._active_sessions:
            del self._active_sessions[session_id]
            if session_id in self._parsers:
                del self._parsers[session_id]
            self._last_mtime.pop(session_id, None)
            self._snapshot_throttle.remove(session_id)
            logger.debug(f"Unregistered session {session_id}")

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

        # Get current processing state
        state = await self.message_manager.get_state(session_id)

        last_offset = 0
        last_index = -1

        if state:
            last_offset = state.get("last_byte_offset", 0)
            last_index = state.get("last_message_index", -1)

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

        parsed_messages = parser.parse_lines(new_lines, start_index=last_index + 1)

        if not parsed_messages:
            # We read lines but found no valid messages (maybe parse errors or skipped types)
            # We still update the offset so we don't re-read them endlessly
            await self.message_manager.update_state(
                session_id=session_id,
                byte_offset=valid_offset,
                message_index=last_index,
            )
            return

        # Store messages
        await self.message_manager.store_messages(session_id, parsed_messages)

        # Extract and store model from parsed messages (if present)
        if self.session_manager:
            for msg in parsed_messages:
                if msg.model:
                    self.session_manager.update_model(session_id, msg.model)
                    break  # Only need the first model found

        # Broadcast new messages
        if self.websocket_server:
            for msg in parsed_messages:
                payload = {
                    "type": "session_message",
                    "session_id": session_id,
                    "message": {
                        "index": msg.index,
                        "role": msg.role,
                        "content": msg.content,
                        "content_type": msg.content_type,
                        "tool_name": msg.tool_name,
                        "tool_input": json.dumps(msg.tool_input) if msg.tool_input else None,
                        "tool_result": json.dumps(msg.tool_result) if msg.tool_result else None,
                        "tool_use_id": msg.tool_use_id,
                        "timestamp": msg.timestamp.isoformat(),
                    },
                }
                await self.websocket_server.broadcast(payload)

        # Snapshot transcript blob (throttled)
        await self._maybe_snapshot_transcript(session_id, transcript_path)

        # Update state
        new_last_index = parsed_messages[-1].index

        await self.message_manager.update_state(
            session_id=session_id,
            byte_offset=valid_offset,
            message_index=new_last_index,
        )

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
            logger.error("Error reading JSON transcript %s: %s", transcript_path, e)
            return

        if not isinstance(data, dict):
            logger.warning("JSON transcript is not an object: %s", transcript_path)
            return

        parser = self._parsers.get(session_id)
        if not parser or not isinstance(parser, GeminiTranscriptParser):
            logger.warning("No GeminiTranscriptParser for JSON session %s", session_id)
            return

        all_messages = parser.parse_session_json(data)
        if not all_messages:
            self._last_mtime[session_id] = current_mtime
            return

        # Get current state to find only new messages
        state = await self.message_manager.get_state(session_id)
        last_index = state.get("last_message_index", -1) if state else -1

        new_messages = [m for m in all_messages if m.index > last_index]

        if not new_messages:
            self._last_mtime[session_id] = current_mtime
            return

        # Store messages
        await self.message_manager.store_messages(session_id, new_messages)

        # Extract and store model from parsed messages
        if self.session_manager:
            for msg in new_messages:
                if msg.model:
                    self.session_manager.update_model(session_id, msg.model)
                    break

        # Broadcast new messages
        if self.websocket_server:
            for msg in new_messages:
                payload = {
                    "type": "session_message",
                    "session_id": session_id,
                    "message": {
                        "index": msg.index,
                        "role": msg.role,
                        "content": msg.content,
                        "content_type": msg.content_type,
                        "tool_name": msg.tool_name,
                        "tool_input": json.dumps(msg.tool_input) if msg.tool_input else None,
                        "tool_result": json.dumps(msg.tool_result) if msg.tool_result else None,
                        "tool_use_id": msg.tool_use_id,
                        "timestamp": msg.timestamp.isoformat(),
                    },
                }
                await self.websocket_server.broadcast(payload)

        # Update state — use 0 for byte_offset since JSON doesn't use offsets
        new_last_index = new_messages[-1].index
        await self.message_manager.update_state(
            session_id=session_id,
            byte_offset=0,
            message_index=new_last_index,
        )

        # Snapshot transcript blob (throttled)
        await self._maybe_snapshot_transcript(session_id, transcript_path)

        # Track mtime for change detection
        self._last_mtime[session_id] = current_mtime

        logger.debug(f"Processed {len(new_messages)} JSON messages for {session_id}")

    async def _maybe_snapshot_transcript(
        self, session_id: str, transcript_path: str, *, force: bool = False
    ) -> None:
        """Snapshot transcript blob if throttle allows (or force=True)."""
        if not self._snapshot_throttle.should_snapshot(session_id, force=force):
            return

        try:
            raw = await asyncio.to_thread(self._read_file_bytes, transcript_path)
            if raw:
                await asyncio.to_thread(self.transcript_manager.store_transcript, session_id, raw)
                self._snapshot_throttle.record_snapshot(session_id)
        except Exception as e:
            logger.warning(f"Transcript snapshot failed for {session_id}: {e}")

    async def force_snapshot(self, session_id: str) -> bool:
        """Force a transcript snapshot (e.g., on session close).

        Returns True if snapshot was taken, False if no transcript path or error.
        """
        transcript_path = self._active_sessions.get(session_id)
        if not transcript_path or not os.path.exists(transcript_path):
            return False

        await self._maybe_snapshot_transcript(session_id, transcript_path, force=True)
        return True

    @staticmethod
    def _read_file_bytes(path: str) -> bytes | None:
        """Read entire file as bytes."""
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None

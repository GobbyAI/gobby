"""
Session message processor.

Handles asynchronous, incremental processing of session transcripts.
Tracks file offsets and updates the database with new messages.
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Dict

from gobby.sessions.transcripts.base import TranscriptParser
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.storage.database import LocalDatabase
from gobby.storage.messages import LocalMessageManager

logger = logging.getLogger(__name__)


class SessionMessageProcessor:
    """
    Processes session transcripts in the background.

    - Watches active session transcript files
    - incrementally reads new content
    - parses messages using TranscriptParser
    - stores normalized messages in the database
    """

    def __init__(self, db: LocalDatabase, poll_interval: float = 2.0):
        self.db = db
        self.message_manager = LocalMessageManager(db)
        self.poll_interval = poll_interval

        # Track active sessions: session_id -> transcript_path
        self._active_sessions: Dict[str, str] = {}

        # Track parsers: session_id -> TranscriptParser
        # Currently hardcoded to ClaudeTranscriptParser, but could support others
        self._parsers: Dict[str, TranscriptParser] = {}

        self._running = False
        self._task: asyncio.Task | None = None

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

    def register_session(self, session_id: str, transcript_path: str) -> None:
        """
        Register a session for monitoring.

        Args:
            session_id: Session ID
            transcript_path: Absolute path to the transcript JSONL file
        """
        if session_id in self._active_sessions:
            return

        if not os.path.exists(transcript_path):
            logger.warning(f"Transcript file not found: {transcript_path}")
            # We still register it, hoping it appears later (or we could fail)
            # For now, let's assume it might be created shortly.

        self._active_sessions[session_id] = transcript_path
        self._parsers[session_id] = ClaudeTranscriptParser()
        logger.debug(f"Registered session {session_id} for processing")

    def unregister_session(self, session_id: str) -> None:
        """Stop monitoring a session."""
        if session_id in self._active_sessions:
            del self._active_sessions[session_id]
            if session_id in self._parsers:
                del self._parsers[session_id]
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
                logger.error(f"Failed to process session {session_id}: {e}")

    async def _process_session(self, session_id: str, transcript_path: str) -> None:
        """
        Process a single session.

        Reads new lines from the transcript file from the last known byte offset.
        """
        if not os.path.exists(transcript_path):
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
            with open(transcript_path, "r", encoding="utf-8") as f:
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

        # Update state
        new_last_index = parsed_messages[-1].index

        await self.message_manager.update_state(
            session_id=session_id,
            byte_offset=valid_offset,
            message_index=new_last_index,
        )

        logger.debug(f"Processed {len(parsed_messages)} messages for {session_id}")

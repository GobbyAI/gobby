"""
Session lifecycle manager.

Handles background jobs for:
- Expiring stale sessions
- Processing transcripts for expired sessions
"""

import asyncio
import logging
import os

from gobby.config.app import SessionLifecycleConfig
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.storage.database import LocalDatabase
from gobby.storage.messages import LocalMessageManager
from gobby.storage.sessions import LocalSessionManager

logger = logging.getLogger(__name__)


class SessionLifecycleManager:
    """
    Manages session lifecycle background jobs.

    Two independent jobs:
    1. expire_stale_sessions - marks old active/paused sessions as expired
    2. process_pending_transcripts - processes transcripts for expired sessions
    """

    def __init__(self, db: LocalDatabase, config: SessionLifecycleConfig):
        self.db = db
        self.config = config
        self.session_manager = LocalSessionManager(db)
        self.message_manager = LocalMessageManager(db)

        self._running = False
        self._expire_task: asyncio.Task | None = None
        self._process_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start background jobs."""
        if self._running:
            return

        self._running = True

        # Start expire job
        self._expire_task = asyncio.create_task(
            self._expire_loop(),
            name="session-lifecycle-expire",
        )

        # Start process job
        self._process_task = asyncio.create_task(
            self._process_loop(),
            name="session-lifecycle-process",
        )

        logger.info(
            f"SessionLifecycleManager started "
            f"(expire every {self.config.expire_check_interval_minutes}m, "
            f"process every {self.config.transcript_processing_interval_minutes}m)"
        )

    async def stop(self) -> None:
        """Stop background jobs."""
        self._running = False

        tasks = [t for t in [self._expire_task, self._process_task] if t]
        for task in tasks:
            task.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._expire_task = None
        self._process_task = None

        logger.info("SessionLifecycleManager stopped")

    async def _expire_loop(self) -> None:
        """Background loop for expiring stale sessions."""
        interval_seconds = self.config.expire_check_interval_minutes * 60

        while self._running:
            try:
                await self._expire_stale_sessions()
            except Exception as e:
                logger.error(f"Error in expire loop: {e}")

            try:
                await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                break

    async def _process_loop(self) -> None:
        """Background loop for processing pending transcripts."""
        interval_seconds = self.config.transcript_processing_interval_minutes * 60

        while self._running:
            try:
                await self._process_pending_transcripts()
            except Exception as e:
                logger.error(f"Error in process loop: {e}")

            try:
                await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                break

    async def _expire_stale_sessions(self) -> int:
        """Expire sessions that have been inactive too long."""
        count = self.session_manager.expire_stale_sessions(
            timeout_hours=self.config.stale_session_timeout_hours
        )
        return count

    async def _process_pending_transcripts(self) -> int:
        """Process transcripts for expired sessions."""
        sessions = self.session_manager.get_pending_transcript_sessions(
            limit=self.config.transcript_processing_batch_size
        )

        if not sessions:
            return 0

        processed = 0
        for session in sessions:
            try:
                await self._process_session_transcript(session.id, session.jsonl_path)
                self.session_manager.mark_transcript_processed(session.id)
                processed += 1
                logger.debug(f"Processed transcript for session {session.id}")
            except Exception as e:
                logger.error(f"Failed to process transcript for {session.id}: {e}")

        if processed > 0:
            logger.info(f"Processed {processed} session transcripts")

        return processed

    async def _process_session_transcript(
        self, session_id: str, jsonl_path: str | None
    ) -> None:
        """
        Process a full transcript for a session.

        Reads the entire transcript and stores messages.
        Uses idempotent upsert so re-processing is safe.

        Args:
            session_id: Session ID
            jsonl_path: Path to transcript JSONL file
        """
        if not jsonl_path or not os.path.exists(jsonl_path):
            logger.warning(f"Transcript not found for session {session_id}: {jsonl_path}")
            return

        # Read entire file
        try:
            with open(jsonl_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            logger.error(f"Error reading transcript {jsonl_path}: {e}")
            raise

        if not lines:
            return

        # Parse all lines
        parser = ClaudeTranscriptParser()
        messages = parser.parse_lines(lines, start_index=0)

        if not messages:
            return

        # Store messages (upsert - safe for re-processing)
        await self.message_manager.store_messages(session_id, messages)

        # Update processing state
        await self.message_manager.update_state(
            session_id=session_id,
            byte_offset=sum(len(line.encode("utf-8")) for line in lines),
            message_index=messages[-1].index,
        )

"""
Session lifecycle manager.

Handles background jobs for:
- Expiring stale sessions
- Processing transcripts for expired sessions
"""

import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from gobby.config.sessions import SessionLifecycleConfig
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.sessions.transcripts.gemini import GeminiTranscriptParser
from gobby.storage.database import DatabaseProtocol
from gobby.storage.session_messages import LocalSessionMessageManager
from gobby.storage.sessions import LocalSessionManager

logger = logging.getLogger(__name__)


class SessionLifecycleManager:
    """
    Manages session lifecycle background jobs.

    Two independent jobs:
    1. expire_stale_sessions - marks old active/paused sessions as expired
    2. process_pending_transcripts - processes transcripts for expired sessions
    """

    def __init__(
        self,
        db: DatabaseProtocol,
        config: SessionLifecycleConfig,
        memory_manager: Any | None = None,
        llm_service: Any | None = None,
        memory_sync_manager: Any | None = None,
    ):
        self.db = db
        self.config = config
        self.session_manager = LocalSessionManager(db)
        self.message_manager = LocalSessionMessageManager(db)
        self.memory_manager = memory_manager
        self.llm_service = llm_service
        self.memory_sync_manager = memory_sync_manager
        self._memory_extraction_config: Any = None

        self._running = False
        self._expire_task: asyncio.Task[None] | None = None
        self._process_task: asyncio.Task[None] | None = None

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
                await self._purge_soft_deleted_definitions()
            except Exception as e:
                logger.error(f"Error purging soft-deleted definitions: {e}")

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
        """Pause inactive active sessions and expire stale sessions."""
        # First, pause active sessions that have been idle too long
        # This catches orphaned sessions that never got AFTER_AGENT hook
        paused = self.session_manager.pause_inactive_active_sessions(
            timeout_minutes=self.config.active_session_pause_minutes
        )

        # Then expire sessions that have been paused/active for too long
        expired = self.session_manager.expire_stale_sessions(
            timeout_hours=self.config.stale_session_timeout_hours
        )

        # Clean up stale prompt files (run in thread to avoid blocking)
        await asyncio.to_thread(self._cleanup_prompt_files)

        return paused + expired

    def _cleanup_prompt_files(self, max_age_seconds: int = 3600) -> int:
        """Delete prompt files older than max_age_seconds.

        Prompt files are read immediately by spawned agents, so any file
        older than 1 hour is safe to remove. Age-based cleanup also catches
        orphaned files from crashed sessions.
        """
        prompt_dir = Path(tempfile.gettempdir()) / "gobby-prompts"
        if not prompt_dir.is_dir():
            return 0

        now = time.time()
        removed = 0
        try:
            for path in prompt_dir.iterdir():
                try:
                    if now - path.stat().st_mtime > max_age_seconds:
                        path.unlink()
                        removed += 1
                except OSError:
                    pass
        except OSError:
            pass  # Handle directory access errors

        if removed > 0:
            logger.info(f"Cleaned up {removed} stale prompt file(s)")
        return removed

    async def _purge_soft_deleted_definitions(self) -> None:
        """Permanently remove definitions that were soft-deleted more than 30 days ago."""
        try:
            from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

            wf_mgr = LocalWorkflowDefinitionManager(self.db)
            wf_mgr.purge_deleted(older_than_days=30)
        except Exception as e:
            logger.error(f"Failed to purge soft-deleted definitions: {e}")

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

    async def _extract_memories_if_needed(self, session_id: str) -> None:
        """Extract memories from a processed session transcript.

        Safety net for ungraceful exits — if on_session_end never fired,
        this catches memories during background transcript processing.
        """
        if not self.memory_manager or not self.llm_service:
            return

        if not getattr(self.memory_manager, "config", None):
            return

        if not self.memory_manager.config.enabled:
            return

        try:
            from gobby.memory.extractor import SessionMemoryExtractor

            extractor = SessionMemoryExtractor(
                memory_manager=self.memory_manager,
                session_manager=self.session_manager,
                llm_service=self.llm_service,
                config=getattr(self, "_memory_extraction_config", None),
            )

            candidates = await extractor.extract(
                session_id=session_id,
                max_memories=5,
            )

            if candidates:
                logger.info(
                    f"Extracted {len(candidates)} memories from expired session {session_id}"
                )

                # Export to JSONL if sync manager available
                if self.memory_sync_manager:
                    try:
                        await self.memory_sync_manager.export_to_files()
                    except Exception as e:
                        logger.warning(f"Memory sync export after extraction failed: {e}")

        except Exception as e:
            logger.warning(f"Memory extraction failed for session {session_id}: {e}")

    async def _generate_summaries_if_needed(self, session_id: str) -> None:
        """Generate summaries for a session that's missing them.

        Safety net for ungraceful exits — if on_session_end or /clear never
        triggered summary generation, this catches it during background
        transcript processing.
        """
        if not self.llm_service:
            return

        session = self.session_manager.get(session_id)
        if not session or session.summary_markdown:
            return

        # Only generate if there's a transcript to read
        if not session.jsonl_path:
            return

        try:
            from gobby.sessions.summarize import generate_session_summaries

            await generate_session_summaries(
                session_id=session_id,
                session_manager=self.session_manager,
                llm_service=self.llm_service,
                db=self.db,
                write_file=True,
                set_handoff_ready=False,  # already expired, don't change status
            )
        except Exception as e:
            logger.warning(f"Summary generation failed for session {session_id}: {e}")

    async def _process_session_transcript(self, session_id: str, jsonl_path: str | None) -> None:
        """
        Process a full transcript for a session.

        Reads the entire transcript and stores messages.
        Aggregates token usage and costs.
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
            with open(jsonl_path, encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            logger.error(f"Error reading transcript {jsonl_path}: {e}")
            raise

        if not lines:
            return

        # Parse all lines
        session = self.session_manager.get(session_id)
        if not session:
            return

        # Choose parser based on source
        # Default to Claude for backward compatibility or safety
        # But we should rely on session.source if possible
        parser: Any = ClaudeTranscriptParser()
        if session.source == "gemini":
            parser = GeminiTranscriptParser()
        elif session.source == "codex":
            parser = CodexTranscriptParser()
        elif session.source == "antigravity":
            parser = ClaudeTranscriptParser()
        # Default (claude or unknown) uses Claude transcript format

        messages = parser.parse_lines(lines, start_index=0)

        if not messages:
            return

        # Store messages (upsert - safe for re-processing)
        await self.message_manager.store_messages(session_id, messages)

        # Aggregate usage
        input_tokens = 0
        output_tokens = 0
        cache_creation_tokens = 0
        cache_read_tokens = 0
        total_cost_usd = 0.0

        for msg in messages:
            if msg.usage:
                input_tokens += msg.usage.input_tokens
                output_tokens += msg.usage.output_tokens
                cache_creation_tokens += msg.usage.cache_creation_tokens
                cache_read_tokens += msg.usage.cache_read_tokens
                if msg.usage.total_cost_usd:
                    total_cost_usd += msg.usage.total_cost_usd

        # Update session with aggregated usage
        # We only update if we found some usage, to avoid overwriting with zeros if re-processing
        # (though re-processing from scratch IS the source of truth, so zeros might be correct if no usage found)
        # Actually, let's always update to ensure consistency with the file
        self.session_manager.update_usage(
            session_id=session_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            total_cost_usd=total_cost_usd,
        )

        # Update processing state
        await self.message_manager.update_state(
            session_id=session_id,
            byte_offset=sum(len(line.encode("utf-8")) for line in lines),
            message_index=messages[-1].index,
        )

        # Extract memories from transcript (ungraceful exit fallback)
        await self._extract_memories_if_needed(session_id)

        # Generate summaries if missing (ungraceful exit safety net)
        await self._generate_summaries_if_needed(session_id)

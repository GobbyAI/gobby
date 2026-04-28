"""
Session lifecycle manager.

Handles background jobs for:
- Expiring stale sessions
- Processing transcripts for expired sessions
"""

import asyncio
import json
import logging
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gobby.app_context import get_app_context
from gobby.config.sessions import SessionLifecycleConfig
from gobby.sessions.summarize import TURN_PATTERN
from gobby.sessions.transcript_archive import backup_transcript
from gobby.sessions.transcripts.base import ParsedMessage
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.sessions.transcripts.droid import DroidTranscriptParser
from gobby.sessions.transcripts.gemini import GeminiTranscriptParser
from gobby.sessions.transcripts.qwen import QwenTranscriptParser
from gobby.storage.database import DatabaseProtocol
from gobby.storage.sessions import SessionManager
from gobby.storage.token_events import (
    TokenEvent,
    TokenEventStore,
    build_token_event_payload,
    canonicalize_event_timestamp,
)

logger = logging.getLogger(__name__)


def _session_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return 0


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
        kg_queue_config: Any | None = None,
    ):
        self.db = db
        self.config = config
        self.session_manager = SessionManager(db)
        self.token_event_store = TokenEventStore(db)
        self.memory_manager = memory_manager
        self.llm_service = llm_service
        self.memory_sync_manager = memory_sync_manager
        self._kg_queue_config = kg_queue_config

        self._running = False
        self._expire_task: asyncio.Task[None] | None = None
        self._process_task: asyncio.Task[None] | None = None
        self._kg_queue_task: asyncio.Task[None] | None = None

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

        # Start KG queue processing job (if memory manager has KG service)
        if self.memory_manager and getattr(self.memory_manager, "kg_service", None):
            self._kg_queue_task = asyncio.create_task(
                self._kg_queue_loop(),
                name="session-lifecycle-kg-queue",
            )

        kg_interval = (
            getattr(self._kg_queue_config, "interval_minutes", 30) if self._kg_queue_config else 30
        )
        logger.info(
            f"SessionLifecycleManager started "
            f"(expire every {self.config.expire_check_interval_minutes}m, "
            f"process every {self.config.transcript_processing_interval_minutes}m, "
            f"kg_queue every {kg_interval}m)"
        )

    async def stop(self) -> None:
        """Stop background jobs."""
        self._running = False

        tasks = [t for t in [self._expire_task, self._process_task, self._kg_queue_task] if t]
        for task in tasks:
            task.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._expire_task = None
        self._process_task = None
        self._kg_queue_task = None

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

    async def _kg_queue_loop(self) -> None:
        """Background loop for processing pending KG graph memories."""
        interval_minutes = (
            getattr(self._kg_queue_config, "interval_minutes", 30) if self._kg_queue_config else 30
        )
        batch_size = (
            getattr(self._kg_queue_config, "batch_size", 20) if self._kg_queue_config else 20
        )
        interval_seconds = interval_minutes * 60

        while self._running:
            try:
                await self._process_pending_graph_memories(batch_size)
            except Exception as e:
                logger.error(f"Error in KG queue loop: {e}")

            try:
                await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                break

    async def _process_pending_graph_memories(self, batch_size: int = 20) -> int:
        """Process queued memories for KG extraction.

        Runs on a slow cadence (default 30 min). Processes memories
        sequentially to avoid bursty LLM calls.
        """
        if not self.memory_manager:
            return 0

        kg_service = getattr(self.memory_manager, "kg_service", None)
        if not kg_service:
            return 0

        pending = await asyncio.to_thread(
            self.memory_manager.get_pending_graph_memories, limit=batch_size
        )
        if not pending:
            return 0

        processed = 0
        for memory in pending:
            try:
                result = await kg_service.add_to_graph(
                    memory.content,
                    memory_id=memory.id,
                    project_id=memory.project_id,
                )
                if result.status in ("success", "noop_no_entities"):
                    await asyncio.to_thread(self.memory_manager.mark_graph_processed, memory.id)
                    processed += 1
            except Exception as e:
                logger.warning(f"KG processing failed for memory {memory.id}: {e}")

        if processed > 0:
            logger.info(f"Processed {processed} memories for knowledge graph")

        return processed

    async def _expire_stale_sessions(self) -> int:
        """Run the full session expiry pipeline, including zero-message cleanup."""
        # First, pause active sessions that have been idle too long
        # This catches orphaned sessions that never got AFTER_AGENT hook
        paused = self.session_manager.pause_inactive_active_sessions(
            timeout_minutes=self.config.active_session_pause_minutes
        )

        # Expire orphaned handoff_ready sessions (legitimate handoffs complete
        # within seconds, so 30 min is generous). This catches sessions that
        # never got picked up by a child session.
        orphaned = self.session_manager.expire_orphaned_handoff_sessions(timeout_minutes=30)

        # Then expire sessions that have been paused/active for too long
        expired = self.session_manager.expire_stale_sessions(
            timeout_hours=self.config.stale_session_timeout_hours
        )

        # Zero-message sessions created by spurious SESSION_START events can be
        # cleaned up much faster than the normal 24h stale-session sweep.
        fast_expired = self.session_manager.expire_empty_sessions(timeout_hours=2)
        pruned = self.session_manager.prune_empty_sessions(min_age_hours=1)

        # Clean up stale prompt files (run in thread to avoid blocking)
        await asyncio.to_thread(self._cleanup_prompt_files)

        return paused + orphaned + expired + fast_expired + pruned

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
        """Process transcripts for expired sessions.

        Runs memory extraction and summary generation as separate steps
        OUTSIDE _process_session_transcript so they execute even when the
        JSONL file has already been deleted.  transcript_processed is only
        set when summaries have been generated (or LLM is unavailable),
        allowing retry on the next cycle if summary generation fails.
        """
        sessions = self.session_manager.get_pending_transcript_sessions(
            limit=self.config.transcript_processing_batch_size
        )

        if not sessions:
            return 0

        archive_dir = getattr(self.config, "transcript_archive_dir", None)

        processed = 0
        for session in sessions:
            agent_depth = getattr(session, "agent_depth", 0) or 0
            source = getattr(session, "source", "") or ""

            # Turn count fallback: subagents get 1 turn, short Q&A gets 1-2.
            # By 3+ turns there's likely something worth remembering.
            digest = getattr(session, "digest_markdown", None)
            turn_count = len(TURN_PATTERN.findall(digest)) if isinstance(digest, str) else 0
            skip_llm = agent_depth > 0 or source in ("pipeline", "cron") or turn_count < 3

            # Step 1: Process transcript (reads JSONL, stores messages, aggregates usage)
            try:
                await self._process_session_transcript(session.id, session.transcript_path)
            except Exception as e:
                logger.error(f"Failed to process transcript for {session.id}: {e}")

            # If transcript file is gone, no point retrying — mark processed and move on
            if not session.transcript_path or not os.path.exists(session.transcript_path):
                self.session_manager.mark_transcript_processed(session.id)
                processed += 1
                logger.info(
                    f"Marked session {session.id} as processed "
                    f"(transcript file missing, no further processing possible)"
                )
                continue

            # Skip LLM-heavy steps for non-human sessions — subagents, pipelines,
            # and cron sessions are ephemeral and not worth the token cost.
            if skip_llm:
                self.session_manager.mark_transcript_processed(session.id)
                processed += 1
                logger.debug(
                    f"Processed transcript for {source} session {session.id} "
                    f"(depth={agent_depth}, skipped summary)"
                )
                continue

            # Step 2: Generate summaries (best-effort)
            try:
                await self._generate_summaries_if_needed(session.id)
            except Exception as e:
                logger.warning(f"Summary generation failed for {session.id}: {e}")

            # Step 3: Only mark as processed if summaries succeeded or LLM is unavailable.
            refreshed = self.session_manager.get(session.id)
            if refreshed and (refreshed.summary_markdown or not self.llm_service):
                self.session_manager.mark_transcript_processed(session.id)
                processed += 1
                logger.debug(f"Processed transcript for session {session.id}")
            else:
                logger.info(
                    f"Deferring transcript_processed for {session.id} — summaries not yet generated"
                )

            # Step 4: Best-effort backup of the transcript archive
            # On success, purge DB messages (gzip is now the source of truth)
            if session.transcript_path and session.external_id:
                try:
                    archive_path = await asyncio.to_thread(
                        backup_transcript,
                        session.external_id,
                        session.transcript_path,
                        archive_dir,
                    )
                    if archive_path:
                        logger.debug(
                            f"Archived transcript for session {session.id} "
                            f"(archived to {archive_path})"
                        )
                    else:
                        logger.warning(f"Transcript backup returned None for {session.id}")
                except Exception as e:
                    logger.warning(f"Transcript backup failed for {session.id}: {e}")

        if processed > 0:
            logger.info(f"Processed {processed} session transcripts")

        return processed

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
        if not session.transcript_path:
            return

        try:
            from gobby.sessions.summarize import generate_session_summaries

            await generate_session_summaries(
                session_id=session_id,
                session_manager=self.session_manager,
                llm_service=self.llm_service,
                db=self.db,
                set_handoff_ready=False,  # already expired, don't change status
            )
        except Exception as e:
            logger.warning(f"Summary generation failed for session {session_id}: {e}")

    async def _process_session_transcript(
        self, session_id: str, transcript_path: str | None
    ) -> None:
        """
        Process a full transcript for a session.

        Reads the entire transcript and stores messages.
        Aggregates token usage.
        Uses idempotent upsert so re-processing is safe.

        Args:
            session_id: Session ID
            transcript_path: Path to transcript JSONL file
        """
        if not transcript_path or not os.path.exists(transcript_path):
            logger.warning(f"Transcript not found for session {session_id}: {transcript_path}")
            return

        # Read entire file
        try:
            with open(transcript_path, encoding="utf-8") as f:
                raw = f.read()
        except Exception as e:
            logger.error(f"Error reading transcript {transcript_path}: {e}")
            raise

        if not raw.strip():
            return

        # Parse all lines
        session = self.session_manager.get(session_id)
        if not session:
            return

        # Choose parser based on source
        # Default to Claude for backward compatibility or safety
        # But we should rely on session.source if possible
        parser: Any = ClaudeTranscriptParser(session_id=session_id)
        if session.source == "gemini":
            parser = GeminiTranscriptParser(session_id=session_id)
        elif session.source == "qwen":
            parser = QwenTranscriptParser(session_id=session_id)
        elif session.source == "codex":
            parser = CodexTranscriptParser(session_id=session_id)
        elif session.source == "droid":
            parser = DroidTranscriptParser(
                session_id=session_id,
                transcript_path=session.transcript_path,
            )
        # Default (claude or unknown) uses Claude transcript format

        # Gemini/Qwen store sessions as single JSON files, not JSONL.
        # Dispatch to parse_session_json() for .json files so the parser
        # can iterate the messages array instead of treating the whole
        # file as one malformed JSONL line.
        if transcript_path.endswith(".json") and hasattr(parser, "parse_session_json"):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON in transcript {transcript_path}: {e}")
                return
            messages = parser.parse_session_json(data)
        else:
            # parse_lines may yield a mix of ParsedMessage and ParsedToolEvent
            # records (Codex MCP tool-call lifecycle); this token-event path
            # only consumes ParsedMessage fields (model, usage, message_id).
            parsed_records = parser.parse_lines(raw.splitlines(keepends=True), start_index=0)
            messages = [r for r in parsed_records if isinstance(r, ParsedMessage)]

        if not messages:
            return

        # Replace any synthetic migration rows with real transcript events as soon as
        # we have a parseable transcript for this session.
        self.token_event_store.delete_session_events(session_id, origin="backfill")
        self.token_event_store.delete_session_events(session_id, origin="transcript")

        session_project_id = session.project_id if isinstance(session.project_id, str) else None
        session_source = session.source if isinstance(session.source, str) else "unknown"
        session_context_window = (
            session.context_window if isinstance(session.context_window, int) else None
        )
        session_model = session.model if isinstance(session.model, str) and session.model else None
        last_model: str | None = session_model
        running_totals = self.token_event_store.get_session_totals(session_id)
        ws_server = None
        app_ctx = get_app_context()
        if app_ctx is not None:
            ws_server = app_ctx.websocket_server
        saw_usage = False

        for msg in messages:
            message_model = msg.model if isinstance(msg.model, str) and msg.model else None
            if message_model:
                last_model = message_model

            usage = msg.usage
            if usage is None:
                continue

            if not all(
                isinstance(getattr(usage, field, 0), int)
                for field in (
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_tokens",
                    "cache_read_tokens",
                )
            ):
                continue

            if (
                usage.input_tokens == 0
                and usage.output_tokens == 0
                and usage.cache_creation_tokens == 0
                and usage.cache_read_tokens == 0
            ):
                continue
            saw_usage = True

            event_timestamp = getattr(msg, "timestamp", None)
            if not isinstance(event_timestamp, datetime):
                event_timestamp = datetime.now(UTC)
            message_id = getattr(msg, "message_id", None)
            if not isinstance(message_id, str) or not message_id:
                message_id = None
            content_type = getattr(msg, "content_type", None)
            metadata = {"content_type": content_type} if isinstance(content_type, str) else None
            event_model = message_model or last_model

            event = TokenEvent(
                session_id=session_id,
                project_id=session_project_id,
                message_id=message_id,
                source=session_source,
                origin="transcript",
                model=event_model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_creation_tokens=usage.cache_creation_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                context_window=session_context_window,
                event_at=canonicalize_event_timestamp(event_timestamp),
                metadata=metadata,
            )
            inserted = self.token_event_store.record(event)
            if inserted:
                running_totals["input_tokens"] += usage.input_tokens
                running_totals["output_tokens"] += usage.output_tokens
                running_totals["cache_creation_tokens"] += usage.cache_creation_tokens
                running_totals["cache_read_tokens"] += usage.cache_read_tokens

                if ws_server is not None:
                    try:
                        await ws_server.broadcast_token_event(
                            build_token_event_payload(
                                {
                                    "session_id": session_id,
                                    "project_id": session_project_id,
                                    "message_id": message_id,
                                    "source": session_source,
                                    "origin": "transcript",
                                    "event_at": canonicalize_event_timestamp(event_timestamp),
                                    "model": event_model,
                                    "model_family": event.normalized_model_family(),
                                    "input_tokens": usage.input_tokens,
                                    "output_tokens": usage.output_tokens,
                                    "cache_creation_tokens": usage.cache_creation_tokens,
                                    "cache_read_tokens": usage.cache_read_tokens,
                                    "context_window": session_context_window,
                                },
                                session_totals=running_totals,
                            )
                        )
                    except Exception:
                        logger.error(
                            "Failed to broadcast transcript token event for session %s",
                            session_id,
                            exc_info=True,
                        )

        if not saw_usage and (
            _session_int(getattr(session, "usage_input_tokens", 0)) > 0
            or _session_int(getattr(session, "usage_output_tokens", 0)) > 0
            or _session_int(getattr(session, "usage_cache_creation_tokens", 0)) > 0
            or _session_int(getattr(session, "usage_cache_read_tokens", 0)) > 0
        ):
            logger.debug(
                "Transcript yielded no token events for %s; preserving existing session totals",
                session_id,
            )
            return

        totals = self.token_event_store.get_session_totals(session_id)
        if saw_usage and not any(totals.values()) and any(running_totals.values()):
            totals = dict(running_totals)

        # Update session with aggregated usage
        self.session_manager.update_usage(
            session_id=session_id,
            input_tokens=totals["input_tokens"],
            output_tokens=totals["output_tokens"],
            cache_creation_tokens=totals["cache_creation_tokens"],
            cache_read_tokens=totals["cache_read_tokens"],
            context_window=session_context_window,
            model=last_model,
        )

        # NOTE: Memory extraction and summary generation are now called from
        # _process_pending_transcripts (the caller), not here.  This ensures
        # they run even when the JSONL file has already been deleted and this
        # method returns early at the file-existence check.

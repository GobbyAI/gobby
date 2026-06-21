"""Lifecycle and registration behavior for session message processing."""

from __future__ import annotations

import asyncio
import logging
import os

from gobby.sessions.message_stats import MessageStats
from gobby.sessions.processor_types import ProcessorHost
from gobby.sessions.transcript_index import TranscriptIndexAppender, load_index_sidecar
from gobby.sessions.transcript_index_resume import hydrate_appender_from_index
from gobby.sessions.transcripts import get_parser

logger = logging.getLogger(__name__)


class ProcessorLifecycleMixin:
    _task: asyncio.Task[None] | None

    async def start(self: ProcessorHost) -> None:
        """Start the processing loop."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("SessionMessageProcessor started")

    async def stop(self: ProcessorHost) -> None:
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
        self: ProcessorHost, session_id: str, transcript_path: str, source: str = "claude"
    ) -> None:
        """
        Register a session for monitoring.

        The transcript file may not exist yet at registration time -- Codex CLI
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
        transcript_exists = os.path.exists(transcript_path)
        if not transcript_path.endswith(".json"):
            appender = TranscriptIndexAppender(
                source,
                session_id,
                transcript_path,
            )
            self._index_appenders[session_id] = appender
            if transcript_exists:
                self._hydrate_registration_from_sidecar(
                    session_id, transcript_path, source, appender
                )
        if transcript_exists:
            logger.debug("Registered session %s for processing (%s)", session_id, source)
        else:
            logger.debug(
                "Registered session %s for processing (%s); "
                "transcript not yet on disk, poll loop will catch it: %s",
                session_id,
                source,
                transcript_path,
            )

    async def flush_session(self: ProcessorHost, session_id: str) -> None:
        """Force an immediate processing pass for a single session.

        Useful when stats need to be up-to-date before reading them
        (e.g., at SESSION_END before completing an agent run).
        """
        transcript_path = self._active_sessions.get(session_id)
        if transcript_path:
            await self._process_session(session_id, transcript_path)

    def unregister_session(self: ProcessorHost, session_id: str) -> None:
        """Stop monitoring a session."""
        if session_id in self._active_sessions:
            del self._active_sessions[session_id]
            if session_id in self._parsers:
                del self._parsers[session_id]
            self._last_mtime.pop(session_id, None)
            self._stats.pop(session_id, None)
            self._stats_hydration_skipped.discard(session_id)
            self._byte_offsets.pop(session_id, None)
            self._message_indices.pop(session_id, None)
            self._index_appenders.pop(session_id, None)
            logger.debug("Unregistered session %s", session_id)
        self._render_states.pop(session_id, None)

    def _revive_expired_terminal_session(self: ProcessorHost, session_id: str) -> None:
        """Repair false-expired terminal rows when transcript activity resumes."""
        if not self.session_manager:
            return
        revive = getattr(self.session_manager, "revive_expired_terminal_session", None)
        if not callable(revive):
            return
        try:
            revive(session_id)
        except Exception:
            logger.debug(
                "Failed to revive expired terminal session %s from transcript activity",
                session_id,
                exc_info=True,
            )

    async def _loop(self: ProcessorHost) -> None:
        """Main processing loop."""
        while self._running:
            try:
                await self._process_all_sessions()
            except Exception as e:
                logger.error("Error in SessionMessageProcessor loop: %s", e)

            await asyncio.sleep(self.poll_interval)

    async def _process_all_sessions(self: ProcessorHost) -> None:
        """Process all registered sessions."""
        sessions = list(self._active_sessions.items())

        for session_id, transcript_path in sessions:
            try:
                await self._process_session(session_id, transcript_path)
            except Exception as e:
                logger.error("Failed to process session %s: %s", session_id, e, exc_info=True)

    def _hydrate_registration_from_sidecar(
        self: ProcessorHost,
        session_id: str,
        transcript_path: str,
        source: str,
        appender: TranscriptIndexAppender,
    ) -> None:
        try:
            st = os.stat(transcript_path)
        except OSError:
            return

        index = load_index_sidecar(
            transcript_path,
            source,
            session_id,
            seek_mode="byte",
            mtime_ns=st.st_mtime_ns,
            size=st.st_size,
        )
        if index is None:
            return

        hydrate_appender_from_index(appender, index)
        self._byte_offsets[session_id] = index.size
        next_parser_index = (
            index.next_parser_index
            if index.next_parser_index is not None
            else index.parsed_message_count
        )
        self._message_indices[session_id] = next_parser_index - 1

        if index.session_stats is None:
            self._stats.pop(session_id, None)
            self._stats_hydration_skipped.add(session_id)
            return

        stats = MessageStats(
            message_count=index.session_stats["message_count"],
            turn_count=index.session_stats["turn_count"],
            tool_call_count=index.session_stats["tool_call_count"],
            last_assistant_content=index.session_stats["last_assistant_content"],
        )
        self._stats[session_id] = stats
        self._stats_hydration_skipped.discard(session_id)
        if self.session_manager:
            self.session_manager.update_stats(
                session_id,
                message_count=stats["message_count"],
                turn_count=stats["turn_count"],
                tool_call_count=stats["tool_call_count"],
                last_assistant_content=stats["last_assistant_content"],
            )

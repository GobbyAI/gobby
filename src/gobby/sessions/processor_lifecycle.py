"""Lifecycle and registration behavior for session message processing."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from gobby.sessions.message_stats import MessageStats
from gobby.sessions.observation_tracker import ObservationTracker
from gobby.sessions.processor_types import ProcessorHost
from gobby.sessions.transcript_index import (
    TranscriptIndexAppender,
    discard_index_sidecar,
    load_index_sidecar,
)
from gobby.sessions.transcript_index_resume import hydrate_appender_from_index
from gobby.sessions.transcript_paths import MISSING_TRANSCRIPT_PATH
from gobby.sessions.transcript_render_models import RenderState
from gobby.sessions.transcript_window import render_window
from gobby.sessions.transcripts import get_parser

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionFlushResult:
    """Outcome of an immediate session transcript processing pass."""

    flushed: bool
    error: str | None = None


class ProcessorLifecycleMixin:
    _task: asyncio.Task[None] | None
    _owner_loop: asyncio.AbstractEventLoop | None

    async def start(self: ProcessorHost) -> None:
        """Start the processing loop."""
        if self._running:
            return

        self._running = True
        self._owner_loop = asyncio.get_running_loop()
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
        registered_path = self._active_sessions.get(session_id)
        if registered_path == transcript_path:
            return
        if registered_path is not None:
            self.unregister_session(session_id)

        self._active_sessions[session_id] = transcript_path
        self._session_sources[session_id] = source
        self._parsers[session_id] = get_parser(
            source,
            session_id=session_id,
            transcript_path=transcript_path,
        )
        try:
            st = os.stat(transcript_path)
            transcript_exists = True
            self._transcript_file_state[session_id] = (st.st_dev, st.st_ino, st.st_mtime_ns)
            self._mirror_start_offsets[session_id] = st.st_size
        except OSError:
            transcript_exists = False
            self._mirror_start_offsets[session_id] = 0
        appender = TranscriptIndexAppender(
            source,
            session_id,
            transcript_path,
            observation_tracker=ObservationTracker(self._observation_store),
        )
        self._index_appenders[session_id] = appender
        if transcript_exists:
            self._hydrate_registration_from_sidecar(session_id, transcript_path, source, appender)
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

    async def flush_session(self: ProcessorHost, session_id: str) -> SessionFlushResult:
        """Force an immediate processing pass for a single session.

        Useful when stats need to be up-to-date before reading them
        (e.g., at SESSION_END before completing an agent run).
        """
        owner_loop = self._owner_loop
        if owner_loop is not None and owner_loop is not asyncio.get_running_loop():
            # Processor state and its per-session asyncio locks belong to the loop that
            # started it; wake delivery and hook handlers call in from other loops.
            if not owner_loop.is_running():
                return SessionFlushResult(flushed=False, error="processor loop is not running")
            return await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(self.flush_session(session_id), owner_loop)
            )
        transcript_path = self._active_sessions.get(session_id)
        if transcript_path is None:
            return SessionFlushResult(flushed=False, error="session is not registered")

        try:
            # Each pass is bounded; a flush promises the whole transcript, so keep
            # passing until one reaches EOF. The lock is released between passes.
            while not await self._process_session(session_id, transcript_path, at_eof=True):
                pass
        except Exception as exc:
            logger.exception(
                "Failed to flush session transcript",
                extra={"session_id": session_id, "transcript_path": transcript_path},
            )
            return SessionFlushResult(flushed=False, error=str(exc))
        return SessionFlushResult(flushed=True)

    async def complete_turn(
        self: ProcessorHost, session_id: str, expected_final_text: str | None = None
    ) -> SessionFlushResult:
        """Read through turn-end evidence before publishing the final assistant group."""
        owner_loop = self._owner_loop
        if owner_loop is not None and owner_loop is not asyncio.get_running_loop():
            if not owner_loop.is_running():
                return SessionFlushResult(flushed=False, error="processor loop is not running")
            return await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(
                    self.complete_turn(session_id, expected_final_text), owner_loop
                )
            )
        transcript_path = self._active_sessions.get(session_id)
        if transcript_path is None:
            return SessionFlushResult(flushed=False, error="session is not registered")
        lock = self._processing_locks.setdefault(session_id, asyncio.Lock())
        deadline = asyncio.get_running_loop().time() + 5.0
        previous_content: tuple[str, str] | None = None
        stable_since = asyncio.get_running_loop().time()
        async with lock:
            while True:
                while not await self._process_session_unlocked(
                    session_id, transcript_path, at_eof=True
                ):
                    pass
                state = self._render_states.get(session_id)
                message = state.current_message if state else None
                now = asyncio.get_running_loop().time()
                current_content = (message.id, message.content) if message is not None else None
                if current_content != previous_content:
                    previous_content = current_content
                    stable_since = now
                if message is not None and message.role == "assistant":
                    ready = (
                        message.content.rstrip().endswith(expected_final_text.rstrip())
                        if expected_final_text
                        else now - stable_since >= 0.25
                    )
                    if ready:
                        # This provider hook confirms a new live completion,
                        # including a response whose prefix was present at registration.
                        self._mirror_eligible_ids.setdefault(session_id, set()).add(message.id)
                        await self._broadcast_rendered_session_message(
                            session_id, message.to_dict(), complete=True
                        )
                        # A provider can begin another assistant turn without a
                        # user record (for example, an automatic continuation).
                        # Its text must not reuse this completed group's ledger key.
                        assert state is not None
                        state.current_message = None
                        state.seen_content.clear()
                        return SessionFlushResult(flushed=True)
                if (
                    not expected_final_text
                    and (message is None or message.role != "assistant")
                    and now - stable_since >= 0.25
                ):
                    return SessionFlushResult(flushed=True)
                if asyncio.get_running_loop().time() >= deadline:
                    return SessionFlushResult(
                        flushed=False, error="final transcript text not yet present"
                    )
                await asyncio.sleep(0.05)

    async def reconcile_codex_transcript(
        self: ProcessorHost, session_id: str
    ) -> SessionFlushResult:
        """Catch up a registered terminal Codex rollout before completion checks."""
        registered_here = False
        if self._session_sources.get(session_id) != "codex":
            if self.session_manager is None:
                return SessionFlushResult(flushed=False, error="session is not registered as Codex")
            session = await self._run_db(self.session_manager.get, session_id)
            if session is None or getattr(session, "source", None) != "codex":
                return SessionFlushResult(flushed=False, error="session is not registered as Codex")
            transcript_path = getattr(session, "transcript_path", None)
            if (
                not isinstance(transcript_path, str)
                or not transcript_path
                or transcript_path == MISSING_TRANSCRIPT_PATH
            ):
                return SessionFlushResult(
                    flushed=False, error="Codex session transcript is unavailable"
                )
            self.register_session(session_id, transcript_path, source="codex")
            registered_here = True
        try:
            return await self.flush_session(session_id)
        finally:
            if registered_here:
                self.unregister_session(session_id)

    def unregister_session(self: ProcessorHost, session_id: str) -> None:
        """Stop monitoring a session."""
        was_registered = self._active_sessions.pop(session_id, None) is not None
        self._parsers.pop(session_id, None)
        self._session_sources.pop(session_id, None)
        self._transcript_file_state.pop(session_id, None)
        self._stats.pop(session_id, None)
        self._stats_hydration_skipped.discard(session_id)
        self._byte_offsets.pop(session_id, None)
        self._message_indices.pop(session_id, None)
        self._index_appenders.pop(session_id, None)
        self._render_states.pop(session_id, None)
        self._restored_render_sessions.discard(session_id)
        self._mirror_start_offsets.pop(session_id, None)
        self._mirror_eligible_ids.pop(session_id, None)
        if was_registered:
            logger.debug("Unregistered session %s", session_id)

    async def _reset_transcript_state(
        self: ProcessorHost, session_id: str, transcript_path: str
    ) -> None:
        """Reset incremental state after transcript truncation or replacement."""
        source = self._session_sources[session_id]
        self._parsers[session_id] = get_parser(
            source,
            session_id=session_id,
            transcript_path=transcript_path,
        )
        self._index_appenders[session_id] = TranscriptIndexAppender(
            source,
            session_id,
            transcript_path,
            observation_tracker=ObservationTracker(self._observation_store),
        )
        self._byte_offsets.pop(session_id, None)
        self._message_indices.pop(session_id, None)
        self._render_states.pop(session_id, None)
        self._stats.pop(session_id, None)
        self._restored_render_sessions.discard(session_id)
        self._mirror_eligible_ids.pop(session_id, None)
        try:
            self._mirror_start_offsets[session_id] = await asyncio.to_thread(
                os.path.getsize, transcript_path
            )
        except OSError:
            self._mirror_start_offsets[session_id] = 0
        self._stats_hydration_skipped.discard(session_id)
        await asyncio.to_thread(discard_index_sidecar, transcript_path)

        if self.session_manager:
            await self._run_db(
                self.session_manager.update_stats,
                session_id,
                message_count=0,
                turn_count=0,
                tool_call_count=0,
                last_assistant_content=None,
            )

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
                logger.exception("Failed to process session %s: %s", session_id, e)

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

        parser = self._parsers.get(session_id)
        index = load_index_sidecar(
            transcript_path,
            source,
            session_id,
            seek_mode="byte",
            mtime_ns=st.st_mtime_ns,
            size=st.st_size,
            allow_append=bool(getattr(parser, "supports_incremental_state", False)),
        )
        if index is None:
            return

        # The stats cursor may already be at EOF while its final assistant
        # response has not reached a turn-end hook. Restore that bounded group
        # so restart cannot discard it before the next completion evidence.
        tail = render_window(transcript_path, source, session_id, index, limit=1, offset=0)
        if tail.degraded:
            return
        if tail.groups:
            message = tail.groups[-1]
            state = RenderState(current_message=message)
            state.pre_window_tool_first_open = index.tool_first_open
            state.pre_window_boundary_index = index.boundaries[-1].parsed_index_start
            for block in message.content_blocks:
                for call in block.tool_calls or []:
                    if call.status == "completed":
                        state.remember_resolved_tool_call(call.id)
                    else:
                        state.pending_tool_calls[call.id] = call
                        state.tool_call_messages[call.id] = message
            self._render_states[session_id] = state
            self._restored_render_sessions.add(session_id)

        hydrate_appender_from_index(appender, index)
        self._parsers[session_id].hydrate_state(index.parser_state)
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

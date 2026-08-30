"""Transcript file processing for session message processing."""

from __future__ import annotations

import asyncio
import logging
import os
from copy import deepcopy
from typing import cast

import aiofiles
import psycopg

from gobby.sessions.message_stats import MessageStats
from gobby.sessions.observation_tracker import ObservationTracker
from gobby.sessions.processor_types import ProcessorHost
from gobby.sessions.transcript_normalization import normalize_transcript_records
from gobby.sessions.transcript_renderer import RenderState, render_incremental
from gobby.sessions.transcripts.base import (
    ParsedMessage,
    ParsedToolEvent,
    TranscriptParser,
    apply_adjustment,
    raw_lines_from_texts,
)

logger = logging.getLogger(__name__)


def _parser_source(parser: object | None) -> str | None:
    source = getattr(parser, "cli_name", None)
    return source if isinstance(source, str) else None


def _parser_supports_incremental_state(parser: object | None) -> bool:
    return bool(getattr(parser, "supports_incremental_state", False))


def _parse_incremental_records(
    parser: TranscriptParser,
    lines: list[str],
    *,
    start_index: int,
) -> list[ParsedMessage | ParsedToolEvent]:
    if not _parser_supports_incremental_state(parser):
        return parser.parse_lines(lines, start_index=start_index)

    records: list[ParsedMessage | ParsedToolEvent] = []
    for event in parser.iter_parse_events(raw_lines_from_texts(lines), start_index):
        records.extend(event.records)
    for adjustment in parser.finalize():
        apply_adjustment(records, adjustment)
    return records


class ProcessorTranscriptMixin:
    def _filter_session_title_messages(
        self: ProcessorHost,
        messages: list[ParsedMessage],
    ) -> list[ParsedMessage]:
        """Filter provider-native title metadata out of conversation content."""
        return [m for m in messages if m.content_type != "session_title"]

    async def _process_parsed_batch(
        self: ProcessorHost,
        session_id: str,
        messages: list[ParsedMessage],
    ) -> MessageStats:
        """Run fallible batch work and roll back processor-local state on failure."""
        had_stats = session_id in self._stats
        previous_stats = deepcopy(self._stats.get(session_id))
        hydration_was_skipped = session_id in self._stats_hydration_skipped
        had_render_state = session_id in self._render_states
        previous_render_state = deepcopy(self._render_states.get(session_id))
        try:
            await self._persist_usage_events(session_id, messages)
            await self._render_and_broadcast_messages(
                session_id,
                messages,
                record_observations=True,
            )
            stats = cast(
                MessageStats,
                await self._run_db(self._accumulate_stats, session_id, messages),
            )
            if self.session_manager:
                await self._run_db(self.session_manager.touch, session_id)
                await self._run_db(
                    self.session_manager.update_stats,
                    session_id,
                    message_count=stats.get("message_count", 0),
                    turn_count=stats.get("turn_count", 0),
                    tool_call_count=stats.get("tool_call_count", 0),
                    last_assistant_content=stats.get("last_assistant_content"),
                )
            return stats
        except Exception:
            if had_stats:
                assert previous_stats is not None
                self._stats[session_id] = previous_stats
            else:
                self._stats.pop(session_id, None)
            if hydration_was_skipped:
                self._stats_hydration_skipped.add(session_id)
            else:
                self._stats_hydration_skipped.discard(session_id)
            if had_render_state:
                assert previous_render_state is not None
                self._render_states[session_id] = previous_render_state
            else:
                self._render_states.pop(session_id, None)
            raise

    async def _process_session(
        self: ProcessorHost,
        session_id: str,
        transcript_path: str,
        *,
        at_eof: bool = False,
    ) -> None:
        """Process a single session."""
        lock = self._processing_locks.setdefault(session_id, asyncio.Lock())
        try:
            async with lock:
                if self._active_sessions.get(session_id) != transcript_path:
                    return
                await self._process_session_unlocked(
                    session_id,
                    transcript_path,
                    at_eof=at_eof,
                )
        finally:
            if session_id not in self._active_sessions:
                self.unregister_session(session_id)

    async def _process_session_unlocked(
        self: ProcessorHost,
        session_id: str,
        transcript_path: str,
        *,
        at_eof: bool = False,
    ) -> None:
        """Process a single session while its processing lock is held."""
        if not await asyncio.to_thread(os.path.exists, transcript_path):
            return

        try:
            transcript_stat = await asyncio.to_thread(os.stat, transcript_path)
        except OSError as exc:
            logger.error(
                "Error stating transcript",
                extra={
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "error": str(exc),
                },
            )
            return

        last_offset = self._byte_offsets.get(session_id, 0)
        previous_state = self._transcript_file_state.get(session_id)
        current_state = (
            transcript_stat.st_dev,
            transcript_stat.st_ino,
            transcript_stat.st_mtime_ns,
        )
        transcript_reset = transcript_stat.st_size < last_offset
        if previous_state is not None:
            transcript_reset = transcript_reset or current_state[:2] != previous_state[:2]
            transcript_reset = transcript_reset or current_state[2] < previous_state[2]
        if transcript_reset:
            logger.info(
                "Transcript changed non-incrementally; resetting processor state",
                extra={"session_id": session_id, "transcript_path": transcript_path},
            )
            await self._reset_transcript_state(session_id, transcript_path)
            last_offset = 0
        self._transcript_file_state[session_id] = current_state

        last_index = self._message_indices.get(session_id, -1)
        new_lines: list[str] = []
        new_line_offsets: list[int] = []
        valid_offset = last_offset

        try:
            async with aiofiles.open(transcript_path, "rb") as f:
                await f.seek(last_offset)

                while True:
                    line_start = await f.tell()
                    raw_line = await f.readline()
                    if not raw_line:
                        break

                    if raw_line.endswith(b"\n"):
                        new_lines.append(raw_line.decode("utf-8", errors="replace"))
                        new_line_offsets.append(line_start)
                        valid_offset = await f.tell()
                    elif at_eof:
                        new_lines.append(raw_line.decode("utf-8", errors="replace"))
                        new_line_offsets.append(line_start)
                        valid_offset = await f.tell()
                    else:
                        break
        except (OSError, UnicodeDecodeError) as e:
            logger.error(
                "Error reading transcript",
                extra={
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "error": str(e),
                },
            )
            return

        if not new_lines:
            return

        await self._run_db(self._revive_expired_terminal_session, session_id)
        parser = self._parsers.get(session_id)
        if not parser:
            return

        parser_state = (
            parser.snapshot_state() if _parser_supports_incremental_state(parser) else None
        )
        try:
            raw_records = _parse_incremental_records(
                parser,
                new_lines,
                start_index=last_index + 1,
            )
            parsed_records = normalize_transcript_records(raw_records, _parser_source(parser))
            parsed_messages: list[ParsedMessage] = [
                r for r in parsed_records if isinstance(r, ParsedMessage)
            ]

            latest_parsed_index = parsed_messages[-1].index if parsed_messages else last_index
            parsed_messages = self._filter_session_title_messages(parsed_messages)
        except Exception:
            if parser_state is not None:
                parser.hydrate_state(parser_state)
            raise

        appender = self._index_appenders.get(session_id)
        pending_appender = None
        appender_stat: os.stat_result | None = None
        should_persist_appender = False
        if appender is not None:
            try:
                appender_stat = await asyncio.to_thread(os.stat, transcript_path)
                pending_appender = appender.clone()
                await self._run_db(
                    pending_appender.append_positioned_lines,
                    new_lines,
                    new_line_offsets,
                    mtime_ns=appender_stat.st_mtime_ns,
                    size=valid_offset,
                )
                should_persist_appender = valid_offset == appender_stat.st_size
            except (OSError, ValueError, psycopg.Error) as exc:
                logger.debug(
                    "Failed to update transcript index",
                    extra={
                        "session_id": session_id,
                        "transcript_path": transcript_path,
                        "error": str(exc),
                    },
                )

        if not parsed_messages:
            if pending_appender is not None:
                self._index_appenders[session_id] = pending_appender
            if latest_parsed_index > last_index:
                self._message_indices[session_id] = latest_parsed_index
            self._byte_offsets[session_id] = valid_offset
            if (
                pending_appender is not None
                and appender_stat is not None
                and should_persist_appender
            ):
                await self._persist_appender_snapshot(
                    session_id,
                    transcript_path,
                    pending_appender,
                    appender_stat,
                )
            return

        try:
            stats = await self._process_parsed_batch(session_id, parsed_messages)
        except Exception:
            if parser_state is not None:
                parser.hydrate_state(parser_state)
            raise

        if pending_appender is not None:
            pending_appender.index.session_stats = stats
            self._index_appenders[session_id] = pending_appender
            if appender_stat is not None and should_persist_appender:
                await self._persist_appender_snapshot(
                    session_id,
                    transcript_path,
                    pending_appender,
                    appender_stat,
                )

        self._byte_offsets[session_id] = valid_offset
        self._message_indices[session_id] = latest_parsed_index

        logger.debug(
            "Processed transcript messages",
            extra={
                "session_id": session_id,
                "transcript_path": transcript_path,
                "message_count": len(parsed_messages),
            },
        )

    async def _render_and_broadcast_messages(
        self: ProcessorHost,
        session_id: str,
        messages: list[ParsedMessage],
        *,
        record_observations: bool = False,
    ) -> None:
        # Mutated in place rather than copied. _process_parsed_batch, the only
        # production caller, already deep-copied this state three lines before
        # calling here and restores it if the batch raises, so a second copy
        # protected nothing the first does not -- and both ran on the event loop
        # (#20859).
        render_state = self._render_states.get(session_id) or RenderState()
        source = messages[0].source if messages else None
        observation_tracker = (
            ObservationTracker(self._observation_store) if record_observations else None
        )
        if observation_tracker is None:
            completed, render_state = render_incremental(
                messages,
                render_state,
                session_id=session_id,
                source=source,
                observation_tracker=None,
            )
        else:
            completed, render_state = await self._run_db(
                render_incremental,
                messages,
                render_state,
                session_id=session_id,
                source=source,
                observation_tracker=observation_tracker,
            )
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
        self._render_states[session_id] = render_state

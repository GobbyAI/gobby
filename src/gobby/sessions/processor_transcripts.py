"""Transcript file processing for session message processing."""

from __future__ import annotations

import asyncio
import json
import logging
import os

import aiofiles
import psycopg

from gobby.memory.digest import _can_replace_with_native_title
from gobby.memory.title_heuristics import normalize_native_title
from gobby.sessions.observation_tracker import ObservationTracker
from gobby.sessions.processor_types import ProcessorHost
from gobby.sessions.transcript_normalization import normalize_transcript_records
from gobby.sessions.transcript_renderer import RenderState, render_incremental
from gobby.sessions.transcripts.base import ParsedMessage

logger = logging.getLogger(__name__)


class ProcessorTranscriptMixin:
    def _extract_native_titles(
        self: ProcessorHost, session_id: str, messages: list[ParsedMessage]
    ) -> list[ParsedMessage]:
        """Extract CLI-native session titles, update the session, and return non-title messages.

        ``session_title`` messages are metadata, not conversation content — they
        must not inflate ``message_count`` or be rendered as chat cards. This
        method intercepts them before stats/render, calls ``update_title`` with
        ``title_source="native"`` when the precedence policy allows it, and
        returns the remaining messages for normal processing.
        """
        if not messages:
            return messages
        title_msgs = [m for m in messages if m.content_type == "session_title"]
        if not title_msgs:
            return messages

        if self.session_manager:
            try:
                session = self.session_manager.get(session_id)
                if session is not None and _can_replace_with_native_title(session):
                    # Use the last title message (latest ai-title update wins).
                    raw_title = title_msgs[-1].content
                    title = normalize_native_title(raw_title)
                    if title:
                        self.session_manager.update_title(
                            session_id,
                            title,
                            title_source="native",
                        )
            except (LookupError, RuntimeError, ValueError, psycopg.Error):
                logger.warning(
                    "Failed to sync native transcript title",
                    extra={"session_id": session_id},
                    exc_info=True,
                )

        return [m for m in messages if m.content_type != "session_title"]

    async def _process_session(self: ProcessorHost, session_id: str, transcript_path: str) -> None:
        """Process a single session."""
        if not await asyncio.to_thread(os.path.exists, transcript_path):
            return

        if transcript_path.endswith(".json"):
            await self._process_json_session(session_id, transcript_path)
            return

        last_offset = self._byte_offsets.get(session_id, 0)
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

        self._revive_expired_terminal_session(session_id)
        parser = self._parsers.get(session_id)
        if not parser:
            return

        source = getattr(parser, "cli_name", None)
        parsed_records = normalize_transcript_records(
            parser.parse_lines(new_lines, start_index=last_index + 1),
            source if isinstance(source, str) else None,
        )
        parsed_messages: list[ParsedMessage] = [
            r for r in parsed_records if isinstance(r, ParsedMessage)
        ]

        latest_parsed_index = parsed_messages[-1].index if parsed_messages else last_index
        parsed_messages = self._extract_native_titles(session_id, parsed_messages)

        self._byte_offsets[session_id] = valid_offset
        appender = self._index_appenders.get(session_id)
        appender_stat: os.stat_result | None = None
        should_persist_appender = False
        if appender is not None:
            try:
                appender_stat = await asyncio.to_thread(os.stat, transcript_path)
                appender.append_positioned_lines(
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
            if latest_parsed_index > last_index:
                self._message_indices[session_id] = latest_parsed_index
            if appender is not None and appender_stat is not None and should_persist_appender:
                self._persist_appender_snapshot(
                    session_id,
                    transcript_path,
                    appender,
                    appender_stat,
                )
            return

        stats = self._accumulate_stats(session_id, parsed_messages)
        if appender is not None:
            appender.index.session_stats = stats
            if appender_stat is not None and should_persist_appender:
                self._persist_appender_snapshot(
                    session_id,
                    transcript_path,
                    appender,
                    appender_stat,
                )

        if self.session_manager:
            self.session_manager.touch(session_id)
            self.session_manager.update_stats(
                session_id,
                message_count=stats.get("message_count", 0),
                turn_count=stats.get("turn_count", 0),
                tool_call_count=stats.get("tool_call_count", 0),
                last_assistant_content=stats.get("last_assistant_content"),
            )
            for msg in parsed_messages:
                if msg.model:
                    self.session_manager.update_model(session_id, msg.model)
                    break
        await self._persist_usage_events(session_id, parsed_messages)

        await self._render_and_broadcast_messages(session_id, parsed_messages)
        self._message_indices[session_id] = latest_parsed_index

        logger.debug(
            "Processed transcript messages",
            extra={
                "session_id": session_id,
                "transcript_path": transcript_path,
                "message_count": len(parsed_messages),
            },
        )

    async def _process_json_session(
        self: ProcessorHost, session_id: str, transcript_path: str
    ) -> None:
        """
        Process a JSON session file.

        Uses mtime to detect changes, reads the entire file, and parses
        all messages. Only stores messages newer than last_message_index.
        """
        try:
            current_mtime = await asyncio.to_thread(os.path.getmtime, transcript_path)
        except OSError:
            return

        last_mtime = self._last_mtime.get(session_id, 0.0)
        if current_mtime <= last_mtime:
            return

        try:
            async with aiofiles.open(transcript_path, encoding="utf-8") as f:
                raw = await f.read()
            data = json.loads(raw)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            logger.error(
                "Error reading JSON transcript",
                extra={
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "error": str(e),
                },
            )
            return

        if not isinstance(data, dict):
            logger.warning(
                "JSON transcript is not an object",
                extra={"session_id": session_id, "transcript_path": transcript_path},
            )
            return

        self._revive_expired_terminal_session(session_id)

        parser = self._parsers.get(session_id)
        if not parser or not hasattr(parser, "parse_session_json"):
            logger.warning(
                "No JSON-session transcript parser for session",
                extra={"session_id": session_id, "transcript_path": transcript_path},
            )
            return

        source = getattr(parser, "cli_name", None)
        all_messages = [
            r
            for r in normalize_transcript_records(
                parser.parse_session_json(data),
                source if isinstance(source, str) else None,
            )
            if isinstance(r, ParsedMessage)
        ]
        if not all_messages:
            self._last_mtime[session_id] = current_mtime
            return

        last_index = self._message_indices.get(session_id, -1)
        new_messages = [m for m in all_messages if m.index > last_index]
        latest_new_index = new_messages[-1].index if new_messages else last_index

        new_messages = self._extract_native_titles(session_id, new_messages)

        if not new_messages:
            if latest_new_index > last_index:
                self._message_indices[session_id] = latest_new_index
            self._last_mtime[session_id] = current_mtime
            return

        stats = self._accumulate_stats(session_id, new_messages)

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
        await self._persist_usage_events(session_id, new_messages)

        await self._render_and_broadcast_messages(
            session_id,
            new_messages,
            record_observations=True,
        )
        self._message_indices[session_id] = latest_new_index
        self._last_mtime[session_id] = current_mtime

        logger.debug(
            "Processed JSON transcript messages",
            extra={
                "session_id": session_id,
                "transcript_path": transcript_path,
                "message_count": len(new_messages),
            },
        )

    async def _render_and_broadcast_messages(
        self: ProcessorHost,
        session_id: str,
        messages: list[ParsedMessage],
        *,
        record_observations: bool = False,
    ) -> None:
        render_state = self._render_states.get(session_id, RenderState())
        source = messages[0].source if messages else None
        observation_tracker = (
            ObservationTracker(self._observation_store) if record_observations else None
        )
        completed, render_state = render_incremental(
            messages,
            render_state,
            session_id=session_id,
            source=source,
            observation_tracker=observation_tracker,
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

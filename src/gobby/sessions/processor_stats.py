"""Stat and render helpers for session message processing."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from typing import Any

from gobby.sessions.context_usage import context_window_from_raw_message
from gobby.sessions.message_stats import (
    MessageStats,
    accumulate_message_stats,
    empty_message_stats,
)
from gobby.sessions.processor_types import ProcessorHost
from gobby.sessions.transcript_index import TranscriptIndexAppender, persist_index_sidecar
from gobby.sessions.transcripts.base import ParsedMessage

logger = logging.getLogger(__name__)


class ProcessorStatsMixin:
    async def _feed_attached_session_tts(
        self: ProcessorHost,
        session_id: str,
        rendered: dict[str, Any],
        *,
        complete: bool,
    ) -> None:
        """Feed opt-in TTS without suppressing transcript broadcasts."""
        if self.websocket_server is None:
            return
        tts_feed = getattr(self.websocket_server, "feed_attached_session_tts", None)
        if not callable(tts_feed):
            return
        try:
            result = tts_feed(session_id, rendered, complete=complete)
            if inspect.isawaitable(result):
                await result
        except Exception:
            self._inc_counter("tts_feed_failures_total")
            logger.warning(
                "Attached-session TTS feed failed for session %s",
                session_id,
                exc_info=True,
            )

    async def _broadcast_rendered_session_message(
        self: ProcessorHost,
        session_id: str,
        rendered: dict[str, Any],
        *,
        complete: bool,
    ) -> None:
        if self.websocket_server is None:
            return
        await self._feed_attached_session_tts(session_id, rendered, complete=complete)
        await self.websocket_server.broadcast(
            {
                "type": "session_message",
                "session_id": session_id,
                "message": rendered,
                "complete": complete,
            }
        )

    def _accumulate_stats(
        self: ProcessorHost, session_id: str, messages: list[Any]
    ) -> MessageStats:
        """Accumulate incremental stats from parsed messages.

        Per-batch counts come from the shared message-stat helpers (also used
        by the lifecycle expiry path) and are folded into the running
        per-session totals so the live and batch writers cannot drift.
        """
        stats = self._stats.get(session_id)
        if stats is None:
            if session_id in self._stats_hydration_skipped:
                stats = self._stats_from_session_manager(session_id)
                self._stats_hydration_skipped.discard(session_id)
            else:
                stats = empty_message_stats()
        stats = accumulate_message_stats(stats, messages)
        self._stats[session_id] = stats
        return stats

    def _stats_from_session_manager(self: ProcessorHost, session_id: str) -> MessageStats:
        stats = empty_message_stats()
        if self.session_manager is None:
            return stats
        try:
            session = self.session_manager.get(session_id)
        except Exception:
            logger.debug("Failed to read existing stats for %s", session_id, exc_info=True)
            return stats
        if session is None:
            return stats

        message_count = getattr(session, "message_count", None)
        if isinstance(message_count, int) and not isinstance(message_count, bool):
            stats["message_count"] = max(0, message_count)
        turn_count = getattr(session, "turn_count", None)
        if isinstance(turn_count, int) and not isinstance(turn_count, bool):
            stats["turn_count"] = max(0, turn_count)
        tool_call_count = getattr(session, "tool_call_count", None)
        if isinstance(tool_call_count, int) and not isinstance(tool_call_count, bool):
            stats["tool_call_count"] = max(0, tool_call_count)
        last_assistant = getattr(session, "last_assistant_content", None)
        if isinstance(last_assistant, str):
            stats["last_assistant_content"] = last_assistant
        return stats

    async def _persist_appender_snapshot(
        self: ProcessorHost,
        session_id: str,
        transcript_path: str,
        appender: TranscriptIndexAppender,
        st: os.stat_result,
    ) -> None:
        # Writing the sidecar re-reads and SHA-256s every indexed byte of the
        # transcript. On a large transcript that held the loop for two seconds
        # at a time, at three quarters of the sampled stacks (#20845).
        try:
            snapshot = appender.snapshot(mtime_ns=st.st_mtime_ns, size=st.st_size)
            await asyncio.to_thread(persist_index_sidecar, transcript_path, snapshot)
        except Exception as exc:
            logger.debug("Failed to persist transcript index for %s: %s", session_id, exc)

    @staticmethod
    def _coerce_context_window(value: Any) -> int | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            coerced = int(value)
        except (TypeError, ValueError):
            return None
        return coerced if coerced > 0 else None

    @classmethod
    def _message_context_window(cls, message: ParsedMessage) -> int | None:
        return context_window_from_raw_message(message.raw_json)

    @staticmethod
    def _usage_has_tokens(message: ParsedMessage) -> bool:
        usage = message.usage
        if usage is None:
            return False
        fields = (
            "input_tokens",
            "output_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
        )
        if not all(
            isinstance(value := getattr(usage, field, 0), int) and not isinstance(value, bool)
            for field in fields
        ):
            return False
        return any(getattr(usage, field, 0) != 0 for field in fields)

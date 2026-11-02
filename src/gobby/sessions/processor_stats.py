"""Stat and render helpers for session message processing."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from collections.abc import Sequence
from typing import Any

from gobby.sessions.context_usage import context_window_from_raw_message
from gobby.sessions.message_stats import (
    MessageStats,
    StatsRecord,
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
        if complete and rendered.get("role") == "assistant" and self.communications_manager:
            texts = [
                block["content"]
                for block in rendered.get("content_blocks", [])
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("content"), str)
                and block["content"].strip()
            ]
            message_id = rendered.get("id")
            eligible = self._mirror_eligible_ids.get(session_id, set())
            if (
                texts
                and isinstance(message_id, str)
                and message_id
                and (session_id not in self._mirror_start_offsets or message_id in eligible)
            ):
                try:
                    await self._mirror_text_blocks(session_id, message_id, texts)
                except Exception:
                    # The durable delivery ledger retries independently. A
                    # failed mirror must not suppress stats or websocket events.
                    logger.warning("Transcript mirror failed for %s", message_id, exc_info=True)
                finally:
                    eligible.discard(message_id)
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

    async def _mirror_text_blocks(
        self: ProcessorHost,
        session_id: str,
        message_id: str,
        texts: list[str],
    ) -> None:
        """Mirror each text block of a completed turn in order, one outbound row each.

        The final block keeps the turn's message id, so its ledger row marks the
        whole turn as delivered. A single-block turn keeps its id unchanged. The
        first failed block stops the turn: every later block is held in the ledger
        and the retry loop sends it only after the block before it is delivered.
        The failure is re-raised after the hold.
        """
        manager = self.communications_manager
        if manager is None:
            return
        last = len(texts) - 1
        keys = [
            message_id if index == last else f"{message_id}#{index}" for index in range(last + 1)
        ]
        for index, text in enumerate(texts):
            order = {"transcript_turn_id": message_id, "transcript_block_index": index}
            try:
                await manager.mirror_session_message(session_id, keys[index], text, order)
            except Exception:
                for later in range(index + 1, last + 1):
                    try:
                        await manager.hold_transcript_message(
                            session_id,
                            keys[later],
                            texts[later],
                            {"transcript_turn_id": message_id, "transcript_block_index": later},
                        )
                    except Exception:
                        logger.warning(
                            "Holding transcript block %s failed", keys[later], exc_info=True
                        )
                raise

    def _accumulate_stats(
        self: ProcessorHost, session_id: str, records: Sequence[StatsRecord]
    ) -> MessageStats:
        """Accumulate incremental stats from parsed transcript records.

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
        stats = accumulate_message_stats(
            stats,
            records,
            source=self._session_sources.get(session_id),
        )
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
        # Persistence writes only the pending journal delta after a full build.
        # Keep its bounded source checks and fsync off the event loop.
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

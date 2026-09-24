"""Transcript processing for expired sessions.

This module owns transcript parsing, token-event reconstruction, artifact generation,
and transcript archiving. Lifecycle scheduling and maintenance remain in lifecycle.py.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from io import StringIO
from typing import Any

import psycopg

from gobby.app_context import get_app_context
from gobby.config.app import DaemonConfig
from gobby.config.sessions import SessionSummaryConfig
from gobby.llm.context_windows import ReconciledModelContext, reconcile_model_context
from gobby.sessions.context_usage import (
    context_window_from_raw_message,
    grok_epoch_max_occupancy,
    snapshot_from_token_usage,
    snapshot_from_window_metadata,
)
from gobby.sessions.message_stats import compute_message_stats
from gobby.sessions.summary_validity import is_summary_markdown_valid
from gobby.sessions.transcript_archive import backup_transcript
from gobby.sessions.transcript_index import rebuild_and_persist_index
from gobby.sessions.transcript_normalization import normalize_transcript_records
from gobby.sessions.transcript_reader import TranscriptReader
from gobby.sessions.transcripts import get_parser
from gobby.sessions.transcripts.base import ParsedMessage
from gobby.storage.context_usage_snapshot import ContextUsageSnapshot
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.token_events import (
    TokenEvent,
    TokenEventStore,
    build_token_event_payload,
    canonicalize_event_timestamp,
)

logger = logging.getLogger("gobby.sessions.lifecycle")

_TRANSCRIPT_INDEX_ERRORS = (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError)
_WINDOW_ONLY_CONTEXT_SOURCES = frozenset({"droid", "agy", "grok"})


def _session_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return 0


def _coerce_context_window(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return None
    return coerced if coerced > 0 else None


def _message_context_window(message: ParsedMessage) -> int | None:
    return context_window_from_raw_message(getattr(message, "raw_json", None))


def _session_artifacts_complete(session: Any) -> bool:
    return is_summary_markdown_valid(session.summary_markdown)


@dataclass(slots=True)
class _PendingTokenEvent:
    """A parsed usage event awaiting its batched off-loop insert.

    Carries everything the post-insert pass needs so that running totals,
    the context snapshot, and the websocket broadcast replay the exact
    per-event semantics the sequential ``record`` loop had.
    """

    event: TokenEvent
    snapshot: ContextUsageSnapshot | None
    payload: dict[str, Any]


def _deterministic_summary_failure(error: str) -> str | None:
    """Only canonical source/validation failures consume the retry budget."""
    if error == "Transcript file not found":
        return "missing_source"
    if error.startswith("Unsupported transcript source:"):
        return "unsupported_source"
    if error.startswith("Corrupt transcript record"):
        return "corrupt_source"
    if (
        error.startswith("Generated session summary was invalid:")
        or error == "Unable to generate a valid session summary"
    ):
        return "invalid_summary"
    return None


class TranscriptProcessingMixin:
    """Transcript-processing behavior shared by the session lifecycle manager."""

    db: HubDatabase
    session_manager: SessionManager
    token_event_store: TokenEventStore

    @property
    def memory_manager(self) -> Any | None:
        """Provided by the host; the lifecycle manager resolves it per use."""
        raise NotImplementedError

    @property
    def llm_service(self) -> Any | None:
        """Provided by the host; the lifecycle manager resolves it per use."""
        raise NotImplementedError

    _transcript_processing_cursor: tuple[datetime, str] | None = None

    async def _process_pending_transcripts(self, active: DaemonConfig) -> int:
        """Attempt one bounded page, rotating past every failure before wrapping."""
        config = active.session_lifecycle
        limit = config.transcript_processing_batch_size
        sessions = await asyncio.to_thread(
            self.session_manager.get_pending_transcript_sessions,
            limit=limit,
            after=self._transcript_processing_cursor,
        )
        if not sessions and self._transcript_processing_cursor is not None:
            self._transcript_processing_cursor = None
            sessions = await asyncio.to_thread(
                self.session_manager.get_pending_transcript_sessions,
                limit=limit,
                after=None,
            )
        processed = 0
        for session in sessions:
            try:
                # Stats reconstruction is independent from archival summary generation.
                try:
                    await self._process_session_transcript(session.id, session.transcript_path)
                except Exception:
                    logger.warning("Transcript stats failed for %s", session.id, exc_info=True)

                current = await asyncio.to_thread(self.session_manager.get, session.id)
                if current is None or current.status != "expired":
                    continue
                skip_llm = (
                    (getattr(current, "agent_depth", 0) or 0) > 0
                    or current.source in ("pipeline", "cron")
                    or _session_int(getattr(current, "turn_count", 0)) < 3
                )
                result = await self._generate_artifacts_if_needed(
                    session.id,
                    active.session_summary,
                    allow_llm=not skip_llm,
                    archive_dir=config.transcript_archive_dir,
                )
                if result.get("success"):
                    marked = await asyncio.to_thread(
                        self.session_manager.mark_transcript_processed,
                        session.id,
                        expected_session=current,
                        source_hash=result.get("source_context_hash"),
                    )
                    processed += int(marked is not None)
                else:
                    error = str(result.get("generation_error") or result.get("error") or "")
                    code = _deterministic_summary_failure(error)
                    if code:
                        await asyncio.to_thread(
                            self.session_manager.record_transcript_processing_failure,
                            session.id,
                            error_code=code,
                            error=error,
                            expected_session=current,
                        )
                    logger.info("Deferring transcript processing for %s: %s", session.id, error)

                if (
                    session.transcript_path
                    and session.external_id
                    and os.path.isfile(session.transcript_path)
                ):
                    await asyncio.to_thread(
                        backup_transcript,
                        session.external_id,
                        session.transcript_path,
                        config.transcript_archive_dir,
                    )
            except Exception:
                # Database, filesystem permissions, and provider failures are transient.
                logger.warning("Transcript retry failed for %s", session.id, exc_info=True)
            finally:
                self._transcript_processing_cursor = (session.created_at, session.id)
        return processed

    async def _generate_artifacts_if_needed(
        self,
        session_id: str,
        session_summary_config: SessionSummaryConfig,
        *,
        allow_llm: bool = True,
        archive_dir: str | None = None,
    ) -> dict[str, Any]:
        """Use the canonical pipeline, including provenance checks and archive fallback."""
        from gobby.sessions.summarize import _generate_session_summary_core

        result = await _generate_session_summary_core(
            session_id,
            self.session_manager,
            self.llm_service if allow_llm else None,
            session_summary_config,
            self.db,
            None,
            transcript_reader=TranscriptReader(self.session_manager, archive_dir=archive_dir),
        )
        outcome = result.result
        if (
            not allow_llm
            and not outcome.get("success")
            and outcome.get("generation_error")
            == "Session summary LLM feature config not available"
        ):
            # No-provider fallback is intentional for short/non-human sessions.
            # Its invalid output is deterministic, unlike an unavailable provider.
            outcome = dict(outcome)
            outcome.pop("generation_error", None)
        return outcome

    async def _process_session_transcript(
        self, session_id: str, transcript_path: str | None
    ) -> None:
        """
        Process a full transcript for a session.

        Reads the entire transcript and stores messages.
        Aggregates token usage.
        Uses idempotent upsert so re-processing is safe.

        The read, the parse, and every database call run in one worker-thread
        hop: an expired session's transcript can be hundreds of megabytes, and
        parsing it on the loop thread stalled hooks, HTTP, and terminal input
        for minutes (#22811). Only the websocket broadcasts run on the loop.

        Args:
            session_id: Session ID
            transcript_path: Path to transcript JSONL file
        """
        payloads = await asyncio.to_thread(
            self._persist_session_transcript, session_id, transcript_path
        )
        app_ctx = get_app_context()
        ws_server = app_ctx.websocket_server if app_ctx is not None else None
        if ws_server is None:
            return
        for payload in payloads:
            try:
                await ws_server.broadcast_token_event(payload)
            except Exception:
                logger.exception(
                    "Failed to broadcast transcript token event for session %s",
                    session_id,
                )

    def _persist_session_transcript(
        self, session_id: str, transcript_path: str | None
    ) -> list[dict[str, Any]]:
        """Blocking body of _process_session_transcript; returns broadcast payloads."""
        if not transcript_path or not os.path.exists(transcript_path):
            # The canonical summary reader can still use an archive or delivered handoff.
            logger.info("Transcript not found for session %s: %s", session_id, transcript_path)
            return []

        # Read entire file
        try:
            with open(transcript_path, encoding="utf-8") as f:
                raw = f.read()
        except Exception as e:
            logger.error("Error reading transcript %s: %s", transcript_path, e)
            raise

        if not raw.strip():
            return []

        # Parse all lines
        session = self.session_manager.get(session_id)
        if not session:
            return []

        parser = get_parser(
            session.source,
            session_id=session_id,
            transcript_path=getattr(session, "transcript_path", None),
        )

        # parse_lines may yield a mix of ParsedMessage and ParsedToolEvent
        # records; this token-event path only consumes ParsedMessage fields
        # (model, usage, message_id). Qwen's .json transcripts use the same
        # line-oriented envelope contract as the other supported CLIs.
        # JSON strings may contain Unicode line separators; only physical LF ends a record.
        parsed_records = parser.parse_lines(list(StringIO(raw)), start_index=0)
        normalized = normalize_transcript_records(parsed_records, session.source)
        messages = [r for r in normalized if isinstance(r, ParsedMessage)]
        session_source = session.source if isinstance(session.source, str) else None
        stats_records = normalized if session_source == "agy" else messages

        if not stats_records:
            return []

        # Persist session stats from the full transcript before any token-usage
        # early return, so sessions the live processor never tailed before expiry
        # still record real message/turn/tool counts instead of phantom zeros.
        # Same predicate as the live path via compute_message_stats.
        try:
            stats = compute_message_stats(
                stats_records,
                source=session_source,
            )
            self.session_manager.update_stats(
                session_id,
                message_count=stats["message_count"],
                turn_count=stats["turn_count"],
                tool_call_count=stats["tool_call_count"],
                last_assistant_content=stats["last_assistant_content"],
            )
        except (ValueError, KeyError, TypeError):
            logger.warning(
                "Failed to persist transcript stats from parsed message data",
                extra={"session_id": session_id},
                exc_info=True,
            )
        except psycopg.Error:
            logger.warning(
                "Database error persisting transcript stats",
                extra={"session_id": session_id},
                exc_info=True,
            )

        # Index sidecars are a seek optimization; transcript token processing must continue.
        try:
            st = os.stat(transcript_path)
            rebuild_and_persist_index(
                transcript_path,
                session_source or "claude",
                session_id,
                mtime_ns=st.st_mtime_ns,
                size=st.st_size,
            )
        except _TRANSCRIPT_INDEX_ERRORS:
            logger.warning(
                "Failed to finalize transcript index for session %s at %s",
                session_id,
                transcript_path,
                exc_info=True,
            )

        if not messages:
            return []

        # Replace any synthetic migration rows with real transcript events as soon as
        # we have a parseable transcript for this session.
        self.token_event_store.delete_session_events(session_id, origin="backfill")
        self.token_event_store.delete_session_events(session_id, origin="transcript")
        running_totals = self.token_event_store.get_session_totals(session_id)

        session_project_id = session.project_id if isinstance(session.project_id, str) else None
        session_source = session_source or "unknown"
        session_context_window = _coerce_context_window(session.context_window)
        session_model = session.model if isinstance(session.model, str) and session.model else None
        last_model: str | None = session_model
        saw_usage = False
        latest_context_snapshot: ContextUsageSnapshot | None = None
        payloads: list[dict[str, Any]] = []

        # Pass 1: fold every message into a message-ordered plan — either a
        # window-metadata snapshot entry or a pending token event. Each
        # reconcile_model_context call builds a resolver that reads aliases and
        # the model registry, so results are memoized per input: an 86 MB
        # transcript made 16,628 calls over 14 distinct inputs (#22811).
        snapshot_plan: list[_PendingTokenEvent | ContextUsageSnapshot | None] = []
        reconciled_by_input: dict[
            tuple[str | None, str | None, int | None], ReconciledModelContext
        ] = {}
        for msg in messages:
            message_model = msg.model if isinstance(msg.model, str) and msg.model else None
            observed_context_window = _message_context_window(msg)
            reconcile_input = (
                last_model,
                message_model,
                (
                    observed_context_window
                    if observed_context_window is not None
                    else session_context_window
                ),
            )
            reconciled_context = reconciled_by_input.get(reconcile_input)
            if reconciled_context is None:
                reconciled_context = reconcile_model_context(
                    *reconcile_input, provider=session_source, db=self.db
                )
                reconciled_by_input[reconcile_input] = reconciled_context
            last_model = reconciled_context.model
            message_context_window = reconciled_context.context_window
            if message_context_window is not None:
                session_context_window = message_context_window
            if session_source == "grok" and msg.context_used_tokens is not None:
                snapshot_plan.append(
                    ContextUsageSnapshot.from_reported_occupancy(
                        source="grok",
                        context_window=message_context_window,
                        context_used_tokens=msg.context_used_tokens,
                        model=message_model or last_model,
                        epoch_reset=msg.context_epoch_reset,
                    )
                )
            usage = msg.usage
            if usage is None:
                if session_source in _WINDOW_ONLY_CONTEXT_SOURCES:
                    snapshot_plan.append(
                        snapshot_from_window_metadata(
                            source=session_source,
                            context_window=message_context_window,
                            model=message_model or last_model,
                        )
                    )
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
                if session_source in _WINDOW_ONLY_CONTEXT_SOURCES:
                    snapshot_plan.append(
                        snapshot_from_window_metadata(
                            source=session_source,
                            context_window=message_context_window,
                            model=message_model or last_model,
                        )
                    )
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
            event_model = last_model

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
                context_window=message_context_window,
                event_at=canonicalize_event_timestamp(event_timestamp),
                metadata=metadata,
            )
            occupancy_snapshot = (
                None
                if session_source == "grok"
                else snapshot_from_token_usage(
                    source=session_source,
                    context_window=message_context_window,
                    usage=usage,
                    model=event_model,
                )
            )
            snapshot_plan.append(
                _PendingTokenEvent(
                    event=event,
                    snapshot=occupancy_snapshot,
                    payload={
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
                        "context_window": message_context_window,
                    },
                )
            )

        # Pass 2: one batched insert for every event (#20885). The returned
        # flags preserve the per-event dedup feedback.
        pending_events = [entry for entry in snapshot_plan if isinstance(entry, _PendingTokenEvent)]
        inserted_flags: list[bool] = []
        if pending_events:
            inserted_flags = self.token_event_store.record_batch(
                [entry.event for entry in pending_events]
            )

        # Pass 3: replay the sequential semantics in message order — a window
        # entry always overwrites the latest snapshot; an event entry updates
        # totals and the snapshot, and yields a broadcast payload carrying the
        # totals as of that event, only when its row actually inserted.
        event_position = 0
        for entry in snapshot_plan:
            if not isinstance(entry, _PendingTokenEvent):
                if entry is None:
                    continue
                if (
                    latest_context_snapshot is not None
                    and latest_context_snapshot.context_used_tokens is not None
                    and entry.context_used_tokens is None
                ):
                    continue
                if session_source == "grok":
                    latest_context_snapshot = grok_epoch_max_occupancy(
                        entry, current=latest_context_snapshot
                    )
                else:
                    latest_context_snapshot = entry
                continue
            inserted = inserted_flags[event_position]
            event_position += 1
            if not inserted:
                continue
            inserted_event = entry.event
            running_totals["input_tokens"] += inserted_event.input_tokens
            running_totals["output_tokens"] += inserted_event.output_tokens
            running_totals["cache_creation_tokens"] += inserted_event.cache_creation_tokens
            running_totals["cache_read_tokens"] += inserted_event.cache_read_tokens
            if entry.snapshot is not None:
                latest_context_snapshot = entry.snapshot
            payloads.append(build_token_event_payload(entry.payload, session_totals=running_totals))

        if not saw_usage and (
            _session_int(getattr(session, "usage_input_tokens", 0)) > 0
            or _session_int(getattr(session, "usage_output_tokens", 0)) > 0
            or _session_int(getattr(session, "usage_cache_creation_tokens", 0)) > 0
            or _session_int(getattr(session, "usage_cache_read_tokens", 0)) > 0
        ):
            if latest_context_snapshot is not None:
                self.session_manager.update_context_usage(session_id, latest_context_snapshot)
            logger.debug(
                "Transcript yielded no token events for %s; preserving existing session totals",
                session_id,
            )
            return payloads

        totals = self.token_event_store.get_session_totals(session_id)
        if saw_usage and not any(totals.values()) and any(running_totals.values()):
            totals = dict(running_totals)
        session_totals = totals

        # Update session with aggregated usage
        self.session_manager.update_usage(
            session_id=session_id,
            input_tokens=session_totals["input_tokens"],
            output_tokens=session_totals["output_tokens"],
            cache_creation_tokens=session_totals["cache_creation_tokens"],
            cache_read_tokens=session_totals["cache_read_tokens"],
            context_window=session_context_window,
            model=last_model,
        )
        if latest_context_snapshot is not None:
            self.session_manager.update_context_usage(session_id, latest_context_snapshot)

        # NOTE: Memory extraction and summary generation are now called from
        # _process_pending_transcripts (the caller), not here.  This ensures
        # they run even when the JSONL file has already been deleted and this
        # method returns early at the file-existence check.
        return payloads

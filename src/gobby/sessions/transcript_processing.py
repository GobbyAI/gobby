"""Transcript processing for expired sessions.

This module owns transcript parsing, token-event reconstruction, artifact generation,
and transcript archiving. Lifecycle scheduling and maintenance remain in lifecycle.py.
"""

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any, cast

import psycopg

from gobby.app_context import get_app_context
from gobby.config.app import DaemonConfig
from gobby.config.sessions import SessionSummaryConfig
from gobby.llm.context_windows import reconcile_model_context
from gobby.sessions.context_usage import (
    context_window_from_raw_message,
    snapshot_from_token_usage,
    snapshot_from_window_metadata,
)
from gobby.sessions.message_stats import MessageProtocol, compute_message_stats
from gobby.sessions.session_wiki_file import session_wiki_path_is_fresh
from gobby.sessions.summary_validity import is_summary_markdown_valid
from gobby.sessions.transcript_archive import backup_transcript
from gobby.sessions.transcript_index import rebuild_and_persist_index
from gobby.sessions.transcript_normalization import normalize_transcript_records
from gobby.sessions.transcripts.base import ParsedMessage
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.sessions.transcripts.droid import DroidTranscriptParser
from gobby.sessions.transcripts.grok import GrokTranscriptParser
from gobby.sessions.transcripts.qwen import QwenTranscriptParser
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


class TranscriptProcessingMixin:
    """Transcript-processing behavior shared by the session lifecycle manager."""

    db: HubDatabase
    session_manager: SessionManager
    token_event_store: TokenEventStore

    @property
    def llm_service(self) -> Any | None:
        """Provided by the host; the lifecycle manager resolves it per use."""
        raise NotImplementedError

    async def _process_pending_transcripts(self, active: DaemonConfig) -> int:
        """Process transcripts for expired sessions.

        Runs memory extraction and summary generation as separate steps
        OUTSIDE _process_session_transcript so they execute even when the
        JSONL file has already been deleted: a purged-but-digest-backed session
        still regenerates its summary (and its mirror wiki file) from the stored
        digest. For those recovery cases transcript_processed is only set once
        the summary is valid, allowing retry on the next cycle. The normal
        on-disk path is unchanged (processed once a summary exists, or when the
        LLM is unavailable).
        """
        config = active.session_lifecycle
        # Synchronous psycopg: a pool checkout runs its runtime-role check and
        # the query itself inline, and this loop is on the event loop thread.
        # The sampler caught this exact chain at 40% of a 2.44s stall (#20845).
        sessions = await asyncio.to_thread(
            self.session_manager.get_pending_transcript_sessions,
            limit=config.transcript_processing_batch_size,
        )

        if not sessions:
            return 0

        archive_dir = config.transcript_archive_dir

        processed = 0
        for session in sessions:
            agent_depth = getattr(session, "agent_depth", 0) or 0
            source = getattr(session, "source", "") or ""

            digest = getattr(session, "digest_markdown", None)

            # Step 1: Process transcript (reads JSONL, stores messages, aggregates usage)
            try:
                await self._process_session_transcript(session.id, session.transcript_path)
            except Exception as e:
                logger.error("Failed to process transcript for %s: %s", session.id, e)

            skip_llm = agent_depth > 0 or source in ("pipeline", "cron")

            # If the transcript file is gone we can't (re)parse it, but a
            # digest-backed session can still synthesize its summary/wiki from
            # the stored digest. Only short-circuit when there's nothing left to
            # do (no usable digest, ephemeral/subagent, or LLM unavailable);
            # otherwise fall through to digest-backed artifact generation.
            transcript_missing = not session.transcript_path or not os.path.exists(
                session.transcript_path
            )
            has_usable_digest = bool(digest and digest.strip())
            if transcript_missing and (skip_llm or not self.llm_service or not has_usable_digest):
                self.session_manager.mark_transcript_processed(session.id)
                processed += 1
                logger.info(
                    "Marked session %s as processed (transcript file missing, no further processing possible)",
                    session.id,
                )
                continue
            if transcript_missing:
                logger.info(
                    "Transcript gone for %s; regenerating digest-backed artifacts (summary/wiki) from the stored digest",
                    session.id,
                )

            if not skip_llm:
                # Parsing persists authoritative transcript stats. Refresh before
                # deciding whether this was only a short Q&A; by 3+ turns there's
                # likely something worth remembering.
                refreshed_stats = self.session_manager.get(session.id)
                turn_count = _session_int(getattr(refreshed_stats, "turn_count", 0))
                skip_llm = turn_count < 3

            # Skip LLM-heavy steps for non-human sessions — subagents, pipelines,
            # and cron sessions are ephemeral and not worth the token cost.
            if skip_llm:
                self.session_manager.mark_transcript_processed(session.id)
                processed += 1
                logger.debug(
                    "Processed transcript for %s session %s (depth=%s, skipped summary)",
                    source,
                    session.id,
                    agent_depth,
                )
                continue

            # Step 2: Generate artifacts — summary and/or wiki (best-effort)
            try:
                await self._generate_artifacts_if_needed(session.id, active.session_summary)
            except Exception as e:
                logger.warning("Artifact generation failed for %s: %s", session.id, e)

            # Step 3: Decide whether the session is settled enough to mark processed.
            #   - LLM unavailable: nothing more we can do, finalize.
            #   - Transcript gone (digest-backed recovery): finalize once the
            #     summary is valid. The summary is the durable artifact (persisted
            #     to the hub); the flat wiki file is a best-effort mirror that
            #     _generate_artifacts_if_needed already (re)wrote in step 2, so a
            #     transient summary failure retries next cycle without looping on
            #     the free local file write.
            #   - Normal on-disk path: unchanged (gated on summary presence).
            refreshed = self.session_manager.get(session.id)
            if not self.llm_service:
                should_mark = bool(refreshed)
            else:
                should_mark = refreshed is not None and _session_artifacts_complete(refreshed)

            if should_mark:
                self.session_manager.mark_transcript_processed(session.id)
                processed += 1
                logger.debug("Processed transcript for session %s", session.id)
            else:
                logger.info(
                    "Deferring transcript_processed for %s — digest-backed artifacts not yet complete",
                    session.id,
                )

            # Step 4: Best-effort backup of the transcript archive
            # Skipped when the file is already gone — nothing to archive.
            if not transcript_missing and session.transcript_path and session.external_id:
                try:
                    archive_path = await asyncio.to_thread(
                        backup_transcript,
                        session.external_id,
                        session.transcript_path,
                        archive_dir,
                    )
                    if archive_path:
                        logger.debug(
                            "Archived transcript for session %s (archived to %s)",
                            session.id,
                            archive_path,
                        )
                    else:
                        logger.warning("Transcript backup returned None for %s", session.id)
                except Exception as e:
                    logger.warning("Transcript backup failed for %s: %s", session.id, e)

        if processed > 0:
            logger.debug("Processed %s session transcripts", processed)

        return processed

    async def _generate_artifacts_if_needed(
        self,
        session_id: str,
        session_summary_config: SessionSummaryConfig,
    ) -> None:
        """Generate the session summary (and its mirror wiki file) when missing.

        Safety net for ungraceful exits — if on_session_end or /clear never
        triggered generation, this catches it during background transcript
        processing. Proceeds when the summary is missing/invalid OR the flat
        wiki file is absent; the summary flow no-ops an already-valid summary
        and restores a missing flat wiki file, so only the missing artifact is
        produced.
        """
        if not self.llm_service:
            return

        session = self.session_manager.get(session_id)
        if not session:
            return

        if is_summary_markdown_valid(session.summary_markdown) and session_wiki_path_is_fresh(
            session
        ):
            return

        digest_markdown = getattr(session, "digest_markdown", None)
        has_digest = bool(digest_markdown and digest_markdown.strip())

        # Digest-backed sessions can regenerate without a readable transcript.
        if not has_digest and not session.transcript_path:
            return

        try:
            from gobby.sessions.summarize import generate_session_summaries

            await generate_session_summaries(
                session_id=session_id,
                session_manager=self.session_manager,
                llm_service=self.llm_service,
                session_summary_config=session_summary_config,
                db=self.db,
                set_handoff_ready=False,  # already expired, don't change status
            )
        except Exception as e:
            logger.warning("Artifact generation failed for session %s: %s", session_id, e)

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
            # Expected for purged or CLI-rotated transcripts; the caller
            # regenerates summaries from the stored digest in that case.
            logger.info("Transcript not found for session %s: %s", session_id, transcript_path)
            return

        # Read entire file
        try:
            with open(transcript_path, encoding="utf-8") as f:
                raw = f.read()
        except Exception as e:
            logger.error("Error reading transcript %s: %s", transcript_path, e)
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
        if session.source == "grok":
            parser = GrokTranscriptParser(session_id=session_id)
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

        # parse_lines may yield a mix of ParsedMessage and ParsedToolEvent
        # records; this token-event path only consumes ParsedMessage fields
        # (model, usage, message_id). Qwen's .json transcripts use the same
        # line-oriented envelope contract as the other supported CLIs.
        parsed_records = parser.parse_lines(raw.splitlines(keepends=True), start_index=0)
        normalized = normalize_transcript_records(parsed_records, session.source)
        messages = [r for r in normalized if isinstance(r, ParsedMessage)]

        if not messages:
            return

        # Persist session stats from the full transcript before any token-usage
        # early return, so sessions the live processor never tailed before expiry
        # still record real message/turn/tool counts instead of phantom zeros.
        # Same predicate as the live path via compute_message_stats.
        try:
            stats = compute_message_stats(cast("list[MessageProtocol]", messages))
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
            index_source = session.source if isinstance(session.source, str) else None
            await asyncio.to_thread(
                rebuild_and_persist_index,
                transcript_path,
                index_source or "claude",
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

        # Replace any synthetic migration rows with real transcript events as soon as
        # we have a parseable transcript for this session.
        self.token_event_store.delete_session_events(session_id, origin="backfill")
        self.token_event_store.delete_session_events(session_id, origin="transcript")

        session_project_id = session.project_id if isinstance(session.project_id, str) else None
        session_source = session.source if isinstance(session.source, str) else "unknown"
        session_context_window = _coerce_context_window(session.context_window)
        session_model = session.model if isinstance(session.model, str) and session.model else None
        last_model: str | None = session_model
        running_totals = self.token_event_store.get_session_totals(session_id)
        ws_server = None
        app_ctx = get_app_context()
        if app_ctx is not None:
            ws_server = app_ctx.websocket_server
        saw_usage = False
        latest_context_snapshot = None

        for msg in messages:
            message_model = msg.model if isinstance(msg.model, str) and msg.model else None
            observed_context_window = _message_context_window(msg)
            reconciled_context = reconcile_model_context(
                last_model,
                message_model,
                (
                    observed_context_window
                    if observed_context_window is not None
                    else session_context_window
                ),
                provider=session_source,
            )
            last_model = reconciled_context.model
            message_context_window = reconciled_context.context_window
            if message_context_window is not None:
                session_context_window = message_context_window
            usage = msg.usage
            if usage is None:
                if session_source in _WINDOW_ONLY_CONTEXT_SOURCES:
                    latest_context_snapshot = snapshot_from_window_metadata(
                        source=session_source,
                        context_window=message_context_window,
                        model=message_model or last_model,
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
                    latest_context_snapshot = snapshot_from_window_metadata(
                        source=session_source,
                        context_window=message_context_window,
                        model=message_model or last_model,
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
            inserted = self.token_event_store.record(event)
            if inserted:
                running_totals["input_tokens"] += usage.input_tokens
                running_totals["output_tokens"] += usage.output_tokens
                running_totals["cache_creation_tokens"] += usage.cache_creation_tokens
                running_totals["cache_read_tokens"] += usage.cache_read_tokens
                latest_context_snapshot = snapshot_from_token_usage(
                    source=session_source,
                    context_window=message_context_window,
                    usage=usage,
                    model=event_model,
                )

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
                                    "context_window": message_context_window,
                                },
                                session_totals=running_totals,
                            )
                        )
                    except Exception:
                        logger.exception(
                            "Failed to broadcast transcript token event for session %s",
                            session_id,
                        )

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
            return

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

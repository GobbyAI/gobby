"""Token usage persistence for session message processing."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

import psycopg

from gobby.llm.context_windows import reconcile_model_context, reconcile_observed_model
from gobby.sessions.context_usage import grok_epoch_max_occupancy, normalize_context_usage_source
from gobby.sessions.message_stats import TURN_BOUNDARY_CONTENT_TYPE
from gobby.sessions.processor_types import WINDOW_ONLY_CONTEXT_SOURCES, ProcessorHost
from gobby.sessions.reasoning_effort import observed_reasoning_effort
from gobby.sessions.transcripts import get_parser
from gobby.sessions.transcripts.base import ParsedMessage
from gobby.storage.context_usage_snapshot import ContextUsageSnapshot
from gobby.storage.token_events import (
    TokenEvent,
    build_session_usage_payload,
    build_token_event_payload,
    canonicalize_event_timestamp,
)

logger = logging.getLogger(__name__)

# Enough transcript tail to hold the latest assistant turn's usage record.
TAIL_OCCUPANCY_BYTES = 512 * 1024

# These transcripts carry no per-line occupancy, so no tail window can yield one.
_NO_LINE_OCCUPANCY_SOURCES = frozenset({"droid", "agy"})

_OCCUPANCY_PAYLOAD_KEYS = (
    "context_used_tokens",
    "context_usage_ratio",
    "context_usage_source",
    "context_usage_confidence",
    "last_prompt_input_tokens",
    "last_prompt_uncached_input_tokens",
    "last_prompt_cache_read_tokens",
    "last_prompt_cache_creation_tokens",
    "last_completion_output_tokens",
)


class ProcessorUsageMixin:
    async def _persist_usage_events(
        self: ProcessorHost,
        session_id: str,
        messages: list[ParsedMessage],
        *,
        publish_occupancy: bool = True,
    ) -> None:
        if not self.session_manager:
            return
        has_usage = any(self._usage_has_tokens(msg) for msg in messages)
        has_context_occupancy = any(msg.context_used_tokens is not None for msg in messages)
        has_window_metadata = any(self._message_context_window(msg) is not None for msg in messages)
        has_model = any(isinstance(msg.model, str) and bool(msg.model) for msg in messages)
        # Claude Code's hook payloads carry no effort; its transcript records do. A
        # catch-up batch short of EOF ends on a historical record, so it keeps the
        # stored effort the way it keeps the stored occupancy.
        observed_effort = (
            next(
                (
                    effort
                    for msg in reversed(messages)
                    if (effort := observed_reasoning_effort(msg.raw_json)) is not None
                ),
                None,
            )
            if publish_occupancy
            else None
        )
        if (
            not has_usage
            and not has_context_occupancy
            and not has_window_metadata
            and not has_model
            and observed_effort is None
        ):
            return

        try:
            session = await self._run_db(self.session_manager.get, session_id)
        except psycopg.Error:
            logger.debug("Failed to load session %s for token usage", session_id, exc_info=True)
            return
        if session is None:
            return
        if observed_effort is not None and observed_effort != getattr(
            session, "reasoning_effort", None
        ):
            await self._run_db(
                self.session_manager.update, session_id, reasoning_effort=observed_effort
            )

        store = self._new_token_event_store()
        project_id = getattr(session, "project_id", None)
        project_id = project_id if isinstance(project_id, str) else None
        source = getattr(session, "source", None)
        source = source if isinstance(source, str) and source else "unknown"
        context_window = self._coerce_context_window(getattr(session, "context_window", None))
        session_model = getattr(session, "model", None)
        session_model = session_model if isinstance(session_model, str) and session_model else None
        last_model = session_model
        if not has_usage and not has_context_occupancy and not has_window_metadata:
            for msg in messages:
                last_model = reconcile_observed_model(last_model, msg.model)
            if last_model is not None and last_model != session_model:
                await self._run_db(self.session_manager.update_model, session_id, last_model)
            return

        running_totals = await self._run_db(store.get_session_totals, session_id)
        latest_context_snapshot = None
        # Known occupancy stands until a newer measurement replaces it; a record that
        # carries none must not reset it to a window-only snapshot.
        occupancy_known = getattr(session, "context_usage_confidence", None) == "reported"
        epoch_stored = getattr(session, "context_used_tokens", None)
        latest_event_at: datetime | None = None
        saw_insert = False
        saw_token_usage = False

        for msg in messages:
            message_model = msg.model if isinstance(msg.model, str) and msg.model else None
            message_context_window = self._message_context_window(msg)
            reconciled_context = await self._run_db(
                reconcile_model_context,
                last_model,
                message_model,
                message_context_window if message_context_window is not None else context_window,
                provider=source,
                db=self.db,
            )
            last_model = reconciled_context.model
            event_context_window = reconciled_context.context_window
            if event_context_window is not None:
                context_window = event_context_window
            if msg.context_used_tokens is not None:
                normalized_source = normalize_context_usage_source(source)
                if normalized_source is not None:
                    occupancy_snapshot = ContextUsageSnapshot.from_reported_occupancy(
                        source=normalized_source,
                        context_window=event_context_window,
                        context_used_tokens=msg.context_used_tokens,
                        model=last_model,
                        epoch_reset=msg.context_epoch_reset,
                    )
                    if occupancy_snapshot.context_used_tokens is not None:
                        if source == "grok":
                            occupancy_snapshot = grok_epoch_max_occupancy(
                                occupancy_snapshot,
                                current=latest_context_snapshot,
                                stored_used_tokens=epoch_stored,
                                occupancy_known=occupancy_known,
                                epoch_reset=msg.context_epoch_reset,
                            )
                            if msg.context_epoch_reset:
                                epoch_stored = occupancy_snapshot.context_used_tokens
                        latest_context_snapshot = occupancy_snapshot
                        occupancy_known = True
            if not self._usage_has_tokens(msg) or msg.usage is None:
                if not occupancy_known and source in WINDOW_ONLY_CONTEXT_SOURCES:
                    latest_context_snapshot = self._snapshot_from_window_metadata(
                        source=source,
                        context_window=event_context_window,
                        model=last_model,
                    )
                continue

            saw_token_usage = True
            event_at = canonicalize_event_timestamp(
                msg.timestamp if isinstance(msg.timestamp, datetime) else datetime.now(UTC)
            )
            message_id = (
                msg.message_id if isinstance(msg.message_id, str) and msg.message_id else None
            )
            metadata = (
                {"content_type": msg.content_type} if isinstance(msg.content_type, str) else None
            )
            usage = msg.usage
            event = TokenEvent(
                session_id=session_id,
                project_id=project_id,
                message_id=message_id,
                source=source,
                origin="transcript",
                model=last_model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_creation_tokens=usage.cache_creation_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                context_window=event_context_window,
                event_at=event_at,
                metadata=metadata,
            )
            latest_event_at = event_at
            # Turn-boundary usage sums every model call in the turn: it is accounting,
            # never the current context size. Occupancy the message reports itself wins
            # over an estimate from its usage (Droid attaches a cumulative delta to both).
            # Droid usage always spans every call since the last read, so it never estimates.
            if (
                msg.content_type != TURN_BOUNDARY_CONTENT_TYPE
                and msg.context_used_tokens is None
                and source != "droid"
                and not (source == "grok" and occupancy_known)
            ):
                latest_context_snapshot = self._snapshot_from_token_usage(
                    source=source,
                    context_window=event_context_window,
                    usage=usage,
                    model=event.model,
                )
                occupancy_known = True
            if not await self._run_db(store.record, event):
                continue

            saw_insert = True
            running_totals["input_tokens"] += usage.input_tokens
            running_totals["output_tokens"] += usage.output_tokens
            running_totals["cache_creation_tokens"] += usage.cache_creation_tokens
            running_totals["cache_read_tokens"] += usage.cache_read_tokens
            if self.websocket_server is not None:
                await self.websocket_server.broadcast_token_event(
                    build_token_event_payload(
                        {
                            "session_id": session_id,
                            "project_id": project_id,
                            "message_id": message_id,
                            "source": source,
                            "origin": "transcript",
                            "event_at": event_at,
                            "model": event.model,
                            "model_family": event.normalized_model_family(),
                            "input_tokens": usage.input_tokens,
                            "output_tokens": usage.output_tokens,
                            "cache_creation_tokens": usage.cache_creation_tokens,
                            "cache_read_tokens": usage.cache_read_tokens,
                            "context_window": event_context_window,
                        },
                        session_totals=running_totals,
                    )
                )

        if not publish_occupancy:
            # A catch-up batch short of EOF ends on a historical message; keep the
            # stored (tail-published) occupancy instead of regressing it.
            latest_context_snapshot = None

        if not saw_insert:
            session_totals = running_totals
            if saw_token_usage:
                totals = await self._run_db(store.get_session_totals, session_id)
                if any(totals.values()) or not any(running_totals.values()):
                    session_totals = totals
                await self._run_db(
                    self.session_manager.update_usage,
                    session_id=session_id,
                    input_tokens=session_totals["input_tokens"],
                    output_tokens=session_totals["output_tokens"],
                    cache_creation_tokens=session_totals["cache_creation_tokens"],
                    cache_read_tokens=session_totals["cache_read_tokens"],
                    context_window=context_window,
                    model=last_model,
                )
            if latest_context_snapshot is not None:
                await self._run_db(
                    self.session_manager.update_context_usage,
                    session_id,
                    latest_context_snapshot,
                )
                if self.websocket_server is not None:
                    await self.websocket_server.broadcast_session_usage_updated(
                        build_session_usage_payload(
                            session_id=session_id,
                            project_id=project_id,
                            model=last_model,
                            context_window=latest_context_snapshot.context_window,
                            totals=session_totals,
                            updated_at=latest_event_at,
                            context_used_tokens=latest_context_snapshot.context_used_tokens,
                            context_usage_ratio=latest_context_snapshot.context_usage_ratio,
                            context_usage_source=latest_context_snapshot.source,
                            context_usage_confidence=latest_context_snapshot.confidence,
                            last_prompt_input_tokens=latest_context_snapshot.raw_prompt_footprint,
                            last_prompt_uncached_input_tokens=(
                                latest_context_snapshot.uncached_prompt_tokens
                            ),
                            last_prompt_cache_read_tokens=(
                                latest_context_snapshot.cache_read_tokens
                            ),
                            last_prompt_cache_creation_tokens=(
                                latest_context_snapshot.cache_creation_tokens
                            ),
                            last_completion_output_tokens=latest_context_snapshot.output_tokens,
                        )
                    )
            return

        totals = await self._run_db(store.get_session_totals, session_id)
        if not any(totals.values()) and any(running_totals.values()):
            totals = dict(running_totals)
        session_totals = totals
        await self._run_db(
            self.session_manager.update_usage,
            session_id=session_id,
            input_tokens=session_totals["input_tokens"],
            output_tokens=session_totals["output_tokens"],
            cache_creation_tokens=session_totals["cache_creation_tokens"],
            cache_read_tokens=session_totals["cache_read_tokens"],
            context_window=context_window,
            model=last_model,
        )
        if latest_context_snapshot is not None:
            await self._run_db(
                self.session_manager.update_context_usage,
                session_id,
                latest_context_snapshot,
            )
        if self.websocket_server is not None:
            payload = build_session_usage_payload(
                session_id=session_id,
                project_id=project_id,
                model=last_model,
                context_window=context_window,
                totals=session_totals,
                updated_at=latest_event_at,
            )
            snapshot = latest_context_snapshot
            if snapshot is not None:
                payload.update(
                    context_used_tokens=snapshot.context_used_tokens,
                    context_usage_ratio=snapshot.context_usage_ratio,
                    context_usage_source=snapshot.source,
                    context_usage_confidence=snapshot.confidence,
                    last_prompt_input_tokens=snapshot.raw_prompt_footprint,
                    last_prompt_uncached_input_tokens=snapshot.uncached_prompt_tokens,
                    last_prompt_cache_read_tokens=snapshot.cache_read_tokens,
                    last_prompt_cache_creation_tokens=snapshot.cache_creation_tokens,
                    last_completion_output_tokens=snapshot.output_tokens,
                )
            else:
                # No occupancy measured this batch (e.g. only a turn-boundary aggregate):
                # omit it so clients keep the last value; null would clear it.
                for key in _OCCUPANCY_PAYLOAD_KEYS:
                    del payload[key]
            await self.websocket_server.broadcast_session_usage_updated(payload)

    async def _publish_tail_occupancy(
        self: ProcessorHost, session_id: str, transcript_path: str
    ) -> None:
        """Publish the latest occupancy and reasoning effort found in the transcript tail.

        Runs while a catch-up has history left to ingest, so the context-pressure
        guard reads the live context size rather than a value from before the
        catch-up began. Token events and totals stay with the ordered history pass.
        """
        source = self._session_sources.get(session_id)
        normalized_source = normalize_context_usage_source(source)
        if self.session_manager is None or source is None or normalized_source is None:
            return
        if source in _NO_LINE_OCCUPANCY_SOURCES:
            return
        try:
            session = await self._run_db(self.session_manager.get, session_id)
        except psycopg.Error:
            logger.warning("Tail occupancy unavailable for session %s", session_id, exc_info=True)
            return
        if session is None:
            return
        session_window = self._coerce_context_window(getattr(session, "context_window", None))
        session_model = getattr(session, "model", None)
        # A single tool-result line can outgrow any fixed window, so widen the
        # read until it holds a usage record or covers the whole transcript.
        limit = TAIL_OCCUPANCY_BYTES
        while True:
            try:
                lines, whole_file = await asyncio.to_thread(
                    _read_complete_tail_lines, transcript_path, limit
                )
                # A fresh parser per window: each re-reads an overlapping superset.
                parser = get_parser(source, session_id=session_id, transcript_path=transcript_path)
                records = await asyncio.to_thread(parser.parse_lines, lines)
            except OSError:
                logger.warning(
                    "Tail occupancy unavailable for session %s", session_id, exc_info=True
                )
                return
            except ValueError:
                logger.warning(
                    "Tail occupancy parse failed for session %s", session_id, exc_info=True
                )
                return
            messages = [msg for msg in records if isinstance(msg, ParsedMessage)]
            snapshot: ContextUsageSnapshot | None = None
            for msg in reversed(messages):
                window = self._message_context_window(msg) or session_window
                model = msg.model if isinstance(msg.model, str) and msg.model else session_model
                if msg.context_used_tokens is not None:
                    snapshot = ContextUsageSnapshot.from_reported_occupancy(
                        source=normalized_source,
                        context_window=window,
                        context_used_tokens=msg.context_used_tokens,
                        model=model,
                        epoch_reset=msg.context_epoch_reset,
                    )
                elif (
                    msg.usage is not None
                    and self._usage_has_tokens(msg)
                    and msg.content_type != TURN_BOUNDARY_CONTENT_TYPE
                ):
                    snapshot = self._snapshot_from_token_usage(
                        source=source, context_window=window, usage=msg.usage, model=model
                    )
                if snapshot is not None and snapshot.context_used_tokens is not None:
                    break
                snapshot = None
            if snapshot is not None or whole_file:
                break
            limit *= 4
        # The catch-up's history passes keep the stored effort until EOF, so the
        # tail publishes the live one alongside the live occupancy.
        effort = next(
            (
                effort
                for msg in reversed(messages)
                if (effort := observed_reasoning_effort(msg.raw_json)) is not None
            ),
            None,
        )
        if effort is not None and effort != getattr(session, "reasoning_effort", None):
            await self._run_db(self.session_manager.update, session_id, reasoning_effort=effort)
        if snapshot is not None:
            await self._run_db(self.session_manager.update_context_usage, session_id, snapshot)


def _read_complete_tail_lines(path: str, limit: int) -> tuple[list[str], bool]:
    """Return the complete lines within the last ``limit`` bytes of ``path``.

    The flag reports whether the window reached the start of the file.
    """
    with open(path, "rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        start = max(0, size - limit)
        handle.seek(start)
        data = handle.read()
    # The final element is unterminated; the first began mid-line unless at byte 0.
    complete = data.split(b"\n")[:-1]
    if start > 0:
        complete = complete[1:]
    return [line.decode("utf-8", errors="replace") for line in complete], start == 0

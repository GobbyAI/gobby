"""Token usage persistence for session message processing."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import psycopg

from gobby.llm.context_windows import reconcile_model_context, reconcile_observed_model
from gobby.sessions.processor_types import WINDOW_ONLY_CONTEXT_SOURCES, ProcessorHost
from gobby.sessions.transcripts.base import ParsedMessage
from gobby.storage.token_events import (
    TokenEvent,
    build_session_usage_payload,
    build_token_event_payload,
    canonicalize_event_timestamp,
)

logger = logging.getLogger(__name__)


class ProcessorUsageMixin:
    async def _persist_usage_events(
        self: ProcessorHost,
        session_id: str,
        messages: list[ParsedMessage],
    ) -> None:
        if not self.session_manager:
            return
        has_usage = any(self._usage_has_tokens(msg) for msg in messages)
        has_window_metadata = any(self._message_context_window(msg) is not None for msg in messages)
        has_model = any(isinstance(msg.model, str) and bool(msg.model) for msg in messages)
        if not has_usage and not has_window_metadata and not has_model:
            return

        try:
            session = await self._run_db(self.session_manager.get, session_id)
        except psycopg.Error:
            logger.debug("Failed to load session %s for token usage", session_id, exc_info=True)
            return
        if session is None:
            return

        store = self._new_token_event_store()
        project_id = getattr(session, "project_id", None)
        project_id = project_id if isinstance(project_id, str) else None
        source = getattr(session, "source", None)
        source = source if isinstance(source, str) and source else "unknown"
        context_window = self._coerce_context_window(getattr(session, "context_window", None))
        session_model = getattr(session, "model", None)
        session_model = session_model if isinstance(session_model, str) and session_model else None
        last_model = session_model
        if not has_usage and not has_window_metadata:
            for msg in messages:
                last_model = reconcile_observed_model(last_model, msg.model)
            if last_model is not None and last_model != session_model:
                await self._run_db(self.session_manager.update_model, session_id, last_model)
            return

        running_totals = await self._run_db(store.get_session_totals, session_id)
        latest_context_snapshot = None
        latest_event_at: datetime | None = None
        saw_insert = False
        saw_token_usage = False

        for msg in messages:
            message_model = msg.model if isinstance(msg.model, str) and msg.model else None
            message_context_window = self._message_context_window(msg)
            reconciled_context = reconcile_model_context(
                last_model,
                message_model,
                message_context_window if message_context_window is not None else context_window,
                provider=source,
            )
            last_model = reconciled_context.model
            event_context_window = reconciled_context.context_window
            if event_context_window is not None:
                context_window = event_context_window
            if not self._usage_has_tokens(msg) or msg.usage is None:
                if source in WINDOW_ONLY_CONTEXT_SOURCES:
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
            latest_context_snapshot = self._snapshot_from_token_usage(
                source=source,
                context_window=event_context_window,
                usage=usage,
                model=event.model,
            )
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
            await self.websocket_server.broadcast_session_usage_updated(
                build_session_usage_payload(
                    session_id=session_id,
                    project_id=project_id,
                    model=last_model,
                    context_window=context_window,
                    totals=session_totals,
                    updated_at=latest_event_at,
                    context_used_tokens=(
                        latest_context_snapshot.context_used_tokens
                        if latest_context_snapshot is not None
                        else None
                    ),
                    context_usage_ratio=(
                        latest_context_snapshot.context_usage_ratio
                        if latest_context_snapshot is not None
                        else None
                    ),
                    context_usage_source=(
                        latest_context_snapshot.source
                        if latest_context_snapshot is not None
                        else None
                    ),
                    context_usage_confidence=(
                        latest_context_snapshot.confidence
                        if latest_context_snapshot is not None
                        else None
                    ),
                    last_prompt_input_tokens=(
                        latest_context_snapshot.raw_prompt_footprint
                        if latest_context_snapshot is not None
                        else None
                    ),
                    last_prompt_uncached_input_tokens=(
                        latest_context_snapshot.uncached_prompt_tokens
                        if latest_context_snapshot is not None
                        else None
                    ),
                    last_prompt_cache_read_tokens=(
                        latest_context_snapshot.cache_read_tokens
                        if latest_context_snapshot is not None
                        else None
                    ),
                    last_prompt_cache_creation_tokens=(
                        latest_context_snapshot.cache_creation_tokens
                        if latest_context_snapshot is not None
                        else None
                    ),
                    last_completion_output_tokens=(
                        latest_context_snapshot.output_tokens
                        if latest_context_snapshot is not None
                        else None
                    ),
                )
            )

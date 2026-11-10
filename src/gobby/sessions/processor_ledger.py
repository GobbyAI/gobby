"""Ledger writes for one processor pass: subagent usage (plan 2.2) and reported runs (plan 2.3)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

import psycopg

from gobby.sessions.reported_usage import DROID_SOURCE, collect_runs
from gobby.sessions.subagent_usage import SUBAGENT_SOURCE, read_subagent_events
from gobby.sessions.transcript_paths import find_supplemental_transcripts_on_disk
from gobby.storage.reported_usage import ReportedUsageStore

if TYPE_CHECKING:
    from gobby.sessions.processor_types import ProcessorHost

logger = logging.getLogger(__name__)


class ProcessorLedgerMixin:
    async def _persist_ledger_batch(
        self: ProcessorHost,
        session_id: str,
        transcript_path: str,
        source: str | None,
        *,
        lines: Sequence[str] = (),
        records: Sequence[object] = (),
    ) -> None:
        """Write the ledger rows this pass produces for the parent session.

        ``lines`` and ``records`` are the pass's new transcript lines and parsed records,
        empty on an idle pass. Runs on every consuming pass, so an idle parent still picks
        up subagent appends and a refreshed Droid sidecar. The reported runs raise on a
        database error, which keeps the pass's offset where it was; the subagent half
        logs and retries, since its cursors advance only after a successful insert.
        """
        if source is None or self.session_manager is None:
            return
        await self._persist_reported_runs(session_id, transcript_path, source, lines, records)
        if source == SUBAGENT_SOURCE:
            await self._persist_subagent_usage(session_id, transcript_path)

    async def _persist_reported_runs(
        self: ProcessorHost,
        session_id: str,
        transcript_path: str,
        source: str,
        lines: Sequence[str],
        records: Sequence[object],
    ) -> None:
        session_manager = self.session_manager
        if session_manager is None:
            return
        session_started_at = None
        if source == DROID_SOURCE:
            session = await self._run_db(session_manager.get, session_id)
            if session is None:
                return
            session_started_at = session.created_at
        runs = await asyncio.to_thread(
            collect_runs,
            source,
            lines=lines,
            messages=records,
            transcript_path=transcript_path,
            session_started_at=session_started_at,
        )
        if runs:
            await self._run_db(ReportedUsageStore(self.db).upsert_runs, session_id, runs)

    async def _persist_subagent_usage(
        self: ProcessorHost,
        session_id: str,
        transcript_path: str,
    ) -> None:
        """Insert new subagent usage events under the parent session."""
        session_manager = self.session_manager
        if session_manager is None:
            return
        # Most parents have no subagents: the directory check keeps idle polls off the database.
        if not self._subagent_cursors.get(session_id) and not await asyncio.to_thread(
            find_supplemental_transcripts_on_disk, SUBAGENT_SOURCE, transcript_path
        ):
            return
        try:
            session = await self._run_db(session_manager.get, session_id)
            if session is None:
                return
            project_id = getattr(session, "project_id", None)
            project_id = project_id if isinstance(project_id, str) else None
            events, cursors = await asyncio.to_thread(
                read_subagent_events,
                session_id,
                project_id,
                transcript_path,
                self._subagent_cursors.get(session_id, {}),
            )
            if events:
                store = self._new_token_event_store()
                await self._run_db(store.record_batch, events)
                # Refresh on every pass that ingests events, not only when rows insert: a
                # refresh that failed after the insert leaves the replay deduped to False,
                # and the totals still need repair. Occupancy and model belong to the
                # parent's own messages, so the refresh carries only the lifetime totals.
                totals = await self._run_db(store.get_session_totals, session_id)
                await self._run_db(
                    session_manager.update_usage,
                    session_id=session_id,
                    input_tokens=totals["input_tokens"],
                    output_tokens=totals["output_tokens"],
                    cache_creation_tokens=totals["cache_creation_tokens"],
                    cache_read_tokens=totals["cache_read_tokens"],
                    context_window=None,
                    model=None,
                )
        except (psycopg.Error, OSError, ValueError):
            logger.warning(
                "Failed to ingest subagent usage for %s; retrying next pass",
                session_id,
                exc_info=True,
            )
            return
        if cursors:
            self._subagent_cursors[session_id] = cursors
        else:
            self._subagent_cursors.pop(session_id, None)

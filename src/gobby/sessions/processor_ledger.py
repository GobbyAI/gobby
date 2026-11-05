"""Claude subagent usage in the live processor's parent ledger (plan 2.2)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import psycopg

from gobby.sessions.subagent_usage import SUBAGENT_SOURCE, read_subagent_events
from gobby.sessions.transcript_paths import find_supplemental_transcripts_on_disk

if TYPE_CHECKING:
    from gobby.sessions.processor_types import ProcessorHost

logger = logging.getLogger(__name__)


class ProcessorLedgerMixin:
    async def _persist_ledger_batch(
        self: ProcessorHost,
        session_id: str,
        transcript_path: str,
        source: str | None,
    ) -> None:
        """Insert new subagent usage events under the parent session.

        Runs on every consuming pass, so an idle parent still picks up subagent appends.
        Cursors advance only after the insert. A failed pass keeps the prior cursors, so
        the next pass rereads the same lines and their message ids dedupe the rows.
        """
        if source != SUBAGENT_SOURCE or self.session_manager is None:
            return
        # Most parents have no subagents: the directory check keeps idle polls off the database.
        if not self._subagent_cursors.get(session_id) and not await asyncio.to_thread(
            find_supplemental_transcripts_on_disk, SUBAGENT_SOURCE, transcript_path
        ):
            return
        try:
            session = await self._run_db(self.session_manager.get, session_id)
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
                    self.session_manager.update_usage,
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

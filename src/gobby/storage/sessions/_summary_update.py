"""Summary update helpers for session storage."""

from __future__ import annotations

from typing import Any

from gobby.storage.session_models import Session
from gobby.storage.sessions._summary_protocols import SummaryUpdateHost as _SummaryUpdateHost
from gobby.utils.datetime import utc_now

from ._update_sentinel import UNSET, UnsetType, is_set


class _SummaryUpdateMixin:
    def persist_summary_state(
        self: _SummaryUpdateHost,
        session_id: str,
        *,
        summary_markdown: str,
        generation_mode: str,
        source_context_hash: str | None = None,
        summary_path: str | None | UnsetType = UNSET,
    ) -> Session | None:
        """Persist the authoritative current summary fields atomically."""
        now = utc_now()

        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET summary_path = CASE WHEN %s THEN %s ELSE summary_path END,
                    summary_markdown = %s,
                    summary_source_context_hash = %s,
                    summary_generation_mode = %s,
                    summary_generated_at = %s,
                    transcript_processing_failure_count = 0,
                    transcript_processing_last_error_code = NULL,
                    transcript_processing_last_error = NULL,
                    transcript_processing_last_failed_at = NULL,
                    updated_at = %s
                WHERE id = %s
                """,
                (
                    is_set(summary_path),
                    summary_path if is_set(summary_path) else None,
                    summary_markdown,
                    source_context_hash,
                    generation_mode,
                    now,
                    now,
                    session_id,
                ),
            )

        updated = self.get(session_id)
        if updated is not None:
            self._notify_session_change("session_updated", session_id)
        return updated

    def update_summary(
        self: _SummaryUpdateHost,
        session_id: str,
        summary_path: str | None | UnsetType = UNSET,
        summary_markdown: str | None | UnsetType = UNSET,
    ) -> Session | None:
        """Update summary fields, preserving omissions and clearing explicit None values."""
        if is_set(summary_markdown) and summary_markdown is not None:
            return self.persist_summary_state(
                session_id,
                summary_markdown=summary_markdown,
                generation_mode="agent_authored",
                source_context_hash=None,
                summary_path=summary_path,
            )

        values: dict[str, Any] = {}
        if is_set(summary_path):
            values["summary_path"] = summary_path
        if is_set(summary_markdown):
            values.update(
                summary_markdown=summary_markdown,
                summary_source_context_hash=None,
                summary_generation_mode=None,
                summary_generated_at=None,
            )
        if not values:
            return self.get(session_id)

        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{column} = %s" for column in values)
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE sessions SET {assignments} WHERE id = %s",
                (*values.values(), session_id),
            )
        updated = self.get(session_id)
        if updated is not None:
            self._notify_session_change("session_updated", session_id)
        return updated

"""Transcript processing mixin for session storage."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from gobby.storage.session_models import Session
from gobby.utils.datetime import utc_now
from gobby.utils.machine_id import get_machine_id

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


class _ManagerState(Protocol):
    db: HubDatabase

    def get(self, session_id: str) -> Session | None: ...


class _TranscriptMixin:
    def get_pending_transcript_sessions(
        self: _ManagerState,
        limit: int = 10,
        *,
        after: tuple[datetime, str] | None = None,
    ) -> list[Session]:
        """Read one bounded, machine-local page in immutable creation order."""
        if limit <= 0:
            return []
        cursor_clause = "AND (created_at, id) > (%s, %s)" if after else ""
        params: tuple[object, ...] = (get_machine_id(),)
        if after:
            params += after
        rows = self.db.fetchall(
            f"""
            SELECT * FROM sessions
            WHERE status = 'expired'
            AND transcript_processed = FALSE
            AND transcript_processing_failure_count < 3
            AND machine_id = %s
            {cursor_clause}
            ORDER BY created_at ASC, id ASC
            LIMIT %s
            """,
            (*params, limit),
        )
        return [Session.from_row(row) for row in rows]

    def mark_transcript_processed(
        self: _ManagerState,
        session_id: str,
        *,
        expected_session: Session | None = None,
        source_hash: str | None = None,
    ) -> Session | None:
        """
        Mark a session's transcript as fully processed.

        Args:
            session_id: Session ID

        Returns:
            Updated session or None if not found
        """
        guard, params = _source_guard(expected_session)
        if expected_session is not None:
            guard += " AND summary_source_context_hash IS NOT DISTINCT FROM %s"
            params += (source_hash,)
        with self.db.transaction() as conn:
            row = conn.execute(
                f"""UPDATE sessions SET transcript_processed = TRUE,
                    transcript_processing_failure_count = 0,
                    transcript_processing_last_error_code = NULL,
                    transcript_processing_last_error = NULL,
                    transcript_processing_last_failed_at = NULL,
                    updated_at = %s
                WHERE id = %s AND status = 'expired' {guard}
                RETURNING id""",
                (utc_now(), session_id, *params),
            ).fetchone()
        return self.get(session_id) if row is not None else None

    def reset_transcript_processed(self: _ManagerState, session_id: str) -> Session | None:
        """
        Reset transcript_processed flag when a session is resumed.

        Args:
            session_id: Session ID

        Returns:
            Updated session or None if not found
        """
        now = utc_now()
        with self.db.transaction():
            self.db.execute(
                """UPDATE sessions SET transcript_processed = FALSE,
                    transcript_processing_failure_count = 0,
                    transcript_processing_last_error_code = NULL,
                    transcript_processing_last_error = NULL,
                    transcript_processing_last_failed_at = NULL,
                    updated_at = %s WHERE id = %s""",
                (now, session_id),
            )
        return self.get(session_id)

    def reset_transcript_processing_failures(
        self: _ManagerState, session_id: str, *, expected_session: Session | None = None
    ) -> None:
        """Clear quarantine after a successful explicit generation or hash-matched reuse."""
        guard, params = _source_guard(expected_session)
        with self.db.transaction() as conn:
            conn.execute(
                f"""UPDATE sessions SET
                    transcript_processing_failure_count = 0,
                    transcript_processing_last_error_code = NULL,
                    transcript_processing_last_error = NULL,
                    transcript_processing_last_failed_at = NULL
                WHERE id = %s AND transcript_processing_failure_count > 0 {guard}""",
                (session_id, *params),
            )

    def record_transcript_processing_failure(
        self: _ManagerState,
        session_id: str,
        *,
        error_code: str,
        error: str,
        expected_session: Session | None = None,
    ) -> None:
        """Count only known deterministic failures; infrastructure errors never quarantine."""
        if error_code not in {
            "missing_source",
            "unsupported_source",
            "corrupt_source",
            "invalid_summary",
        }:
            raise ValueError(f"Not a deterministic transcript failure: {error_code}")
        guard, params = _source_guard(expected_session)
        with self.db.transaction() as conn:
            conn.execute(
                f"""UPDATE sessions SET
                    transcript_processing_failure_count = transcript_processing_failure_count + 1,
                    transcript_processing_last_error_code = %s,
                    transcript_processing_last_error = %s,
                    transcript_processing_last_failed_at = %s
                WHERE id = %s AND status = 'expired' AND transcript_processed = FALSE {guard}""",
                (error_code, error[:2000], utc_now(), session_id, *params),
            )


def _source_guard(session: Session | None) -> tuple[str, tuple[object, ...]]:
    if session is None:
        return "", ()
    return (
        " AND source = %s AND external_id = %s"
        " AND transcript_path IS NOT DISTINCT FROM %s"
        " AND last_activity IS NOT DISTINCT FROM %s",
        (session.source, session.external_id, session.transcript_path, session.last_activity),
    )

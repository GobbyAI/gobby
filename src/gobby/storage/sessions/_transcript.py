"""Transcript processing mixin for session storage."""

from __future__ import annotations

import builtins
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from gobby.storage.session_models import Session

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol


class _ManagerState(Protocol):
    db: DatabaseProtocol

    def get(self, session_id: str) -> Session | None: ...


class _TranscriptMixin:
    def get_pending_transcript_sessions(
        self: _ManagerState, limit: int = 10
    ) -> builtins.list[Session]:
        """
        Get sessions that need transcript processing.

        These are expired sessions with transcript_processed = FALSE.

        Args:
            limit: Maximum sessions to return

        Returns:
            List of sessions needing processing
        """
        rows = self.db.fetchall(
            """
            SELECT * FROM sessions
            WHERE status = 'expired'
            AND transcript_processed = FALSE
            AND transcript_path IS NOT NULL
            ORDER BY updated_at ASC
            LIMIT ?
            """,
            (limit,),
        )
        return [Session.from_row(row) for row in rows]

    def mark_transcript_processed(self: _ManagerState, session_id: str) -> Session | None:
        """
        Mark a session's transcript as fully processed.

        Args:
            session_id: Session ID

        Returns:
            Updated session or None if not found
        """
        now = datetime.now(UTC).isoformat()
        with self.db.transaction():
            self.db.execute(
                "UPDATE sessions SET transcript_processed = TRUE, updated_at = ? WHERE id = ?",
                (now, session_id),
            )
        return self.get(session_id)

    def reset_transcript_processed(self: _ManagerState, session_id: str) -> Session | None:
        """
        Reset transcript_processed flag when a session is resumed.

        Args:
            session_id: Session ID

        Returns:
            Updated session or None if not found
        """
        now = datetime.now(UTC).isoformat()
        with self.db.transaction():
            self.db.execute(
                "UPDATE sessions SET transcript_processed = FALSE, updated_at = ? WHERE id = ?",
                (now, session_id),
            )
        return self.get(session_id)

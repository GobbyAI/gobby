"""Identity and project-assignment operations for session storage."""

from __future__ import annotations

from typing import Protocol

from gobby.storage.hub.protocol import HubDatabase, SessionSeqMutation
from gobby.storage.session_models import Session
from gobby.storage.session_resolution import is_session_uuid
from gobby.utils.datetime import utc_now

from ._registration_cache import invalidate_session_caches
from ._web_chat_crud import _SessionWebChatCRUDMixin


class _SessionIdentityCRUDHost(Protocol):
    db: HubDatabase

    def get(self, session_id: str) -> Session | None: ...

    def _notify_session_change(self, event: str, session_id: str) -> None: ...


class _SessionIdentityCRUDMixin(_SessionWebChatCRUDMixin):
    def move_to_project(
        self: _SessionIdentityCRUDHost,
        session_id: str,
        project_id: str,
    ) -> Session | None:
        """Move a session and allocate its sequence number in the destination project."""
        with self.db.transaction_immediate(SessionSeqMutation(project_id=project_id)) as conn:
            existing = self.get(session_id)
            if existing is None:
                return None
            if existing.project_id == project_id:
                return existing

            max_seq_row = conn.execute(
                "SELECT MAX(seq_num) AS max_seq FROM sessions WHERE project_id = %s",
                (project_id,),
            ).fetchone()
            next_seq_num = ((max_seq_row["max_seq"] if max_seq_row else None) or 0) + 1
            conn.execute(
                """
                UPDATE sessions
                SET project_id = %s, seq_num = %s, updated_at = %s
                WHERE id = %s
                """,
                (project_id, next_seq_num, utc_now(), existing.id),
            )
            moved = self.get(existing.id)
            if moved is None:
                raise RuntimeError(f"Session {existing.id} not found after project move")
            return moved

    def get(self: _SessionIdentityCRUDHost, session_id: str) -> Session | None:
        """Get session by ID."""
        if not is_session_uuid(session_id):
            return None
        row = self.db.fetchone("SELECT * FROM sessions WHERE id = %s", (session_id,))
        return Session.from_row(row) if row else None

    def resolve_session_reference(
        self: _SessionIdentityCRUDHost, ref: str, project_id: str | None = None
    ) -> str:
        """Resolve a session reference to a UUID."""
        from gobby.storage.session_resolution import resolve_session_reference as _resolve

        return _resolve(self.db, ref, project_id)

    def touch(self: _SessionIdentityCRUDHost, session_id: str) -> None:
        """Refresh updated_at without changing any other fields."""
        now = utc_now()
        with self.db.transaction():
            self.db.execute(
                "UPDATE sessions SET updated_at = %s WHERE id = %s",
                (now, session_id),
            )

    def delete(self: _SessionIdentityCRUDHost, session_id: str) -> bool:
        """Delete session by ID."""
        with self.db.transaction():
            cursor = self.db.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            invalidate_session_caches(self, session_id)
            self._notify_session_change("session_deleted", session_id)
        return deleted

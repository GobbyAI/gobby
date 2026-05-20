"""Discovery/query mixin for session storage."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from gobby.storage.session_models import Session
from gobby.storage.sql_dialect import newer_than_now_expr

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol


class _ManagerState(Protocol):
    db: DatabaseProtocol


class _DiscoveryMixin:
    def find_by_external_id(
        self: _ManagerState,
        external_id: str,
        machine_id: str,
        project_id: str | None,
        source: str,
        session_type: str | None = None,
    ) -> Session | None:
        """
        Find session by external_id, machine_id, project_id, and source.

        This is the primary lookup for reconnecting to an existing session
        after daemon restart. The external_id (e.g., Claude Code's session ID)
        is stable within a session.

        Args:
            external_id: External session identifier
            machine_id: Machine identifier
            project_id: Project identifier
            source: CLI source (claude, gemini, qwen, codex, droid)
            session_type: Optional session type filter ('terminal' or 'web_chat')

        Returns:
            Session if found, None otherwise.
        """
        query = """
            SELECT * FROM sessions
            WHERE external_id = ?
              AND machine_id = ?
              AND ((project_id = ?) OR (project_id IS NULL AND ? IS NULL))
              AND source = ?
        """
        params: list[str | None] = [external_id, machine_id, project_id, project_id, source]
        if session_type is not None:
            query += " AND session_type = ?"
            params.append(session_type)
        row = self.db.fetchone(query, tuple(params))
        return Session.from_row(row) if row else None

    def find_active_by_external_id(
        self: _ManagerState,
        external_id: str,
        source: str,
    ) -> Session | None:
        """Find an active session by external_id and source (relaxed lookup).

        Unlike find_by_external_id, this does not require machine_id or project_id,
        making it suitable for the statusline handler which only knows the session_id.

        Args:
            external_id: External session identifier (e.g., Claude Code session ID)
            source: CLI source (claude, gemini, etc.)

        Returns:
            Most recently updated matching session, or None.
        """
        row = self.db.fetchone(
            """
            SELECT * FROM sessions
            WHERE external_id = ? AND source = ? AND status = 'active'
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (external_id, source),
        )
        return Session.from_row(row) if row else None

    def find_by_external_id_any_project(
        self: _ManagerState,
        external_id: str,
        machine_id: str,
        source: str,
        session_type: str | None = None,
    ) -> Session | None:
        """Find session by external_id, machine_id, source — ignoring project_id.

        Fallback lookup for daemon restart recovery when the caller may not
        know the correct project_id.  Returns the most recently updated match.

        Args:
            external_id: External session identifier
            machine_id: Machine identifier
            source: CLI source (claude, gemini, qwen, codex, droid)
            session_type: Optional session type filter ('terminal' or 'web_chat')

        Returns:
            Most recently updated matching session, or None.
        """
        query = """
            SELECT * FROM sessions
            WHERE external_id = ?
              AND machine_id = ?
              AND source = ?
        """
        params: list[str | None] = [external_id, machine_id, source]
        if session_type is not None:
            query += " AND session_type = ?"
            params.append(session_type)
        query += " ORDER BY updated_at DESC LIMIT 1"
        row = self.db.fetchone(query, tuple(params))
        return Session.from_row(row) if row else None

    def find_by_external_id_all_sources(
        self: _ManagerState,
        external_id: str,
        machine_id: str,
        project_id: str | None,
        session_type: str | None = None,
    ) -> list[Session]:
        """Find all sessions sharing an external_id across sources within one project."""
        query = """
            SELECT * FROM sessions
            WHERE external_id = ?
              AND machine_id = ?
              AND ((project_id = ?) OR (project_id IS NULL AND ? IS NULL))
        """
        params: list[str | None] = [external_id, machine_id, project_id, project_id]
        if session_type is not None:
            query += " AND session_type = ?"
            params.append(session_type)
        query += " ORDER BY created_at ASC, id ASC"

        rows = self.db.fetchall(query, tuple(params))
        return [Session.from_row(row) for row in rows]

    def find_parent(
        self: _ManagerState,
        machine_id: str,
        project_id: str,
        source: str | None = None,
        status: str = "handoff_ready",
        max_age_minutes: int = 10,
    ) -> Session | None:
        """
        Find most recent parent session with specific status.

        Args:
            machine_id: Machine identifier
            project_id: Project identifier
            source: Optional source identifier to filter by
            status: Status to filter by (default: handoff_ready)
            max_age_minutes: Only match sessions updated within this many minutes.
                Legitimate handoffs happen within seconds; stale sessions should
                not be matched. Default 10 minutes.

        Returns:
            Session object or None
        """
        updated_recent_sql = newer_than_now_expr(self.db, "updated_at", "?", "minute")
        query = (
            "SELECT * FROM sessions WHERE machine_id = ? AND status = ? AND project_id = ?"
            f" AND {updated_recent_sql}"
        )
        params: list[Any] = [machine_id, status, project_id, max_age_minutes]

        if source:
            query += " AND source = ?"
            params.append(source)

        query += " ORDER BY updated_at DESC LIMIT 1"

        row = self.db.fetchone(query, tuple(params))
        return Session.from_row(row) if row else None

    def find_children(self: _ManagerState, parent_session_id: str) -> list[Session]:
        """
        Find all child sessions of a parent.

        Args:
            parent_session_id: The parent session ID.

        Returns:
            List of child Session objects.
        """
        rows = self.db.fetchall(
            """
            SELECT * FROM sessions
            WHERE parent_session_id = ?
            ORDER BY created_at ASC
            """,
            (parent_session_id,),
        )
        return [Session.from_row(row) for row in rows]

    def is_ancestor(self: _ManagerState, ancestor_id: str, descendant_id: str) -> bool:
        """Check if ancestor_id is in the parent chain of descendant_id.

        Walks the parent_session_id chain from descendant upward.
        A session is NOT considered its own ancestor.

        Args:
            ancestor_id: Potential ancestor session ID
            descendant_id: Potential descendant session ID

        Returns:
            True if ancestor_id is found in the parent chain
        """
        current_id = descendant_id
        seen: set[str] = set()
        while True:
            row = self.db.fetchone(
                "SELECT parent_session_id FROM sessions WHERE id = ?",
                (current_id,),
            )
            if not row or row["parent_session_id"] is None:
                return False
            parent_id = row["parent_session_id"]
            if parent_id == ancestor_id:
                return True
            if parent_id in seen:
                return False
            seen.add(parent_id)
            current_id = parent_id

"""Parent candidate selection and lineage queries for session storage."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from gobby.storage.session_models import Session
from gobby.storage.sql_dialect import newer_than_now_expr

from ._discovery_helpers import handoff_candidate_matches, parse_terminal_context_value

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

MAX_HANDOFF_PARENT_CANDIDATES = 8


class _ManagerState(Protocol):
    db: HubDatabase


class _LineageDiscoveryMixin:
    def find_parent(
        self: _ManagerState,
        machine_id: str,
        project_id: str,
        source: str | None = None,
        status: str = "awaiting_handoff",
        max_age_minutes: int = 10,
        terminal_context: dict[str, Any] | str | None = None,
        candidate_limit: int = 1,
    ) -> Session | None:
        """
        Find most recent parent session with specific status.

        Args:
            machine_id: Machine identifier
            project_id: Project identifier
            source: Optional source identifier to filter by
            status: Status to filter by (default: awaiting_handoff)
            max_age_minutes: Only match sessions updated within this many minutes.
                Legitimate handoffs happen within seconds; stale sessions should
                not be matched. Default 10 minutes.
            terminal_context: Child terminal identity used to select the matching parent.
            candidate_limit: Number of newest candidates to scan, bounded at eight.

        Returns:
            Session object or None
        """
        updated_recent_sql = newer_than_now_expr(self.db, "updated_at", "%s", "minute")
        # newer_than_now_expr returns a trusted static fragment for the active SQL dialect.
        query = (
            "SELECT * FROM sessions WHERE machine_id = %s AND status = %s AND project_id = %s"  # nosec B608
            f" AND {updated_recent_sql}"
        )
        params: list[Any] = [machine_id, status, project_id, max_age_minutes]

        if source:
            query += " AND source = %s"
            params.append(source)

        bounded_limit = max(1, min(candidate_limit, MAX_HANDOFF_PARENT_CANDIDATES))
        query += " ORDER BY updated_at DESC LIMIT %s"
        params.append(bounded_limit)

        rows = self.db.fetchall(query, tuple(params))
        candidates = [Session.from_row(row) for row in rows]
        if not candidates:
            return None

        requested_context = parse_terminal_context_value(terminal_context)
        if not requested_context:
            return candidates[0] if len(candidates) == 1 else None

        return next(
            (
                candidate
                for candidate in candidates
                if handoff_candidate_matches(candidate, requested_context)
            ),
            None,
        )

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
            WHERE parent_session_id = %s
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
                "SELECT parent_session_id FROM sessions WHERE id = %s",
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

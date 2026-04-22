"""Query mixin for session storage."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from gobby.storage.session_models import Session

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol


class _ManagerState(Protocol):
    db: DatabaseProtocol


def _build_session_filters(
    project_id: str | None,
    status: str | None,
    source: str | None,
) -> tuple[list[str], list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []

    if status == "deleted":
        conditions.append("status = 'deleted'")
    else:
        conditions.append("status != 'deleted'")
        if status:
            conditions.append("status = ?")
            params.append(status)

    if project_id:
        conditions.append("project_id = ?")
        params.append(project_id)
    if source:
        conditions.append("source = ?")
        params.append(source)

    return conditions, params


class _QueryMixin:
    def list(
        self: _ManagerState,
        project_id: str | None = None,
        status: str | None = None,
        source: str | None = None,
        limit: int = 100,
        exclude_subagents: bool = False,
    ) -> list[Session]:
        """
        List sessions with optional filters.

        Args:
            project_id: Filter by project
            status: Filter by status
            source: Filter by CLI source
            limit: Maximum number of results
            exclude_subagents: If True, only return top-level sessions (agent_depth = 0)

        Returns:
            List of Session instances
        """
        conditions, params = _build_session_filters(project_id, status, source)

        if exclude_subagents:
            conditions.append(
                "(parent_session_id IS NULL OR parent_session_id = '') AND agent_depth = 0"
            )

        where_clause = " AND ".join(conditions)
        params.append(limit)

        rows = self.db.fetchall(
            f"""
            SELECT * FROM sessions
            WHERE {where_clause}
            ORDER BY updated_at DESC
            LIMIT ?
            """,  # nosec B608
            tuple(params),
        )
        return [Session.from_row(row) for row in rows]

    def count(
        self: _ManagerState,
        project_id: str | None = None,
        status: str | None = None,
        source: str | None = None,
    ) -> int:
        """
        Count sessions with optional filters.

        Args:
            project_id: Filter by project
            status: Filter by status
            source: Filter by CLI source

        Returns:
            Count of matching sessions
        """
        conditions, params = _build_session_filters(project_id, status, source)
        where_clause = " AND ".join(conditions)

        result = self.db.fetchone(
            f"SELECT COUNT(*) as count FROM sessions WHERE {where_clause}",  # nosec B608
            tuple(params),
        )
        return result["count"] if result else 0

    def count_by_status(self: _ManagerState) -> dict[str, int]:
        """
        Count sessions grouped by status.

        Returns:
            Dictionary mapping status to count
        """
        rows = self.db.fetchall("SELECT status, COUNT(*) as count FROM sessions GROUP BY status")
        return {row["status"]: row["count"] for row in rows}

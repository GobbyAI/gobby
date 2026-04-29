"""Query mixin for session storage."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol

from gobby.storage.session_models import Session

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol


class _ManagerState(Protocol):
    db: DatabaseProtocol


# Type alias defined outside the class so `list` resolves to the builtin
# rather than _QueryMixin.list (which shadows it inside the class body).
_TaskRefsByRole = dict[str, list[int]]


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
        cursor_updated_at: str | None = None,
        cursor_id: str | None = None,
    ) -> list[Session]:
        """
        List sessions with optional filters.

        Args:
            project_id: Filter by project
            status: Filter by status
            source: Filter by CLI source
            limit: Maximum number of results
            exclude_subagents: If True, only return top-level sessions (agent_depth = 0)
            cursor_updated_at: Compound-cursor timestamp from a prior page's last row.
                When set with cursor_id, returns rows strictly after (lower than) the
                cursor in the (updated_at, id) DESC ordering.
            cursor_id: Compound-cursor session id paired with cursor_updated_at.
                Both must be supplied together; supplying one without the other is ignored.

        Returns:
            List of Session instances
        """
        conditions, params = _build_session_filters(project_id, status, source)

        if exclude_subagents:
            conditions.append(
                "(parent_session_id IS NULL OR parent_session_id = '') AND agent_depth = 0"
            )

        if cursor_updated_at is not None and cursor_id is not None:
            conditions.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
            params.extend([cursor_updated_at, cursor_updated_at, cursor_id])

        where_clause = " AND ".join(conditions)
        params.append(limit)

        rows = self.db.fetchall(
            f"""
            SELECT * FROM sessions
            WHERE {where_clause}
            ORDER BY updated_at DESC, id DESC
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

    def fetch_task_refs_by_session(
        self: _ManagerState,
        session_ids: Sequence[str],
    ) -> dict[str, _TaskRefsByRole]:
        """Bulk-load task seq_nums per session, grouped by linkage role.

        Returns a mapping: ``{ session_id: { "claimed": [...], "created": [...],
        "closed": [...] } }``. Every input id appears in the result with empty
        lists when the session has no task refs in a given role — callers don't
        have to handle missing keys.

        One query per call regardless of how many sessions are passed in. Tasks
        with NULL seq_num (legacy rows) are skipped.
        """
        result: dict[str, _TaskRefsByRole] = {
            sid: {"claimed": [], "created": [], "closed": []} for sid in session_ids
        }
        if not session_ids:
            return result

        placeholders = ",".join(["?"] * len(session_ids))
        sql = f"""
            SELECT
                seq_num,
                claimed_by_session_id,
                created_in_session_id,
                closed_in_session_id
            FROM tasks
            WHERE seq_num IS NOT NULL
              AND (
                  claimed_by_session_id IN ({placeholders})
                  OR created_in_session_id IN ({placeholders})
                  OR closed_in_session_id IN ({placeholders})
              )
            ORDER BY seq_num
        """  # nosec B608
        rows = self.db.fetchall(sql, tuple(session_ids) * 3)

        for row in rows:
            seq_num = row["seq_num"]
            for role, col in (
                ("claimed", "claimed_by_session_id"),
                ("created", "created_in_session_id"),
                ("closed", "closed_in_session_id"),
            ):
                sid = row[col]
                if sid in result:
                    result[sid][role].append(seq_num)

        return result

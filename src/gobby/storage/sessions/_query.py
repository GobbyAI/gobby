"""Query mixin for session storage."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol

from gobby.storage.session_models import Session

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


class _ManagerState(Protocol):
    db: HubDatabase


# Type alias defined outside the class so `list` resolves to the builtin
# rather than _QueryMixin.list (which shadows it inside the class body).
_TaskRefsByRole = dict[str, list[int]]


_TASK_REF_ROLE_COLUMNS: dict[str, str] = {
    "claimed": "claimed_by_session_id",
    "created": "created_in_session_id",
    "closed": "closed_in_session_id",
}


def _build_session_filters(
    project_id: str | None,
    status: str | None,
    source: str | None,
    *,
    sources: Sequence[str] | None = None,
    statuses: Sequence[str] | None = None,
    modes: Sequence[str] | None = None,
    models: Sequence[str] | None = None,
    session_seq_min: int | None = None,
    session_seq_max: int | None = None,
    task_ref_min: int | None = None,
    task_ref_max: int | None = None,
    task_ref_roles: Sequence[str] | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
) -> tuple[list[str], list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []

    if status == "deleted":
        conditions.append("status = 'deleted'")
    else:
        conditions.append("status != 'deleted'")
        if status:
            conditions.append("status = %s")
            params.append(status)
        if statuses:
            placeholders = ",".join(["%s"] * len(statuses))
            conditions.append(f"status IN ({placeholders})")
            params.extend(statuses)

    if project_id:
        conditions.append("project_id = %s")
        params.append(project_id)
    if source:
        conditions.append("source = %s")
        params.append(source)
    if sources:
        placeholders = ",".join(["%s"] * len(sources))
        conditions.append(f"source IN ({placeholders})")
        params.extend(sources)

    # Mode resolves to agent_depth: interactive = depth 0, auto = depth >= 1.
    # Empty / both = no filter (the user wants everything).
    if modes:
        unique_modes = set(modes)
        if "interactive" in unique_modes and "auto" not in unique_modes:
            conditions.append("agent_depth = 0")
        elif "auto" in unique_modes and "interactive" not in unique_modes:
            conditions.append("agent_depth >= 1")
        # "both selected" or "neither selected" → no filter

    if models:
        placeholders = ",".join(["%s"] * len(models))
        conditions.append(f"model IN ({placeholders})")
        params.extend(models)

    if session_seq_min is not None:
        conditions.append("seq_num >= %s")
        params.append(session_seq_min)
    if session_seq_max is not None:
        conditions.append("seq_num <= %s")
        params.append(session_seq_max)

    # Task-ref overlap: a session matches if any task with seq_num in
    # [min, max] is linked to it via any of the selected roles. Default role
    # is "claimed" — the most useful axis ("which sessions worked on tasks
    # in range X").
    if task_ref_min is not None or task_ref_max is not None:
        roles = list(task_ref_roles) if task_ref_roles else ["claimed"]
        role_clauses: list[str] = []
        for role in roles:
            col = _TASK_REF_ROLE_COLUMNS.get(role)
            if col is None:
                continue
            bounds: list[str] = []
            if task_ref_min is not None:
                bounds.append("seq_num >= %s")
                params.append(task_ref_min)
            if task_ref_max is not None:
                bounds.append("seq_num <= %s")
                params.append(task_ref_max)
            bound_sql = " AND ".join(bounds)
            role_clauses.append(
                f"EXISTS (SELECT 1 FROM tasks WHERE {col} = sessions.id AND {bound_sql})"  # nosec B608
            )
        if role_clauses:
            conditions.append(f"({' OR '.join(role_clauses)})")

    if created_after:
        conditions.append("created_at >= %s")
        params.append(created_after)
    if created_before:
        conditions.append("created_at < %s")
        params.append(created_before)

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
        sources: Sequence[str] | None = None,
        statuses: Sequence[str] | None = None,
        modes: Sequence[str] | None = None,
        models: Sequence[str] | None = None,
        session_seq_min: int | None = None,
        session_seq_max: int | None = None,
        task_ref_min: int | None = None,
        task_ref_max: int | None = None,
        task_ref_roles: Sequence[str] | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
    ) -> list[Session]:
        """
        List sessions with optional filters.

        Args:
            project_id: Filter by project
            status: Filter by status
            source: Filter by CLI source (single value)
            limit: Maximum number of results
            exclude_subagents: If True, only return top-level sessions (agent_depth = 0)
            cursor_updated_at: Compound-cursor timestamp from a prior page's last row.
                When set with cursor_id, returns rows strictly after (lower than) the
                cursor in the (updated_at, id) DESC ordering.
            cursor_id: Compound-cursor session id paired with cursor_updated_at.
                Both must be supplied together; supplying one without the other is ignored.
            sources: Multi-value source filter (source IN ...). Combined with `source` via AND
                if both are supplied — most callers use one or the other.
            statuses: Multi-value status filter (status IN ...). Stacks on top of the
                exclude-deleted base predicate; the legacy `status` positional and
                `statuses` are independent (most callers use one or the other).
            modes: "interactive" / "auto" → agent_depth predicate. Empty/both = no filter.
            models: Multi-value model filter (model IN ...).
            session_seq_min / session_seq_max: Inclusive range on sessions.seq_num.
            task_ref_min / task_ref_max: Inclusive range matched against task.seq_num
                via task_ref_roles linkages (default role: claimed). A session matches
                when any linked task in any selected role has seq_num in the range.
            task_ref_roles: Subset of {"claimed", "created", "closed"}. Empty/None
                defaults to {"claimed"} when a range is set.
            created_after / created_before: ISO timestamp range on sessions.created_at.
                created_after is inclusive, created_before is exclusive.

        Returns:
            List of Session instances
        """
        conditions, params = _build_session_filters(
            project_id,
            status,
            source,
            sources=sources,
            statuses=statuses,
            modes=modes,
            models=models,
            session_seq_min=session_seq_min,
            session_seq_max=session_seq_max,
            task_ref_min=task_ref_min,
            task_ref_max=task_ref_max,
            task_ref_roles=task_ref_roles,
            created_after=created_after,
            created_before=created_before,
        )

        if exclude_subagents:
            conditions.append(
                "(parent_session_id IS NULL OR parent_session_id = '') AND agent_depth = 0"
            )

        if cursor_updated_at is not None and cursor_id is not None:
            conditions.append("(updated_at < %s OR (updated_at = %s AND id < %s))")
            params.extend([cursor_updated_at, cursor_updated_at, cursor_id])

        where_clause = " AND ".join(conditions)
        params.append(limit)

        rows = self.db.fetchall(
            f"""
            SELECT * FROM sessions
            WHERE {where_clause}
            ORDER BY updated_at DESC, id DESC
            LIMIT %s
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

        placeholders = ",".join(["%s"] * len(session_ids))
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

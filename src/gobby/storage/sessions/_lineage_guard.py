"""Parent-session lineage guards for session storage writes."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from ._constants import get_logger

_MAX_PARENT_CHAIN_DEPTH = 1000


class _ParentLookupConnection(Protocol):
    def execute(self, sql: str, params: Sequence[object] = ()) -> Any: ...


def sanitize_parent_session_id(
    conn: _ParentLookupConnection,
    *,
    child_session_id: str,
    parent_session_id: str | None,
    context: str,
) -> str | None:
    """Return a parent ID only when it cannot create a lineage cycle."""
    if parent_session_id is None:
        return None

    invalid_reason = _invalid_parent_chain_reason(
        conn,
        child_session_id=child_session_id,
        parent_session_id=parent_session_id,
    )
    if invalid_reason is None:
        return parent_session_id

    get_logger().warning(
        "Ignoring parent_session_id %s for session %s during %s: %s",
        parent_session_id,
        child_session_id,
        context,
        invalid_reason,
    )
    return None


def repair_self_parent_session(
    conn: _ParentLookupConnection,
    *,
    session_id: str,
    now: str,
) -> None:
    """Clear a corrupt self-parent row inside the caller's transaction."""
    cursor = conn.execute(
        """
        UPDATE sessions
           SET parent_session_id = NULL,
               updated_at = %s
         WHERE id = %s
           AND parent_session_id = id
        """,
        (now, session_id),
    )
    if _rowcount(cursor) > 0:
        get_logger().warning("Repaired self-parent session lineage for %s", session_id)


def _invalid_parent_chain_reason(
    conn: _ParentLookupConnection,
    *,
    child_session_id: str,
    parent_session_id: str,
) -> str | None:
    if parent_session_id == child_session_id:
        return "session cannot be its own parent"

    seen: set[str] = set()
    current_id: str | None = parent_session_id
    depth = 0
    while current_id:
        if current_id == child_session_id:
            return "parent chain would cycle back to the child session"
        if current_id in seen:
            return "parent chain is already cyclic"
        if depth >= _MAX_PARENT_CHAIN_DEPTH:
            return "parent chain exceeds maximum traversal depth"

        seen.add(current_id)
        depth += 1
        current_id = _fetch_parent_session_id(conn, current_id)

    return None


def _fetch_parent_session_id(conn: _ParentLookupConnection, session_id: str) -> str | None:
    cursor = conn.execute(
        "SELECT parent_session_id FROM sessions WHERE id = %s",
        (session_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    value = _row_value(row, "parent_session_id", 0)
    return str(value) if value is not None else None


def _row_value(row: Any, key: str, index: int) -> Any:
    keys = row.keys() if hasattr(row, "keys") else ()
    if key in keys:
        return row[key]
    return row[index]


def _rowcount(cursor: Any) -> int:
    rowcount = getattr(cursor, "rowcount", -1)
    if callable(rowcount):
        return int(rowcount())
    return int(rowcount)

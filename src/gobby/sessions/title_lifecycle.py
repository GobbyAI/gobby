"""Deterministic session-title transitions driven by task claims."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gobby.storage.sessions._title_defaults import (
    MANUAL_TITLE_SOURCE,
    PROVISIONAL_TITLE_SOURCE,
    TASK_TITLE_SOURCE,
    format_provisional_session_title,
    format_task_session_title,
)


@dataclass(frozen=True, slots=True)
class ClaimedTaskTitle:
    seq_num: int
    title: str


def latest_open_claimed_task(db: Any, session_id: str, *, conn: Any = None) -> ClaimedTaskTitle | None:
    """Return the latest still-open task currently claimed by a session."""
    executor = conn if conn is not None else db
    row = executor.execute(
        """
        SELECT seq_num, title
        FROM tasks
        WHERE claimed_by_session_id = %s
          AND closed_at IS NULL
        ORDER BY updated_at DESC, seq_num DESC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    if row is None or not isinstance(row["seq_num"], int):
        return None
    title = str(row["title"] or "").strip()
    return ClaimedTaskTitle(row["seq_num"], title) if title else None


def update_title_for_claim(session_manager: Any, session_id: str, task: Any) -> Any:
    """Apply the deterministic task title after a successful claim."""
    seq_num = getattr(task, "seq_num", None)
    title = getattr(task, "title", None)
    if not isinstance(seq_num, int) or not isinstance(title, str) or not title.strip():
        return session_manager.get(session_id)
    return session_manager.update_title(
        session_id,
        format_task_session_title(seq_num, title),
        title_source=TASK_TITLE_SOURCE,
    )


def recompute_automatic_title(session_manager: Any, session_id: str) -> Any:
    """Recompute a nonmanual title from the latest open claim or provisional rule."""
    session = session_manager.get(session_id)
    if session is None or getattr(session, "title_source", None) == MANUAL_TITLE_SOURCE:
        return session
    task = latest_open_claimed_task(session_manager.db, session_id)
    if task is not None:
        title = format_task_session_title(task.seq_num, task.title)
        source = TASK_TITLE_SOURCE
    else:
        seq_num = getattr(session, "seq_num", None)
        if not isinstance(seq_num, int):
            return session
        title = format_provisional_session_title(seq_num)
        source = PROVISIONAL_TITLE_SOURCE
    return session_manager.update_title(session_id, title, title_source=source)


def clear_successor_title(conn: Any, predecessor: Any, successor_seq_num: int) -> tuple[str, str]:
    """Choose the title persisted on a clear successor before claim transfer."""
    title = getattr(predecessor, "title", None)
    if getattr(predecessor, "title_source", None) == MANUAL_TITLE_SOURCE and isinstance(
        title, str
    ):
        return title, MANUAL_TITLE_SOURCE
    task = latest_open_claimed_task(None, predecessor.id, conn=conn)
    if task is not None:
        return format_task_session_title(task.seq_num, task.title), TASK_TITLE_SOURCE
    return format_provisional_session_title(successor_seq_num), PROVISIONAL_TITLE_SOURCE


def apply_clear_successor_title(
    session_manager: Any,
    successor_id: str,
    predecessor: Any,
) -> Any:
    """Inherit a manual title or recompute the successor's automatic title."""
    title = getattr(predecessor, "title", None)
    if getattr(predecessor, "title_source", None) == MANUAL_TITLE_SOURCE and isinstance(
        title, str
    ):
        return session_manager.update_title(
            successor_id,
            title,
            title_source=MANUAL_TITLE_SOURCE,
        )
    return recompute_automatic_title(session_manager, successor_id)

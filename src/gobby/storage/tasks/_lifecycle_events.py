"""Append-only task lifecycle event storage helpers."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from gobby.storage.database import DatabaseProtocol


@dataclass(frozen=True)
class TaskLifecycleEvent:
    id: int
    task_id: str
    from_state: str | None
    to_state: str
    reason: str
    by_actor: str
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> TaskLifecycleEvent:
        return cls(
            id=int(row["id"]),
            task_id=row["task_id"],
            from_state=row["from_state"],
            to_state=row["to_state"],
            reason=row["reason"],
            by_actor=row["by_actor"],
            created_at=row["created_at"],
        )


class TaskLifecycleEventManager:
    """Append-only lifecycle event audit storage."""

    def __init__(self, db: DatabaseProtocol):
        self.db = db

    def ensure_table(self) -> None:
        """Create the table for focused tests before the canonical migration lands."""
        with self.db.transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_lifecycle_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    by_actor TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_lifecycle_events_task
                    ON task_lifecycle_events (task_id, created_at)
                """
            )

    def record_lifecycle_event(
        self,
        task_id: str,
        from_state: str | None,
        to_state: str,
        reason: str,
        by_actor: str | None = None,
        *,
        by: str | None = None,
    ) -> TaskLifecycleEvent:
        actor = by_actor if by_actor is not None else by
        if not reason.strip():
            raise ValueError("reason is required")
        if actor is None or not actor.strip():
            raise ValueError("by_actor is required")

        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO task_lifecycle_events (
                    task_id, from_state, to_state, reason, by_actor
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, from_state, to_state, reason, actor),
            )
            event_id = cursor.lastrowid
            if event_id is None:
                raise RuntimeError("SQLite did not return a lifecycle event id")

        row = self.db.fetchone("SELECT * FROM task_lifecycle_events WHERE id = ?", (event_id,))
        if row is None:
            raise RuntimeError(f"Lifecycle event {event_id} disappeared after insert")
        return TaskLifecycleEvent.from_row(row)

    def list_lifecycle_events(
        self,
        task_id: str,
        *,
        limit: int | None = None,
        newest_first: bool = False,
    ) -> list[TaskLifecycleEvent]:
        if newest_first:
            sql = """
                SELECT *
                  FROM task_lifecycle_events
                 WHERE task_id = ?
                 ORDER BY created_at DESC, id DESC
            """
        else:
            sql = """
                SELECT *
                  FROM task_lifecycle_events
                 WHERE task_id = ?
                 ORDER BY id
            """
        params: tuple[object, ...] = (task_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (*params, limit)
        return [TaskLifecycleEvent.from_row(row) for row in self.db.fetchall(sql, params)]

    def list_events(
        self,
        task_id: str,
        *,
        limit: int | None = None,
        newest_first: bool = False,
    ) -> list[TaskLifecycleEvent]:
        """Compatibility alias for callers that use the shorter name."""
        return self.list_lifecycle_events(
            task_id,
            limit=limit,
            newest_first=newest_first,
        )


def record_lifecycle_event(
    db: DatabaseProtocol,
    task_id: str,
    from_state: str | None,
    to_state: str,
    reason: str,
    by_actor: str | None = None,
    *,
    by: str | None = None,
) -> int:
    return (
        TaskLifecycleEventManager(db)
        .record_lifecycle_event(
            task_id,
            from_state,
            to_state,
            reason,
            by_actor,
            by=by,
        )
        .id
    )


def list_lifecycle_events(
    db: DatabaseProtocol,
    task_id: str,
    *,
    limit: int | None = None,
    newest_first: bool = True,
) -> list[TaskLifecycleEvent]:
    """List lifecycle events for a task. Wrapper defaults to newest_first=True."""
    return TaskLifecycleEventManager(db).list_lifecycle_events(
        task_id,
        limit=limit,
        newest_first=newest_first,
    )

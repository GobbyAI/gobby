"""Append-only task lifecycle event storage helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from gobby.storage.database import DatabaseProtocol

# Reason recorded by ``gobby build`` (see gobby.build.lifecycle._record_build_event).
# This is the durable, append-only signal that automation was ever started for a
# task; stopping a build clears ``allow_automation`` but never removes this row,
# so it remains the source of truth for "has this task ever been built".
BUILD_EVENT_REASON = "gobby build"


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
    def from_row(cls, row: Mapping[str, Any]) -> TaskLifecycleEvent:
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
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
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
            row = conn.execute(
                """
                INSERT INTO task_lifecycle_events (
                    task_id, from_state, to_state, reason, by_actor
                )
                VALUES (?, ?, ?, ?, ?)
                RETURNING id
                """,
                (task_id, from_state, to_state, reason, actor),
            ).fetchone()
            if row is None:
                raise RuntimeError("SQLite did not return a lifecycle event id")
            event_id = int(row["id"])

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

    def has_build_event(self, task_id: str) -> bool:
        """Return True if ``gobby build`` was ever started for this task.

        Durable, append-only signal: a stopped build keeps this row, so it
        distinguishes a never-built task from a paused one.
        """
        return bool(
            self.db.fetchone(
                """
                SELECT 1
                  FROM task_lifecycle_events
                 WHERE task_id = ?
                   AND reason = ?
                 LIMIT 1
                """,
                (task_id, BUILD_EVENT_REASON),
            )
        )

    def tasks_with_build_event(self, task_ids: Sequence[str]) -> set[str]:
        """Batched form of :meth:`has_build_event` for list serialization."""
        ids = list(task_ids)
        if not ids:
            return set()
        placeholders = ", ".join("?" for _ in ids)
        query = (
            """
            SELECT DISTINCT task_id
              FROM task_lifecycle_events
             WHERE reason = ?
               AND task_id IN (
            """
            + placeholders
            + ")"
        )
        rows = self.db.fetchall(
            query,
            (BUILD_EVENT_REASON, *ids),
        )
        return {row["task_id"] for row in rows}


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

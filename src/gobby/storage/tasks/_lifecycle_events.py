"""Append-only task lifecycle event storage helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from gobby.failure_categories import FailureCategory
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import normalize_datetime_model


@normalize_datetime_model(required=("created_at",))
@dataclass(frozen=True)
class TaskLifecycleEvent:
    id: int
    task_id: str
    from_state: str | None
    to_state: str
    reason: str
    by_actor: str
    failure_category: FailureCategory | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> TaskLifecycleEvent:
        return cls(
            id=int(row["id"]),
            task_id=row["task_id"],
            from_state=row["from_state"],
            to_state=row["to_state"],
            reason=row["reason"],
            by_actor=row["by_actor"],
            failure_category=(
                FailureCategory(row["failure_category"])
                if row.get("failure_category") is not None
                else None
            ),
            created_at=row["created_at"],
        )


class TaskLifecycleEventManager:
    """Append-only lifecycle event audit storage."""

    def __init__(self, db: HubDatabase):
        self.db = db

    def record_lifecycle_event(
        self,
        task_id: str,
        from_state: str | None,
        to_state: str,
        reason: str,
        by_actor: str | None = None,
        *,
        by: str | None = None,
        failure_category: FailureCategory | None = None,
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
                    task_id, from_state, to_state, reason, by_actor, failure_category
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    task_id,
                    from_state,
                    to_state,
                    reason,
                    actor,
                    failure_category.value if failure_category is not None else None,
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("PostgreSQL hub did not return a lifecycle event id")
            event_id = int(row["id"])

        row = self.db.fetchone("SELECT * FROM task_lifecycle_events WHERE id = %s", (event_id,))
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
                 WHERE task_id = %s
                 ORDER BY created_at DESC, id DESC
            """
        else:
            sql = """
                SELECT *
                  FROM task_lifecycle_events
                 WHERE task_id = %s
                 ORDER BY id
            """
        params: tuple[object, ...] = (task_id,)
        if limit is not None:
            sql += " LIMIT %s"
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
    db: HubDatabase,
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
    db: HubDatabase,
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

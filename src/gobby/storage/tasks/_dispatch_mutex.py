"""Task dispatch mutex storage helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from gobby.storage.hub.protocol import DispatchMutexRow, HubDatabase


@dataclass(frozen=True)
class DispatchMutex:
    task_id: str
    lease_until: str | None
    lease_holder: str | None
    run_id: str | None
    action_kind: str | None
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> DispatchMutex:
        return cls(
            task_id=row["task_id"],
            lease_until=row["lease_until"],
            lease_holder=row["lease_holder"],
            run_id=row["run_id"],
            action_kind=row["action_kind"],
            updated_at=row["updated_at"],
        )


def _coerce_timestamp(value: datetime | str | None) -> str:
    if value is None:
        return datetime.now(UTC).isoformat()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    return value


class TaskDispatchMutexManager:
    """CRUD wrapper for the high-churn task dispatch mutex table."""

    def __init__(self, db: HubDatabase):
        self.db = db

    def ensure_table(self) -> None:
        """Create the table when a focused test uses an unmigrated database."""
        with self.db.transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_dispatch_mutex (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
                    lease_until TEXT,
                    lease_holder TEXT,
                    run_id TEXT,
                    action_kind TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dispatch_mutex_scan
                    ON task_dispatch_mutex (lease_until, run_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dispatch_mutex_run_id
                    ON task_dispatch_mutex (run_id)
                """
            )

    def get_mutex(self, task_id: str) -> DispatchMutex | None:
        row = self.db.fetchone(
            "SELECT * FROM task_dispatch_mutex WHERE task_id = %s",
            (task_id,),
        )
        return DispatchMutex.from_row(row) if row is not None else None

    def get_mutex_by_run_id(self, run_id: str) -> DispatchMutex | None:
        row = self.db.fetchone(
            "SELECT * FROM task_dispatch_mutex WHERE run_id = %s",
            (run_id,),
        )
        return DispatchMutex.from_row(row) if row is not None else None

    def acquire_mutex(
        self,
        task_id: str,
        holder: str,
        kind: str,
        ttl_seconds: int,
        run_id: str | None = None,
        now: datetime | str | None = None,
    ) -> bool:
        now_iso = _coerce_timestamp(now)
        lease_until = _coerce_timestamp(
            datetime.fromisoformat(now_iso) + timedelta(seconds=ttl_seconds)
        )

        with self.db.transaction_immediate(DispatchMutexRow(task_id=task_id)) as conn:
            row = conn.execute(
                "SELECT lease_until, lease_holder, run_id FROM task_dispatch_mutex WHERE task_id = %s",
                (task_id,),
            ).fetchone()
            if row is not None:
                existing_until = row["lease_until"]
                existing_holder = row["lease_holder"]
                existing_run_id = row["run_id"]
                if existing_until is not None and existing_until >= now_iso:
                    if (
                        run_id is None
                        or existing_holder != holder
                        or existing_run_id != run_id
                    ):
                        return False

            conn.execute(
                """
                INSERT INTO task_dispatch_mutex (
                    task_id, lease_until, lease_holder, run_id, action_kind, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT(task_id) DO UPDATE SET
                    lease_until = excluded.lease_until,
                    lease_holder = excluded.lease_holder,
                    run_id = excluded.run_id,
                    action_kind = excluded.action_kind,
                    updated_at = excluded.updated_at
                """,
                (task_id, lease_until, holder, run_id, kind, now_iso),
            )
            return True

    def release_mutex(self, task_id: str, holder: str) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM task_dispatch_mutex WHERE task_id = %s AND lease_holder = %s",
                (task_id, holder),
            )
            return cursor.rowcount > 0

    def force_release(self, task_id: str) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM task_dispatch_mutex WHERE task_id = %s",
                (task_id,),
            )
            return cursor.rowcount > 0

    def clear_by_run_id(self, run_id: str) -> int:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM task_dispatch_mutex WHERE run_id = %s",
                (run_id,),
            )
            return cursor.rowcount

    def attach_run_id(self, mutex_id: str, run_id: str) -> bool:
        updated_at = _coerce_timestamp(None)
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE task_dispatch_mutex
                   SET run_id = %s,
                       updated_at = %s
                 WHERE task_id = %s
                """,
                (run_id, updated_at, mutex_id),
            )
            return cursor.rowcount > 0

    def refresh_mutex_for_run(
        self,
        task_id: str,
        run_id: str,
        ttl_seconds: int,
        now: datetime | str | None = None,
    ) -> bool:
        now_iso = _coerce_timestamp(now)
        lease_until = _coerce_timestamp(
            datetime.fromisoformat(now_iso) + timedelta(seconds=ttl_seconds)
        )

        with self.db.transaction_immediate(DispatchMutexRow(task_id=task_id)) as conn:
            cursor = conn.execute(
                """
                UPDATE task_dispatch_mutex
                   SET lease_until = %s,
                       updated_at = %s
                 WHERE task_id = %s
                   AND run_id = %s
                """,
                (lease_until, now_iso, task_id, run_id),
            )
            return cursor.rowcount > 0

    def sweep_expired(self, *, now: datetime | str | None = None) -> int:
        now_iso = _coerce_timestamp(now)
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                DELETE FROM task_dispatch_mutex
                WHERE lease_until IS NOT NULL
                  AND lease_until < %s
                """,
                (now_iso,),
            )
            return cursor.rowcount


def get_mutex(db: HubDatabase, task_id: str) -> DispatchMutex | None:
    return TaskDispatchMutexManager(db).get_mutex(task_id)


def get_mutex_by_run_id(db: HubDatabase, run_id: str) -> DispatchMutex | None:
    return TaskDispatchMutexManager(db).get_mutex_by_run_id(run_id)


def acquire_mutex(
    db: HubDatabase,
    task_id: str,
    holder: str,
    kind: str,
    ttl_seconds: int,
    run_id: str | None = None,
    now: datetime | str | None = None,
) -> bool:
    return TaskDispatchMutexManager(db).acquire_mutex(
        task_id,
        holder=holder,
        kind=kind,
        run_id=run_id,
        ttl_seconds=ttl_seconds,
        now=now,
    )


def release_mutex(db: HubDatabase, task_id: str, holder: str) -> bool:
    return TaskDispatchMutexManager(db).release_mutex(task_id, holder)


def force_release(db: HubDatabase, task_id: str) -> bool:
    return TaskDispatchMutexManager(db).force_release(task_id)


def clear_by_run_id(db: HubDatabase, run_id: str) -> int:
    return TaskDispatchMutexManager(db).clear_by_run_id(run_id)


def attach_run_id(db: HubDatabase, mutex_id: str, run_id: str) -> bool:
    return TaskDispatchMutexManager(db).attach_run_id(mutex_id, run_id)


def refresh_mutex_for_run(
    db: HubDatabase,
    task_id: str,
    run_id: str,
    ttl_seconds: int,
    now: datetime | str | None = None,
) -> bool:
    return TaskDispatchMutexManager(db).refresh_mutex_for_run(
        task_id,
        run_id=run_id,
        ttl_seconds=ttl_seconds,
        now=now,
    )


def sweep_expired(db: HubDatabase, *, now: datetime | str | None = None) -> int:
    return TaskDispatchMutexManager(db).sweep_expired(now=now)

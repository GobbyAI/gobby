"""Durable backoff for task-validation LLM infrastructure failures.

When every text-generation candidate fails for infrastructure reasons (timeouts,
capability unavailability, provider transport/format errors), re-running
validation on every dispatcher heartbeat just burns the per-candidate timeout
budget and floods the logs. This module persists a per-task backoff window plus a
consecutive-failure counter so the validation entry point can skip until
``next_retry_at`` and escalate after too many consecutive infrastructure failures.

The counter resets the moment validation produces a real verdict (``valid`` or
``invalid``), so a past outage cannot poison later attempts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from gobby.storage.hub.protocol import HubDatabase

# Exponential backoff schedule for consecutive infrastructure failures.
BASE_BACKOFF_SECONDS = 300.0  # first retry ~5 min out
MAX_BACKOFF_SECONDS = 3600.0  # capped at 1 hour
# Escalate (surface to a human) after this many consecutive infra failures.
MAX_CONSECUTIVE_INFRA_FAILURES = 5


def compute_next_retry_at(consecutive_failures: int, now: datetime) -> datetime:
    """Return the next allowed retry time for ``consecutive_failures`` (>= 1).

    Exponential: ``BASE * 2**(failures - 1)`` seconds, capped at ``MAX``.
    """
    exponent = max(0, consecutive_failures - 1)
    delay = min(BASE_BACKOFF_SECONDS * (2.0**exponent), MAX_BACKOFF_SECONDS)
    return now + timedelta(seconds=delay)


@dataclass(frozen=True)
class ValidationBackoffState:
    """Persisted backoff state for one task's validation infrastructure failures."""

    task_id: str
    consecutive_failures: int
    next_retry_at: datetime | None
    last_error: str | None

    def is_in_backoff_window(self, now: datetime) -> bool:
        """True if validation should be skipped because the retry time is in the future."""
        return self.next_retry_at is not None and now < self.next_retry_at

    def should_escalate(self) -> bool:
        """True once consecutive infra failures reach the escalation threshold."""
        return self.consecutive_failures >= MAX_CONSECUTIVE_INFRA_FAILURES


def _as_utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = value[:-1] + "+00:00" if value.endswith("Z") else value
        value = datetime.fromisoformat(parsed)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class TaskValidationBackoffStore:
    """CRUD wrapper for the ``task_validation_backoff`` table."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def ensure_table(self) -> None:
        """Create the table when a focused test uses an unmigrated database."""
        with self.db.transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_validation_backoff (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TIMESTAMPTZ,
                    last_error TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

    def get(self, task_id: str) -> ValidationBackoffState | None:
        """Return the current backoff state for ``task_id`` or None if absent."""
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT task_id, consecutive_failures, next_retry_at, last_error "
                "FROM task_validation_backoff WHERE task_id = %s",
                (task_id,),
            ).fetchone()
        if row is None or not isinstance(row, Mapping):
            return None
        return ValidationBackoffState(
            task_id=row["task_id"],
            consecutive_failures=int(row["consecutive_failures"]),
            next_retry_at=_as_utc(row["next_retry_at"]),
            last_error=row["last_error"],
        )

    def record_failure(
        self,
        task_id: str,
        *,
        error: str | None,
        now: datetime | None = None,
    ) -> ValidationBackoffState:
        """Increment the failure counter, schedule the next retry, and persist it."""
        current = now or datetime.now(UTC)
        existing = self.get(task_id)
        consecutive = (existing.consecutive_failures if existing else 0) + 1
        next_retry_at = compute_next_retry_at(consecutive, current)
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO task_validation_backoff
                    (task_id, consecutive_failures, next_retry_at, last_error, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (task_id) DO UPDATE SET
                    consecutive_failures = excluded.consecutive_failures,
                    next_retry_at = excluded.next_retry_at,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (task_id, consecutive, next_retry_at, error, current),
            )
        return ValidationBackoffState(
            task_id=task_id,
            consecutive_failures=consecutive,
            next_retry_at=next_retry_at,
            last_error=error,
        )

    def clear(self, task_id: str) -> bool:
        """Drop any backoff row for ``task_id`` (called after a real verdict)."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM task_validation_backoff WHERE task_id = %s",
                (task_id,),
            )
            return bool(getattr(cursor, "rowcount", 0))

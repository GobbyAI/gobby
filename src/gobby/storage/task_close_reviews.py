"""Durable state for oversized task-close validator reviews."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import uuid4

from gobby.storage.hub.protocol import HubDatabase

ActiveTaskCloseReviewStatus = Literal["launching", "running", "finalizing"]
TerminalTaskCloseReviewStatus = Literal["closed", "invalid", "stale", "error"]
TaskCloseReviewStatus = ActiveTaskCloseReviewStatus | TerminalTaskCloseReviewStatus

ACTIVE_TASK_CLOSE_REVIEW_STATUSES: tuple[ActiveTaskCloseReviewStatus, ...] = (
    "launching",
    "running",
    "finalizing",
)
TERMINAL_TASK_CLOSE_REVIEW_STATUSES: tuple[TerminalTaskCloseReviewStatus, ...] = (
    "closed",
    "invalid",
    "stale",
    "error",
)

_COLUMNS = """
    id, task_id, task_ref, caller_session_id, agent_run_id,
    close_arguments, review_fingerprint, evidence_fingerprint, status,
    result_payload, error, launched_at, completed_at, delivered_at,
    created_at, updated_at
"""

# Marks a row that records an inline bounded review rather than a delegated
# background one. Such a row is born terminal and already delivered, so it
# holds no active-review lock and never reaches wake delivery; the kind tag is
# what keeps the memo lookup from ever reading an agentic row's payload, whose
# shape is a terminal review envelope rather than a bare verdict (#20866).
INLINE_CRITERIA_VERDICT_KIND = "inline_criteria_verdict"


@dataclass(frozen=True, slots=True)
class TaskCloseReview:
    """One durable close request and its validator lifecycle."""

    id: str
    task_id: str
    task_ref: str
    caller_session_id: str
    agent_run_id: str | None
    close_arguments: dict[str, Any]
    review_fingerprint: str
    evidence_fingerprint: str
    status: TaskCloseReviewStatus
    result_payload: dict[str, Any] | None
    error: str | None
    launched_at: datetime | None
    completed_at: datetime | None
    delivered_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_TASK_CLOSE_REVIEW_STATUSES

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_TASK_CLOSE_REVIEW_STATUSES


class TaskCloseReviewStore:
    """Transactional CRUD for ``task_close_reviews``."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def create_or_get_active(
        self,
        *,
        task_id: str,
        task_ref: str,
        caller_session_id: str,
        close_arguments: Mapping[str, Any],
        review_fingerprint: str,
        evidence_fingerprint: str,
    ) -> tuple[TaskCloseReview, bool]:
        """Create one launching review or return the task's concurrent active review."""
        review_id = str(uuid4())
        now = datetime.now(UTC)
        active = list(ACTIVE_TASK_CLOSE_REVIEW_STATUSES)
        with self.db.transaction() as conn:
            row = conn.execute(
                f"""
                INSERT INTO task_close_reviews (
                    id, task_id, task_ref, caller_session_id, close_arguments,
                    review_fingerprint, evidence_fingerprint, status,
                    created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, 'launching', %s, %s)
                ON CONFLICT (task_id)
                WHERE status = ANY (
                    ARRAY['launching'::text, 'running'::text, 'finalizing'::text]
                )
                DO NOTHING
                RETURNING {_COLUMNS}
                """,  # nosec B608 - static column fragment
                (
                    review_id,
                    task_id,
                    task_ref,
                    caller_session_id,
                    _json(close_arguments),
                    review_fingerprint,
                    evidence_fingerprint,
                    now,
                    now,
                ),
            ).fetchone()
            created = row is not None
            if row is None:
                row = conn.execute(
                    f"""
                    SELECT {_COLUMNS}
                    FROM task_close_reviews
                    WHERE task_id = %s AND status = ANY(%s)
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,  # nosec B608 - static column fragment
                    (task_id, active),
                ).fetchone()
        if row is None:
            raise RuntimeError(f"Active close review for task {task_id} disappeared")
        return _review_from_row(row), created

    def get_memoized_verdict(
        self,
        *,
        task_id: str,
        review_fingerprint: str,
        evidence_fingerprint: str,
    ) -> dict[str, Any] | None:
        """Return the verdict already reviewed for this exact evidence state."""
        with self.db.transaction() as conn:
            row = conn.execute(
                """
                SELECT result_payload
                FROM task_close_reviews
                WHERE task_id = %s
                  AND review_fingerprint = %s
                  AND evidence_fingerprint = %s
                  AND result_payload->>'kind' = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (task_id, review_fingerprint, evidence_fingerprint, INLINE_CRITERIA_VERDICT_KIND),
            ).fetchone()
        if not isinstance(row, Mapping):
            return None
        payload = _json_object(row["result_payload"])
        verdict = (payload or {}).get("verdict")
        return dict(verdict) if isinstance(verdict, Mapping) else None

    def memoize_verdict(
        self,
        *,
        task_id: str,
        task_ref: str,
        caller_session_id: str,
        close_arguments: Mapping[str, Any],
        review_fingerprint: str,
        evidence_fingerprint: str,
        verdict: Mapping[str, Any],
        valid: bool,
    ) -> None:
        """Record one bounded verdict so this evidence state is never re-reviewed.

        The row is written terminal, completed, and delivered in one statement:
        no lifecycle transition applies to a review that already happened
        inline, and the partial active-status unique index does not cover
        terminal rows.

        Superseded memos for the task are dropped in the same transaction. A
        task's evidence state moves forward — a new commit, a fresh edit,
        repaired criteria — so an older memo can only be hit again by reverting
        to that exact state, which is worth one more review rather than a row
        per attempt for the life of the project.
        """
        now = datetime.now(UTC)
        with self.db.transaction() as conn:
            conn.execute(
                """
                DELETE FROM task_close_reviews
                WHERE task_id = %s
                  AND result_payload->>'kind' = %s
                  AND (review_fingerprint, evidence_fingerprint) <> (%s, %s)
                """,
                (task_id, INLINE_CRITERIA_VERDICT_KIND, review_fingerprint, evidence_fingerprint),
            )
            conn.execute(
                """
                INSERT INTO task_close_reviews (
                    id, task_id, task_ref, caller_session_id, close_arguments,
                    review_fingerprint, evidence_fingerprint, status, result_payload,
                    completed_at, delivered_at, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                """,
                (
                    str(uuid4()),
                    task_id,
                    task_ref,
                    caller_session_id,
                    _json(close_arguments),
                    review_fingerprint,
                    evidence_fingerprint,
                    "closed" if valid else "invalid",
                    _json({"kind": INLINE_CRITERIA_VERDICT_KIND, "verdict": dict(verdict)}),
                    now,
                    now,
                    now,
                    now,
                ),
            )

    def get(self, review_id: str) -> TaskCloseReview | None:
        return self._get("id = %s", (review_id,))

    def get_by_run(self, run_id: str) -> TaskCloseReview | None:
        return self._get("agent_run_id = %s", (run_id,))

    def get_active_for_task(self, task_id: str) -> TaskCloseReview | None:
        return self._get(
            "task_id = %s AND status = ANY(%s)",
            (task_id, list(ACTIVE_TASK_CLOSE_REVIEW_STATUSES)),
        )

    def bind_run(self, review_id: str, run_id: str) -> TaskCloseReview | None:
        """Bind a successful launch and move the review to running."""
        now = datetime.now(UTC)
        with self.db.transaction() as conn:
            row = conn.execute(
                f"""
                UPDATE task_close_reviews
                SET agent_run_id = %s, status = 'running', launched_at = %s, updated_at = %s
                WHERE id = %s AND status = 'launching'
                RETURNING {_COLUMNS}
                """,  # nosec B608 - static column fragment
                (run_id, now, now, review_id),
            ).fetchone()
        return _review_from_row(row) if row is not None else None

    def claim_finalizing(self, review_id: str, run_id: str) -> TaskCloseReview | None:
        """Claim the single verdict-finalization transition."""
        return self._transition(
            review_id,
            from_status="running",
            to_status="finalizing",
            run_id=run_id,
        )

    def restore_running(self, review_id: str, run_id: str, *, error: str) -> bool:
        """Return a malformed submission to running so the validator can correct it."""
        now = datetime.now(UTC)
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE task_close_reviews
                SET status = 'running', error = %s, updated_at = %s
                WHERE id = %s AND agent_run_id = %s AND status = 'finalizing'
                """,
                (error, now, review_id, run_id),
            )
        return bool(getattr(cursor, "rowcount", 0))

    def finish(
        self,
        review_id: str,
        *,
        status: TerminalTaskCloseReviewStatus,
        result_payload: Mapping[str, Any],
        error: str | None = None,
    ) -> TaskCloseReview | None:
        """Persist a terminal payload and clear the task's active-review lock."""
        now = datetime.now(UTC)
        with self.db.transaction() as conn:
            row = conn.execute(
                f"""
                UPDATE task_close_reviews
                SET status = %s, result_payload = %s::jsonb, error = %s,
                    completed_at = %s, updated_at = %s
                WHERE id = %s AND status = ANY(%s)
                RETURNING {_COLUMNS}
                """,  # nosec B608 - static column fragment
                (
                    status,
                    _json(result_payload),
                    error,
                    now,
                    now,
                    review_id,
                    list(ACTIVE_TASK_CLOSE_REVIEW_STATUSES),
                ),
            ).fetchone()
        return _review_from_row(row) if row is not None else self.get(review_id)

    def mark_delivered(self, review_id: str) -> bool:
        """Record acknowledged wake delivery once."""
        now = datetime.now(UTC)
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE task_close_reviews
                SET delivered_at = COALESCE(delivered_at, %s), updated_at = %s
                WHERE id = %s AND status = ANY(%s)
                """,
                (now, now, review_id, list(TERMINAL_TASK_CLOSE_REVIEW_STATUSES)),
            )
        return bool(getattr(cursor, "rowcount", 0))

    def list_reconcilable(self) -> list[TaskCloseReview]:
        """List active intents and terminal payloads awaiting delivery."""
        with self.db.transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT {_COLUMNS}
                FROM task_close_reviews
                WHERE status = ANY(%s)
                   OR (status = ANY(%s) AND delivered_at IS NULL)
                ORDER BY created_at, id
                """,  # nosec B608 - static column fragment
                (
                    list(ACTIVE_TASK_CLOSE_REVIEW_STATUSES),
                    list(TERMINAL_TASK_CLOSE_REVIEW_STATUSES),
                ),
            ).fetchall()
        return [_review_from_row(row) for row in rows]

    def _transition(
        self,
        review_id: str,
        *,
        from_status: ActiveTaskCloseReviewStatus,
        to_status: ActiveTaskCloseReviewStatus,
        run_id: str,
    ) -> TaskCloseReview | None:
        now = datetime.now(UTC)
        with self.db.transaction() as conn:
            row = conn.execute(
                f"""
                UPDATE task_close_reviews
                SET status = %s, error = NULL, updated_at = %s
                WHERE id = %s AND agent_run_id = %s AND status = %s
                RETURNING {_COLUMNS}
                """,  # nosec B608 - static column fragment
                (to_status, now, review_id, run_id, from_status),
            ).fetchone()
        return _review_from_row(row) if row is not None else None

    def _get(self, predicate: str, params: tuple[object, ...]) -> TaskCloseReview | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM task_close_reviews WHERE {predicate}",  # nosec B608
                params,
            ).fetchone()
        return _review_from_row(row) if row is not None else None


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), default=str)


def _json_object(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise TypeError(f"Expected JSON object, got {type(value).__name__}")
    return {str(key): item for key, item in value.items()}


def _review_from_row(row: object) -> TaskCloseReview:
    if not isinstance(row, Mapping):
        raise TypeError(f"task_close_reviews query returned {type(row).__name__}; expected Mapping")
    close_arguments = _json_object(row["close_arguments"])
    if close_arguments is None:
        raise TypeError("task_close_reviews.close_arguments cannot be null")
    return TaskCloseReview(
        id=str(row["id"]),
        task_id=str(row["task_id"]),
        task_ref=str(row["task_ref"]),
        caller_session_id=str(row["caller_session_id"]),
        agent_run_id=str(row["agent_run_id"]) if row["agent_run_id"] is not None else None,
        close_arguments=close_arguments,
        review_fingerprint=str(row["review_fingerprint"]),
        evidence_fingerprint=str(row["evidence_fingerprint"]),
        status=cast(TaskCloseReviewStatus, str(row["status"])),
        result_payload=_json_object(row["result_payload"]),
        error=str(row["error"]) if row["error"] is not None else None,
        launched_at=cast(datetime | None, row["launched_at"]),
        completed_at=cast(datetime | None, row["completed_at"]),
        delivered_at=cast(datetime | None, row["delivered_at"]),
        created_at=cast(datetime, row["created_at"]),
        updated_at=cast(datetime, row["updated_at"]),
    )


__all__ = [
    "ACTIVE_TASK_CLOSE_REVIEW_STATUSES",
    "INLINE_CRITERIA_VERDICT_KIND",
    "TERMINAL_TASK_CLOSE_REVIEW_STATUSES",
    "ActiveTaskCloseReviewStatus",
    "TaskCloseReview",
    "TaskCloseReviewStatus",
    "TaskCloseReviewStore",
    "TerminalTaskCloseReviewStatus",
]

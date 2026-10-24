"""Read-only access to retired task-close review records."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from gobby.storage.hub.protocol import HubDatabase, Transaction


@dataclass(frozen=True, slots=True)
class HistoricalCloseReview:
    id: str
    task_id: str
    task_ref: str
    caller_session_id: str
    agent_run_id: str | None
    close_arguments: dict[str, Any]
    review_fingerprint: str
    evidence_fingerprint: str
    status: str
    result_payload: dict[str, Any] | None
    error: str | None
    launched_at: datetime | None
    completed_at: datetime | None
    delivered_at: datetime | None
    created_at: datetime
    updated_at: datetime
    diff_sha: str | None
    test_bodies_sha: str | None
    stable_facts: dict[str, Any] | None


class ReviewHistory:
    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def get(self, review_id: str) -> HistoricalCloseReview | None:
        row = self.db.fetchone(
            "SELECT * FROM task_close_review_history WHERE id = %s", (review_id,)
        )
        return _from_row(row) if row is not None else None

    def for_task(
        self,
        task_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[HistoricalCloseReview]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("History requires a limit of 1..100 and a nonnegative offset.")
        rows = self.db.fetchall(
            """SELECT * FROM task_close_review_history WHERE task_id = %s
               ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s""",
            (task_id, limit, offset),
        )
        return [_from_row(row) for row in rows]


def read_review_row(conn: Transaction, review_id: str) -> Mapping[str, Any] | None:
    """Read an immutable historical record inside a task-repair transaction."""
    return conn.execute(
        "SELECT * FROM task_close_review_history WHERE id = %s", (review_id,)
    ).fetchone()


def _object(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise TypeError("Historical review JSON must be an object.")
    return dict(value)


def _from_row(row: Mapping[str, Any]) -> HistoricalCloseReview:
    arguments = _object(row["close_arguments"])
    if arguments is None:
        raise TypeError("Historical close arguments cannot be null.")
    return HistoricalCloseReview(
        id=str(row["id"]),
        task_id=str(row["task_id"]),
        task_ref=str(row["task_ref"]),
        caller_session_id=str(row["caller_session_id"]),
        agent_run_id=str(row["agent_run_id"]) if row["agent_run_id"] is not None else None,
        close_arguments=arguments,
        review_fingerprint=str(row["review_fingerprint"]),
        evidence_fingerprint=str(row["evidence_fingerprint"]),
        status=str(row["status"]),
        result_payload=_object(row["result_payload"]),
        error=row["error"],
        launched_at=cast(datetime | None, row["launched_at"]),
        completed_at=cast(datetime | None, row["completed_at"]),
        delivered_at=cast(datetime | None, row["delivered_at"]),
        created_at=cast(datetime, row["created_at"]),
        updated_at=cast(datetime, row["updated_at"]),
        diff_sha=row["diff_sha"],
        test_bodies_sha=row["test_bodies_sha"],
        stable_facts=_object(row["stable_facts"]),
    )

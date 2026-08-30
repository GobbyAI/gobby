"""PostgreSQL access for session-feedback review runs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.json_helpers import json_dumps


@dataclass(frozen=True, slots=True)
class FeedbackRow:
    """One unreviewed session_feedback row as seen by the review loop."""

    id: str
    session_id: str
    source: str
    kind: str
    kind_other_label: str | None
    evidence: str
    impact: str
    frequency: str
    suggestion: str | None
    disposition: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class FeedbackReviewRun:
    """One feedback_review_runs row."""

    id: str
    status: str
    dry_run: bool
    window_start: datetime | None
    window_end: datetime | None
    rows_considered: int
    findings: dict[str, Any] | None
    actions: dict[str, Any] | None
    digest_md: str | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None


_ROW_COLUMNS = (
    "id, session_id, source, kind, kind_other_label, evidence, impact, "
    "frequency, suggestion, disposition, created_at"
)
_RUN_COLUMNS = (
    "id, status, dry_run, window_start, window_end, rows_considered, "
    "findings, actions, digest_md, error, created_at, completed_at"
)


class FeedbackReviewStore:
    """Hub-transaction storage for the session-feedback review loop."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def list_unreviewed(self, limit: int) -> list[FeedbackRow]:
        """Return the oldest unreviewed feedback rows, bounded by *limit*."""
        rows = self.db.fetchall(
            f"""
            SELECT {_ROW_COLUMNS}
            FROM session_feedback
            WHERE reviewed = FALSE
            ORDER BY created_at, id
            LIMIT %s
            """,
            (limit,),
        )
        return [FeedbackRow(**dict(row)) for row in rows]

    def create_run(
        self,
        *,
        dry_run: bool,
        window_start: datetime | None,
        window_end: datetime | None,
        rows_considered: int,
    ) -> str:
        """Insert a `running` run row and return its id."""
        run_id = str(uuid4())
        self.db.execute(
            """
            INSERT INTO feedback_review_runs (
                id, status, dry_run, window_start, window_end,
                rows_considered, created_at
            )
            VALUES (%s, 'running', %s, %s, %s, %s, %s)
            """,
            (run_id, dry_run, window_start, window_end, rows_considered, _now()),
        )
        return run_id

    def finalize_run(
        self,
        run_id: str,
        *,
        status: str,
        findings: dict[str, Any] | None = None,
        actions: dict[str, Any] | None = None,
        digest_md: str | None = None,
        error: str | None = None,
    ) -> None:
        """Move a run to a terminal status with its outputs."""
        self.db.execute(
            """
            UPDATE feedback_review_runs
            SET status = %s, findings = %s, actions = %s, digest_md = %s,
                error = %s, completed_at = %s
            WHERE id = %s
            """,
            (status, _json(findings), _json(actions), digest_md, error, _now(), run_id),
        )

    def mark_reviewed(self, feedback_ids: list[str], run_id: str) -> int:
        """Flip the batch reviewed and link it to *run_id* in one transaction."""
        if not feedback_ids:
            return 0
        with self.db.transaction() as conn:
            result = conn.execute(
                """
                UPDATE session_feedback
                SET reviewed = TRUE, review_run_id = %s
                WHERE id = ANY(%s) AND reviewed = FALSE
                """,
                (run_id, feedback_ids),
            )
            return int(result.rowcount or 0)

    def get_run(self, run_id: str) -> FeedbackReviewRun | None:
        row = self.db.fetchone(
            f"SELECT {_RUN_COLUMNS} FROM feedback_review_runs WHERE id = %s",
            (run_id,),
        )
        return _run_from_row(row) if row else None

    def latest_run(self) -> FeedbackReviewRun | None:
        row = self.db.fetchone(
            f"SELECT {_RUN_COLUMNS} FROM feedback_review_runs ORDER BY created_at DESC LIMIT 1"
        )
        return _run_from_row(row) if row else None


def _run_from_row(row: Any) -> FeedbackReviewRun:
    data = dict(row)
    # The hub row boundary serializes JSONB values to JSON strings.
    data["findings"] = _decode_json(data["findings"])
    data["actions"] = _decode_json(data["actions"])
    return FeedbackReviewRun(**data)


def _decode_json(value: Any) -> dict[str, Any] | None:
    if value is None or isinstance(value, dict):
        return value
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise TypeError(f"Expected a JSON object, got {type(decoded).__name__}")
    return decoded


def _now() -> datetime:
    return datetime.now(UTC)


def _json(value: dict[str, Any] | None) -> str | None:
    return None if value is None else json_dumps(value)

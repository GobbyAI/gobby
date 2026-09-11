"""PostgreSQL access for session-feedback review runs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
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
    observations: list[dict[str, Any]] = field(default_factory=list)


_ROW_COLUMNS = (
    "id, session_id, source, kind, kind_other_label, evidence, impact, "
    "frequency, suggestion, disposition, created_at"
)
_RUN_COLUMNS = (
    "id, status, dry_run, window_start, window_end, rows_considered, "
    "findings, actions, digest_md, error, created_at, completed_at, observations"
)


class FeedbackReviewStore:
    """Hub-transaction storage for the session-feedback review loop."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def freeze_batch(self, limit: int, *, dry_run: bool) -> tuple[str, list[FeedbackRow]] | None:
        """Admit one review and freeze exactly its inputs before launching a reader."""
        with self.db.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('feedback-review', 0))")
            if conn.execute(
                "SELECT 1 FROM feedback_review_runs WHERE status = 'running' LIMIT 1"
            ).fetchone():
                return None
            rows = self.list_unreviewed(limit)
            if not rows:
                return None
            run_id = self.create_run(
                dry_run=dry_run,
                window_start=rows[0].created_at,
                window_end=rows[-1].created_at,
                rows_considered=len(rows),
                observations=rows,
            )
            return run_id, rows

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
        observations: list[FeedbackRow] | None = None,
    ) -> str:
        """Insert a `running` run row and return its id."""
        run_id = str(uuid4())
        self.db.execute(
            """
            INSERT INTO feedback_review_runs (
                id, status, dry_run, window_start, window_end,
                rows_considered, created_at, observations
            )
            VALUES (%s, 'running', %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                dry_run,
                window_start,
                window_end,
                rows_considered,
                _now(),
                json_dumps([asdict(row) for row in observations or []]),
            ),
        )
        return run_id

    def save_progress(self, run_id: str, findings: dict[str, Any], actions: dict[str, Any]) -> None:
        """Checkpoint accepted findings and every completed action before consuming inputs."""
        self.db.execute(
            "UPDATE feedback_review_runs SET findings = %s, actions = %s WHERE id = %s",
            (_json(findings), _json(actions), run_id),
        )

    def assign_reviewer(self, run_id: str, agent_run_id: str, report_path: str) -> None:
        """Bind submission authority and its Markdown destination before review."""
        self.db.execute(
            "UPDATE feedback_review_runs SET actions = COALESCE(actions, '{}'::jsonb) || %s::jsonb "
            "WHERE id = %s AND status = 'running'",
            (_json({"reviewer_agent_run_id": agent_run_id, "report_path": report_path}), run_id),
        )

    def submit_review(
        self, run_id: str, session_id: str, findings: dict[str, Any], summary_md: str
    ) -> str:
        """Persist a reviewer submission independently of agent completion/handoff."""
        from gobby.feedback.agent import validate_feedback_findings

        if not summary_md.strip():
            raise ValueError("A nonblank Markdown summary is required")
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM feedback_review_runs WHERE id = %s FOR UPDATE", (run_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown feedback review run: {run_id}")
            run = _run_from_row(row)
            if run.status != "running":
                raise ValueError("Feedback review is no longer accepting submissions")
            actions = run.actions or {}
            owner = conn.execute(
                "SELECT child_session_id FROM agent_runs WHERE id = %s",
                (actions.get("reviewer_agent_run_id"),),
            ).fetchone()
            if owner is None or owner["child_session_id"] != session_id:
                raise ValueError("Only the assigned reviewer may submit this review")
            validate_feedback_findings(
                findings, observation_ids=[item["id"] for item in run.observations]
            )
            report_path = actions.get("report_path")
            if not isinstance(report_path, str) or not report_path:
                raise ValueError("Feedback review has no Markdown destination")
            write_review_report(report_path, summary_md)
            conn.execute(
                "UPDATE feedback_review_runs SET findings = %s, digest_md = %s WHERE id = %s",
                (_json(findings), summary_md, run_id),
            )
            return report_path

    def observations_page(self, run_id: str, *, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """Read frozen observations; concurrent feedback never enters this batch."""
        _validate_page(offset, limit)
        row = self.db.fetchone(
            "SELECT jsonb_array_length(observations) AS total FROM feedback_review_runs WHERE id = %s",
            (run_id,),
        )
        if row is None:
            raise ValueError(f"Unknown feedback review run: {run_id}")
        rows = self.db.fetchall(
            """SELECT item FROM feedback_review_runs,
                jsonb_array_elements(observations) WITH ORDINALITY AS frozen(item, ordinal)
                WHERE id = %s ORDER BY ordinal LIMIT %s OFFSET %s""",
            (run_id, limit, offset),
        )
        items = [
            json.loads(item["item"]) if isinstance(item["item"], str) else item["item"]
            for item in rows
        ]
        total = int(row["total"])
        return {
            "run_id": run_id,
            "observations": items,
            "total": total,
            "next_offset": offset + len(items) if offset + len(items) < total else None,
            "historical_inputs_missing": total == 0,
        }

    def results_page(self, run_id: str, *, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """Read accepted clusters with their actual daemon action outcomes."""
        _validate_page(offset, limit)
        run = self.get_run(run_id)
        if run is None:
            raise ValueError(f"Unknown feedback review run: {run_id}")
        clusters = (run.findings or {}).get("clusters", [])
        items = clusters[offset : offset + limit]
        ids = {value for cluster in items for value in cluster.get("observation_ids", [])}
        actions = run.actions or {}
        outcomes = {
            key: [
                item
                for item in actions.get(key, [])
                if ids.intersection(item.get("observation_ids", []))
            ]
            for key in ("filed", "suppressed", "deferred", "failed", "retained")
        }
        return {
            "run_id": run_id,
            "status": run.status,
            "error": run.error,
            "clusters": items,
            "outcomes": outcomes,
            "review_attempts": actions.get("review_attempts", []),
            "total": len(clusters),
            "next_offset": offset + len(items) if offset + len(items) < len(clusters) else None,
        }

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

    def mark_running_interrupted(self) -> int:
        """Finalize orphaned running runs as interrupted.

        Runs live inside the daemon process, so at startup no run can
        legitimately still be running; their rows stay unreviewed and are
        re-picked by the next run.
        """
        with self.db.transaction() as conn:
            result = conn.execute(
                """
                UPDATE feedback_review_runs
                SET status = 'interrupted', completed_at = %s
                WHERE status = 'running'
                """,
                (_now(),),
            )
            return int(result.rowcount or 0)

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
    observations = data.get("observations", [])
    data["observations"] = (
        json.loads(observations) if isinstance(observations, str) else observations
    )
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


def write_review_report(report_path: str, markdown: str) -> None:
    """Replace a run's local report without exposing a partially written document."""
    path = Path(report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".md.tmp")
    try:
        temporary.write_text(markdown.rstrip() + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _json(value: dict[str, Any] | None) -> str | None:
    return None if value is None else json_dumps(value)


def _validate_page(offset: int, limit: int) -> None:
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("offset must be nonnegative and limit must be between 1 and 100")

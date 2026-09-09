"""Publication state and append-only attempt history, independent of source execution."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.json_helpers import json_dumps

_SOURCES = {"feedback": "feedback_review_runs", "dream": "memory_dream_runs"}
logger = logging.getLogger(__name__)
_TERMINAL = {"completed", "partial", "failed", "interrupted", "reverted", "revert_failed"}


def transient_publication_error(error: str) -> bool:
    return any(
        value in error.lower()
        for value in (
            "index.lock",
            "cannot lock ref",
            "temporarily unavailable",
            "connection reset",
            "input/output error",
            "message too long",
            "socket unavailable",
        )
    )


def report_path(kind: str, run_id: str) -> str:
    if kind not in _SOURCES:
        raise ValueError("source_kind must be feedback or dream")
    return f"docs/reports/{kind}/{UUID(run_id)}.md"


def queue_terminal_report(db: HubDatabase, kind: str, run_id: str, project_id: str) -> None:
    """Publication admission cannot relabel an already terminal source execution."""
    try:
        ReportStore(db).request(kind, run_id, project_id)
    except Exception:
        logger.exception(
            "Could not queue %s synthesis report for run %s; request_report can retry without replaying the source",
            kind,
            run_id,
        )


class ReportStore:
    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def request(self, kind: str, run_id: str, project_id: str) -> dict[str, Any]:
        path = report_path(kind, run_id)
        source = self.db.fetchone(f"SELECT status FROM {_SOURCES[kind]} WHERE id = %s", (run_id,))  # nosec B608
        if source is None or source["status"] not in _TERMINAL:
            raise ValueError("Reports require an existing terminal source run")
        self.db.execute(
            """INSERT INTO synthesis_reports (source_kind, source_run_id, project_id, report_path)
            VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            (kind, run_id, project_id, path),
        )
        return self.get(kind, run_id)

    def source_ready(self, kind: str, run_id: str) -> bool:
        report_path(kind, run_id)
        fields = "status, options" if kind == "dream" else "status"
        source = self.db.fetchone(f"SELECT {fields} FROM {_SOURCES[kind]} WHERE id = %s", (run_id,))  # nosec B608
        if source is None or source["status"] not in _TERMINAL:
            return False
        options = source.get("options") or {}
        if isinstance(options, str):
            options = json.loads(options)
        return not options.get("_restart_resume_pending", False)

    def get(self, kind: str, run_id: str, *, include_content: bool = True) -> dict[str, Any]:
        report_path(kind, run_id)
        row = self.db.fetchone(
            "SELECT * FROM synthesis_reports WHERE source_kind = %s AND source_run_id = %s",
            (kind, run_id),
        )
        if row is None:
            return {"source_kind": kind, "source_run_id": run_id, "status": "not_requested"}
        report = dict(row)
        if not include_content:
            report.pop("content", None)
        return report

    def attempts(
        self, kind: str, run_id: str, *, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        report_path(kind, run_id)
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("offset must be nonnegative and limit must be between 1 and 100")
        rows = self.db.fetchall(
            """SELECT * FROM synthesis_report_attempts WHERE source_kind = %s AND source_run_id = %s
            ORDER BY started_at, id LIMIT %s OFFSET %s""",
            (kind, run_id, limit + 1, offset),
        )
        attempts = [dict(row) for row in rows[:limit]]
        for attempt in attempts:
            if isinstance(attempt["diagnostics"], str):
                attempt["diagnostics"] = json.loads(attempt["diagnostics"])
        return {"attempts": attempts, "next_offset": offset + limit if len(rows) > limit else None}

    def prepare_task(self, kind: str, run_id: str) -> dict[str, Any]:
        """Atomically retain the one documentation task even across launch retries."""
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM synthesis_reports WHERE source_kind = %s AND source_run_id = %s FOR UPDATE",
                (kind, run_id),
            ).fetchone()
            if row is None:
                raise ValueError("Report has not been requested")
            if row["task_id"] is None:
                task = LocalTaskManager(self.db).create_task(
                    project_id=str(row["project_id"]),
                    title=f"Synthesize {kind} run {run_id}",
                    description=f"Write and commit {row['report_path']} from recorded evidence. Retain the report branch without merging it.",
                    category="docs",
                    task_type="task",
                    labels=["synthesis-report"],
                    validation_criteria="Report identifies the source run, explains findings and actual outcomes, cites evidence, states limitations, and is committed on a retained isolated branch.",
                )
                conn.execute(
                    """UPDATE synthesis_reports SET task_id = %s, branch_name = %s, updated_at = now()
                    WHERE source_kind = %s AND source_run_id = %s""",
                    (task.id, f"reports/{kind}/{run_id}", kind, run_id),
                )
        return self.get(kind, run_id)

    def begin(self, kind: str, run_id: str) -> str | None:
        attempt_id = str(uuid4())
        with self.db.transaction() as conn:
            row = conn.execute(
                """UPDATE synthesis_reports SET status = CASE WHEN content IS NULL THEN 'synthesizing'
                ELSE 'publishing' END, updated_at = now()
                WHERE source_kind = %s AND source_run_id = %s AND status = 'pending' RETURNING content""",
                (kind, run_id),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """INSERT INTO synthesis_report_attempts (id, source_kind, source_run_id, phase, status)
                VALUES (%s, %s, %s, %s, 'running')""",
                (
                    attempt_id,
                    kind,
                    run_id,
                    "publication" if row["content"] is not None else "launch",
                ),
            )
        return attempt_id

    def attach_agent(
        self, attempt_id: str, agent_run_id: str | None, worktree_id: str | None
    ) -> None:
        with self.db.transaction() as conn:
            row = conn.execute(
                """UPDATE synthesis_report_attempts SET agent_run_id = COALESCE(%s, agent_run_id),
                diagnostics = diagnostics || %s::jsonb WHERE id = %s AND status = 'running'
                RETURNING source_kind, source_run_id""",
                (
                    agent_run_id,
                    json_dumps({"agent_run_id": agent_run_id, "worktree_id": worktree_id}),
                    attempt_id,
                ),
            ).fetchone()
            if row is None:
                raise ValueError("Report attempt is no longer running")
            conn.execute(
                """UPDATE synthesis_reports SET worktree_id = COALESCE(%s, worktree_id), updated_at = now()
                WHERE source_kind = %s AND source_run_id = %s""",
                (worktree_id, row["source_kind"], row["source_run_id"]),
            )

    def save_draft(self, kind: str, run_id: str, content: str) -> dict[str, Any]:
        path = report_path(kind, run_id)
        try:
            if not content.strip() or len(content.encode()) > 1_048_576 or "\x00" in content:
                raise ValueError("Report must be nonempty Markdown of at most 1 MiB")
            required = [run_id, "## Findings", "## Outcomes", "## Limitations"]
            if any(value not in content for value in required):
                raise ValueError(
                    "Report must identify its run and include Findings, Outcomes, and Limitations sections"
                )
        except ValueError as exc:
            self.record_failure(kind, run_id, "validation", str(exc))
            raise
        digest = hashlib.sha256(content.encode()).hexdigest()
        result = self.db.execute(
            """UPDATE synthesis_reports SET content = %s, content_hash = %s, status = 'publishing', updated_at = now()
            WHERE source_kind = %s AND source_run_id = %s AND status IN ('synthesizing', 'publishing')""",
            (content, digest, kind, run_id),
        )
        if result.rowcount != 1:
            raise ValueError("Report is not in an active publication attempt")
        self.db.execute(
            """UPDATE synthesis_report_attempts SET phase = 'publication'
            WHERE source_kind = %s AND source_run_id = %s AND status = 'running'""",
            (kind, run_id),
        )
        return {"report_path": path, "content_hash": digest}

    def record_failure(self, kind: str, run_id: str, phase: str, error: str) -> None:
        report_path(kind, run_id)
        if phase not in {"launch", "synthesis", "validation", "publication", "verification"}:
            raise ValueError("Unknown publication phase")
        attempt = self.active_attempt(kind, run_id)
        if attempt is not None:
            self.phase(str(attempt["id"]), phase)
            self.fail(
                str(attempt["id"]),
                error,
                transient=phase in {"launch", "publication"} and transient_publication_error(error),
            )

    def fail(
        self,
        attempt_id: str,
        error: str,
        *,
        transient: bool = False,
        interrupted: bool = False,
        **diagnostics: Any,
    ) -> None:
        with self.db.transaction() as conn:
            row = conn.execute(
                """UPDATE synthesis_report_attempts SET status = %s, error = %s, diagnostics = diagnostics || %s::jsonb,
                completed_at = now() WHERE id = %s AND status = 'running' RETURNING source_kind, source_run_id""",
                (
                    "interrupted" if interrupted else "failed",
                    error,
                    json_dumps(diagnostics),
                    attempt_id,
                ),
            ).fetchone()
            if row is None:
                return
            conn.execute(
                """UPDATE synthesis_reports SET
                status = CASE WHEN %s AND auto_retries = 0 THEN 'pending' ELSE %s END,
                auto_retries = CASE WHEN %s THEN LEAST(auto_retries + 1, 1) ELSE auto_retries END,
                updated_at = now() WHERE source_kind = %s AND source_run_id = %s""",
                (
                    transient,
                    "interrupted" if interrupted else "failed",
                    transient,
                    row["source_kind"],
                    row["source_run_id"],
                ),
            )

    def retry(self, kind: str, run_id: str) -> dict[str, Any]:
        report_path(kind, run_id)
        self.db.execute(
            """UPDATE synthesis_reports SET status = 'pending', updated_at = now()
            WHERE source_kind = %s AND source_run_id = %s AND status IN ('failed', 'interrupted')""",
            (kind, run_id),
        )
        return self.get(kind, run_id, include_content=False)

    def recover(self) -> None:
        # A process can stop after spawn commits its run but before attach_agent.
        # Recover that identity from the report's unique task before considering retry.
        unlinked = self.db.fetchall(
            """SELECT a.id, r.id AS agent_run_id, r.worktree_id FROM synthesis_report_attempts a
            JOIN synthesis_reports p USING (source_kind, source_run_id)
            JOIN LATERAL (SELECT id, worktree_id FROM agent_runs WHERE task_id = p.task_id
              AND agent_name = 'synthesis-reporter' AND created_at >= a.started_at
              ORDER BY created_at DESC LIMIT 1) r ON TRUE
            WHERE a.status = 'running' AND a.agent_run_id IS NULL AND r.worktree_id IS NOT NULL"""
        )
        for row in unlinked:
            self.attach_agent(str(row["id"]), str(row["agent_run_id"]), str(row["worktree_id"]))
        rows = self.db.fetchall(
            """SELECT a.id, a.agent_run_id, r.error, r.started_at AS agent_started_at
            FROM synthesis_report_attempts a LEFT JOIN agent_runs r ON r.id = a.agent_run_id
            WHERE a.status = 'running' AND (r.id IS NULL OR r.status NOT IN ('pending', 'running', 'success'))"""
        )
        for row in rows:
            launch_failed = row["agent_started_at"] is None and bool(row["error"])
            if launch_failed:
                self.phase(str(row["id"]), "launch")
            self.fail(
                str(row["id"]),
                row["error"] or "daemon restarted during publication",
                transient=row["agent_run_id"] is None
                or (launch_failed and transient_publication_error(str(row["error"]))),
                interrupted=True,
            )

    def pending(self) -> list[dict[str, Any]]:
        self.admit_terminal_sources()
        return [
            dict(row)
            for row in self.db.fetchall(
                "SELECT source_kind, source_run_id FROM synthesis_reports WHERE status IN ('pending', 'synthesizing', 'publishing') ORDER BY created_at LIMIT 10"
            )
        ]

    def admit_terminal_sources(self) -> None:
        # Source JSON carries a durable request marker written with its terminal
        # result. This repairs an interrupted enqueue without sweeping history.
        for kind, table in _SOURCES.items():
            column = "actions" if kind == "feedback" else "summary"
            rows = self.db.fetchall(
                f"""SELECT s.id, s.{column}->>'report_project_id' AS project_id FROM {table} s
                WHERE s.{column}->>'report_project_id' IS NOT NULL
                AND s.status IN ('completed', 'partial', 'failed', 'interrupted')
                AND NOT EXISTS (SELECT 1 FROM synthesis_reports r
                    WHERE r.source_kind = %s AND r.source_run_id = s.id) LIMIT 10""",
                (kind,),
            )
            for row in rows:
                self.request(kind, str(row["id"]), str(row["project_id"]))

    def active_attempt(self, kind: str, run_id: str) -> dict[str, Any] | None:
        row = self.db.fetchone(
            """SELECT id, agent_run_id FROM synthesis_report_attempts
            WHERE source_kind = %s AND source_run_id = %s AND status = 'running'""",
            (kind, run_id),
        )
        return dict(row) if row is not None else None

    def phase(self, attempt_id: str, phase: str) -> None:
        self.db.execute(
            "UPDATE synthesis_report_attempts SET phase = %s WHERE id = %s AND status = 'running'",
            (phase, attempt_id),
        )

    def interrupted_coordinator(self, attempt_id: str) -> None:
        self.db.execute(
            """UPDATE synthesis_report_attempts SET diagnostics = diagnostics ||
            jsonb_build_object('coordinator_interrupted_at', now()) WHERE id = %s AND status = 'running'""",
            (attempt_id,),
        )

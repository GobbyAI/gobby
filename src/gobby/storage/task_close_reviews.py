"""Durable state for queued task-close reviewer runs."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from psycopg.errors import UniqueViolation

from gobby.storage.agents import DELIBERATE_STOP_TERMINAL_REASONS
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import parse_stored_datetime, utc_now

ActiveTaskCloseReviewStatus = Literal["queued", "launching", "running", "finalizing"]
TerminalTaskCloseReviewStatus = Literal["closed", "invalid", "external_pending", "stale", "error"]
TaskCloseReviewStatus = ActiveTaskCloseReviewStatus | TerminalTaskCloseReviewStatus
TaskCloseReviewErrorClass = Literal["retryable_infrastructure", "action_required"]

ACTIVE_TASK_CLOSE_REVIEW_STATUSES: tuple[ActiveTaskCloseReviewStatus, ...] = (
    "queued",
    "launching",
    "running",
    "finalizing",
)
_ACTIVE_TASK_CLOSE_REVIEW_CONSTRAINT = "uq_task_close_reviews_active_task"
TERMINAL_TASK_CLOSE_REVIEW_STATUSES: tuple[TerminalTaskCloseReviewStatus, ...] = (
    "closed",
    "invalid",
    "external_pending",
    "stale",
    "error",
)

REVIEWER_RUN_ENDED_SUCCESS_ERROR = (
    "Task-close reviewer run ended with status success before finalization."
)

# How long a `finalizing` row must sit untouched before a running daemon may
# treat it as abandoned. `claim_finalizing` stamps `updated_at` at the claim, so
# this is the age of the finalization attempt itself. It only has to sit
# comfortably above a real `commit_close` — git subprocesses and the closure
# cascade, seconds in practice — because the in-process set below, not the
# clock, is what proves liveness. The clock covers the sliver between the claim
# committing and the submission registering itself.
FINALIZING_ORPHAN_GRACE_SECONDS = 600

_finalizing_in_process: set[str] = set()


@contextmanager
def finalizing_in_process(review_id: str) -> Iterator[None]:
    """Hold `review_id` in this process's finalizing set for the block.

    `claim_finalizing` keeps a row `finalizing` across `commit_close`, so the
    status alone cannot tell a live submission from an abandoned one. The
    daemon finalizing a row does so in-process, which makes this set an exact
    liveness oracle for this daemon: a `finalizing` row whose id is absent is
    held by no submission running here.

    Releasing in `finally` is load-bearing. A submission that dies by exception
    is exactly the strand being recovered, so it must leave the row sweepable.
    """
    _finalizing_in_process.add(review_id)
    try:
        yield
    finally:
        _finalizing_in_process.discard(review_id)


def is_finalizing_in_process(review_id: str) -> bool:
    """Report whether a submission in this process currently holds `review_id`."""
    return review_id in _finalizing_in_process


class TaskCloseReviewStaleTaskError(RuntimeError):
    """Raised when review launch loses its task timestamp precondition."""


@dataclass(frozen=True, slots=True)
class QueuedAgentRunSpec:
    """Agent-run identity persisted atomically with a queued close review."""

    id: str
    machine_id: str
    provider: str
    model: str | None
    agent_name: str
    prompt: str
    timeout_seconds: float
    requested_reasoning_effort: str | None = None


_COLUMNS = """
    id, task_id, task_ref, caller_session_id, agent_run_id,
    close_arguments, review_fingerprint, evidence_fingerprint,
    diff_sha, test_bodies_sha, stable_facts, status,
    result_payload, error, launched_at, completed_at, delivered_at,
    created_at, updated_at
"""
_QUALIFIED_COLUMNS = """
    r.id, r.task_id, r.task_ref, r.caller_session_id, r.agent_run_id,
    r.close_arguments, r.review_fingerprint, r.evidence_fingerprint,
    r.diff_sha, r.test_bodies_sha, r.stable_facts, r.status,
    r.result_payload, r.error, r.launched_at, r.completed_at, r.delivered_at,
    r.created_at, r.updated_at
"""

# Marks the verdict a background reviewer submitted, captured on arrival while
# the review is still `finalizing`. It is forensic only and carries a kind of
# its own so the terminal envelope stays distinguishable from other payloads.
SUBMITTED_VERDICT_KIND = "submitted_close_verdict"


@dataclass(frozen=True, slots=True)
class TaskCloseReview:
    """One durable close request and its reviewer lifecycle."""

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
    diff_sha: str | None = None
    test_bodies_sha: str | None = None
    stable_facts: dict[str, Any] | None = None

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

    def has_retry_wait(self, task_id: str, *, caller_session_id: str) -> bool:
        """Only the latest task attempt can grant its caller an unexpired retry wait."""
        with self.db.transaction() as conn:
            row = conn.execute(
                f"""
                SELECT {_COLUMNS} FROM task_close_reviews
                WHERE task_id = %s
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,  # nosec B608 - static column fragment
                (task_id,),
            ).fetchone()
        if row is None:
            return False
        review = _review_from_row(row)
        payload = review.result_payload or {}
        if (
            review.caller_session_id != caller_session_id
            or review.status != "error"
            or payload.get("error_class") != "retryable_infrastructure"
            or review.close_arguments.get("_retry_superseded_at")
        ):
            return False
        retry_after = payload.get("retry_after")
        if not isinstance(retry_after, str):
            return False
        try:
            expiry = parse_stored_datetime(retry_after)
        except (TypeError, ValueError):
            return False
        return expiry is not None and utc_now() < expiry

    def supersede_retry_wait(self, task_id: str, *, caller_session_id: str) -> None:
        """Starting a retry consumes old waits even if evaluation never launches a review."""
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE task_close_reviews
                SET close_arguments = close_arguments || %s::jsonb, updated_at = %s
                WHERE task_id = %s AND caller_session_id = %s AND status = 'error'
                  AND result_payload ->> 'error_class' = 'retryable_infrastructure'
                  AND NOT (close_arguments ? '_retry_superseded_at')
                """,
                (
                    json.dumps({"_retry_superseded_at": utc_now().isoformat()}),
                    utc_now(),
                    task_id,
                    caller_session_id,
                ),
            )

    def create_or_get_active(
        self,
        *,
        task_id: str,
        task_ref: str,
        caller_session_id: str,
        commit_shas: Sequence[str],
        close_arguments: Mapping[str, Any],
        expected_task_updated_at: datetime,
        review_fingerprint: str,
        evidence_fingerprint: str,
        diff_sha: str,
        test_bodies_sha: str,
        stable_facts: Mapping[str, object],
        review_id: str,
        run: QueuedAgentRunSpec,
    ) -> tuple[TaskCloseReview, bool]:
        """Atomically enqueue a review and its durable waitable agent run."""
        now = datetime.now(UTC)
        active = list(ACTIVE_TASK_CLOSE_REVIEW_STATUSES)
        with self.db.transaction() as conn:
            row = conn.execute(
                f"""
                WITH launch_task AS (
                    SELECT id
                    FROM tasks
                    WHERE id = %s AND updated_at = %s
                    FOR UPDATE
                )
                INSERT INTO task_close_reviews (
                    id, task_id, task_ref, caller_session_id, close_arguments,
                    review_fingerprint, evidence_fingerprint,
                    diff_sha, test_bodies_sha, stable_facts, status, agent_run_id,
                    created_at, updated_at
                )
                SELECT
                    %s, %s, %s, %s, %s::jsonb, %s, %s,
                    %s, %s, %s::jsonb, 'queued', %s, %s, %s
                FROM launch_task
                ON CONFLICT (task_id)
                WHERE status = ANY (
                    ARRAY[
                        'queued'::text, 'launching'::text,
                        'running'::text, 'finalizing'::text
                    ]
                )
                DO NOTHING
                RETURNING {_COLUMNS}
                """,  # nosec B608 - static column fragment
                (
                    task_id,
                    expected_task_updated_at,
                    review_id,
                    task_id,
                    task_ref,
                    caller_session_id,
                    _json(close_arguments),
                    review_fingerprint,
                    evidence_fingerprint,
                    diff_sha,
                    test_bodies_sha,
                    _json(stable_facts),
                    run.id,
                    now,
                    now,
                ),
            ).fetchone()
            created = row is not None
            if created:
                for commit_sha in dict.fromkeys(commit_shas):
                    conn.execute(
                        """
                        UPDATE tasks
                           SET commits = COALESCE(commits, '[]'::jsonb)
                                         || jsonb_build_array(%s::text)
                         WHERE id = %s
                           AND NOT COALESCE(commits, '[]'::jsonb)
                                   @> jsonb_build_array(%s::text)
                        """,
                        (commit_sha, task_id, commit_sha),
                    )
                conn.execute(
                    """
                    INSERT INTO agent_runs (
                        id, machine_id, parent_session_id, agent_name,
                        provider, model, requested_reasoning_effort,
                        status, prompt, timeout_seconds
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'queued', %s, %s)
                    """,
                    (
                        run.id,
                        run.machine_id,
                        caller_session_id,
                        run.agent_name,
                        run.provider,
                        run.model,
                        run.requested_reasoning_effort,
                        run.prompt,
                        run.timeout_seconds,
                    ),
                )
            if row is None:
                task_row = conn.execute(
                    "SELECT updated_at FROM tasks WHERE id = %s FOR UPDATE",
                    (task_id,),
                ).fetchone()
                if (
                    not isinstance(task_row, Mapping)
                    or task_row["updated_at"] != expected_task_updated_at
                ):
                    raise TaskCloseReviewStaleTaskError(task_id)
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

    def list_queued_project_ids(self) -> list[str]:
        """Return projects with queued reviews in promotion order."""
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT t.project_id, MIN(r.created_at) AS first_queued_at
                FROM task_close_reviews r
                JOIN tasks t ON t.id = r.task_id
                WHERE r.status = 'queued'
                GROUP BY t.project_id
                ORDER BY first_queued_at, t.project_id
                """
            ).fetchall()
        return [str(row["project_id"]) for row in rows]

    def claim_queued(
        self,
        *,
        project_id: str,
        max_concurrency: int,
    ) -> list[TaskCloseReview]:
        """Promote FIFO reviews while a project has execution capacity."""
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        now = datetime.now(UTC)
        claimed: list[TaskCloseReview] = []
        with self.db.transaction() as conn:
            project = conn.execute(
                "SELECT id FROM projects WHERE id = %s FOR UPDATE",
                (project_id,),
            ).fetchone()
            if project is None:
                return []
            active = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM task_close_reviews r
                JOIN tasks t ON t.id = r.task_id
                WHERE t.project_id = %s
                  AND r.status = ANY(%s)
                """,
                (project_id, ["launching", "running", "finalizing"]),
            ).fetchone()
            active_count = int(active["count"]) if isinstance(active, Mapping) else 0
            slots = max_concurrency - active_count
            if slots <= 0:
                return []
            rows = conn.execute(
                f"""
                SELECT {_QUALIFIED_COLUMNS}
                FROM task_close_reviews r
                JOIN tasks t ON t.id = r.task_id
                WHERE t.project_id = %s AND r.status = 'queued'
                ORDER BY r.created_at, r.id
                LIMIT %s
                FOR UPDATE OF r SKIP LOCKED
                """,  # nosec B608 - static column fragment
                (project_id, slots),
            ).fetchall()
            for row in rows:
                review = _review_from_row(row)
                timeout = review.close_arguments.get("_review_timeout_seconds")
                timeout_seconds = float(timeout) if isinstance(timeout, int | float) else 1200.0
                deadline = datetime.fromtimestamp(now.timestamp() + timeout_seconds, UTC)
                close_arguments = {
                    **review.close_arguments,
                    "_review_deadline_at": deadline.isoformat(),
                }
                promoted = conn.execute(
                    f"""
                    UPDATE task_close_reviews
                    SET status = 'launching', close_arguments = %s::jsonb,
                        launched_at = %s, updated_at = %s
                    WHERE id = %s AND status = 'queued'
                    RETURNING {_COLUMNS}
                    """,  # nosec B608 - static column fragment
                    (_json(close_arguments), now, now, review.id),
                ).fetchone()
                if promoted is not None:
                    claimed.append(_review_from_row(promoted))
        return claimed

    def restore_unlaunched(self, review_id: str, run_id: str, *, error: str) -> bool:
        """Return an interrupted promotion to the durable FIFO queue."""
        now = datetime.now(UTC)
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE task_close_reviews
                SET status = 'queued', launched_at = NULL, error = %s,
                    close_arguments = close_arguments - '_review_deadline_at', updated_at = %s
                WHERE id = %s AND agent_run_id = %s AND status = 'launching'
                  AND EXISTS (
                      SELECT 1 FROM agent_runs
                      WHERE id = %s AND status = 'queued'
                  )
                """,
                (error, now, review_id, run_id, run_id),
            )
        return bool(getattr(cursor, "rowcount", 0))

    def get(self, review_id: str) -> TaskCloseReview | None:
        return self._get("id = %s", (review_id,))

    def get_by_run(self, run_id: str) -> TaskCloseReview | None:
        return self._get("agent_run_id = %s", (run_id,))

    def get_active_for_task(self, task_id: str) -> TaskCloseReview | None:
        return self._get(
            "task_id = %s AND status = ANY(%s)",
            (task_id, list(ACTIVE_TASK_CLOSE_REVIEW_STATUSES)),
        )

    def get_delivered_rejected_verdict(
        self,
        *,
        task_id: str,
        review_fingerprint: str,
        expected_task_updated_at: datetime,
    ) -> TaskCloseReview | None:
        """Return the newest delivered rejection when no active review owns the task."""
        with self.db.transaction() as conn:
            task_row = conn.execute(
                "SELECT updated_at FROM tasks WHERE id = %s FOR UPDATE",
                (task_id,),
            ).fetchone()
            if (
                not isinstance(task_row, Mapping)
                or task_row["updated_at"] != expected_task_updated_at
            ):
                return None
            row = conn.execute(
                f"""
                SELECT {_QUALIFIED_COLUMNS}
                FROM task_close_reviews AS r
                WHERE r.task_id = %s
                  AND r.review_fingerprint = %s
                  AND r.status = 'invalid'
                  AND r.delivered_at IS NOT NULL
                  AND r.result_payload IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM task_close_reviews AS active
                      WHERE active.task_id = r.task_id
                        AND active.status = ANY(%s)
                  )
                ORDER BY r.completed_at DESC NULLS LAST, r.created_at DESC
                LIMIT 1
                """,  # nosec B608 - static column fragment
                (
                    task_id,
                    review_fingerprint,
                    list(ACTIVE_TASK_CLOSE_REVIEW_STATUSES),
                ),
            ).fetchone()
        return _review_from_row(row) if row is not None else None

    def get_active_for_caller_session(self, session_id: str) -> TaskCloseReview | None:
        return self._get(
            "caller_session_id = %s AND status = ANY(%s)",
            (session_id, list(ACTIVE_TASK_CLOSE_REVIEW_STATUSES)),
        )

    def get_latest_agentic_for_task_caller(
        self,
        *,
        task_id: str,
        caller_session_id: str,
    ) -> TaskCloseReview | None:
        """Return the caller's latest delegated close review for a task."""
        with self.db.transaction() as conn:
            row = conn.execute(
                f"""
                SELECT {_COLUMNS}
                FROM task_close_reviews
                WHERE task_id = %s
                  AND caller_session_id = %s
                  AND agent_run_id IS NOT NULL
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,  # nosec B608 - static column fragment
                (task_id, caller_session_id),
            ).fetchone()
        return _review_from_row(row) if row is not None else None

    def count_unjudged_attempts(self, task_id: str) -> int:
        """Count this task's reviews whose reviewer never judged the evidence.

        A reviewer that died instead of answering says nothing about the close,
        so the next attempt has to move along the configured candidate list;
        relaunching onto the same runtime just reproduces the failure. The
        classification cannot be trusted to name the cause — a provider that
        stops mid-launch often leaves ``terminal_reason`` NULL — so every
        unfinished review counts except the ones we ended deliberately.
        """
        with self.db.transaction() as conn:
            row = conn.execute(
                """
                SELECT count(*) AS failures
                FROM task_close_reviews AS review
                JOIN agent_runs AS run ON run.id = review.agent_run_id
                WHERE review.task_id = %s
                  AND review.status = 'error'
                  AND (
                    run.terminal_reason IS NULL
                    OR run.terminal_reason <> ALL(%s)
                  )
                """,
                (task_id, list(DELIBERATE_STOP_TERMINAL_REASONS)),
            ).fetchone()
        return int(row["failures"]) if row is not None else 0

    def bind_run(self, review_id: str, run_id: str) -> TaskCloseReview | None:
        """Bind a successful launch and move the review to running."""
        now = datetime.now(UTC)
        with self.db.transaction() as conn:
            row = conn.execute(
                f"""
                UPDATE task_close_reviews
                SET agent_run_id = %s, status = 'running',
                    launched_at = COALESCE(launched_at, %s), updated_at = %s
                WHERE id = %s AND agent_run_id = %s AND status = 'launching'
                RETURNING {_COLUMNS}
                """,  # nosec B608 - static column fragment
                (run_id, now, now, review_id, run_id),
            ).fetchone()
        return _review_from_row(row) if row is not None else None

    def claim_finalizing(
        self,
        review_id: str,
        run_id: str,
        *,
        verdict: Mapping[str, Any] | None = None,
    ) -> TaskCloseReview | None:
        """Claim verdict finalization, including a late verdict from a successful run.

        The claim clears `delivered_at` and overwrites `result_payload`. Both
        are safe only because the eligibility predicate below admits just
        `running` and the run-ended-without-verdict error, neither of which has
        a delivered terminal payload to lose. Widening that predicate to a
        status whose payload already reached its caller would silently erase it.

        `verdict` records the submission as it arrived, which is forensic only
        and never reapplied: an admissible verdict must be re-derived against
        live fingerprints by a fresh `close_task`. Its value is the
        distinction a restart otherwise erases — a row left `finalizing` with a
        captured verdict means the submit reached the daemon and died inside
        finalization, while one without means it was never sent (#22404).
        """
        now = datetime.now(UTC)
        captured = (
            _json({"kind": SUBMITTED_VERDICT_KIND, "verdict": dict(verdict)})
            if verdict is not None
            else None
        )
        with self.db.transaction() as conn:
            savepoint = conn.savepoint("task_close_review_claim_finalizing")
            try:
                row = conn.execute(
                    f"""
                    UPDATE task_close_reviews
                    SET status = 'finalizing', result_payload = %s::jsonb, error = NULL,
                        completed_at = NULL, delivered_at = NULL, updated_at = %s
                    WHERE id = %s AND agent_run_id = %s
                      AND (
                          status = 'running'
                          OR (status = 'error' AND error = %s)
                      )
                    RETURNING {_COLUMNS}
                    """,  # nosec B608 - static column fragment
                    (captured, now, review_id, run_id, REVIEWER_RUN_ENDED_SUCCESS_ERROR),
                ).fetchone()
            except UniqueViolation as exc:
                savepoint.rollback()
                savepoint.release()
                if exc.diag.constraint_name == _ACTIVE_TASK_CLOSE_REVIEW_CONSTRAINT:
                    return None
                raise
            savepoint.release()
        return _review_from_row(row) if row is not None else None

    def restore_running(self, review_id: str, run_id: str, *, error: str) -> bool:
        """Return a malformed submission to running so the reviewer can correct it."""
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
        return self._finish(
            review_id,
            status=status,
            result_payload=result_payload,
            error=error,
            eligible_statuses=ACTIVE_TASK_CLOSE_REVIEW_STATUSES,
        )

    def finish_run_ended(
        self,
        review_id: str,
        *,
        result_payload: Mapping[str, Any],
        error: str,
    ) -> TaskCloseReview | None:
        """Terminalize an abandoned run without overwriting an in-flight finalization."""
        return self._finish(
            review_id,
            status="error",
            result_payload=result_payload,
            error=error,
            eligible_statuses=("launching", "running"),
        )

    def finish_orphaned_finalizing(
        self,
        review_id: str,
        *,
        expected_updated_at: datetime,
        result_payload: Mapping[str, Any],
        error: str,
    ) -> TaskCloseReview | None:
        """Release a `finalizing` review whose submission was abandoned.

        Callers must first prove the row is orphaned. `finish_run_ended`
        excludes `finalizing` because `claim_finalizing` holds that status
        across `commit_close`'s git subprocesses, so the status is reachable
        while a real submission is still running, and a reconciler write would
        contradict that caller. Without this the row holds
        `uq_task_close_reviews_active_task` forever and every later
        `close_task` for its task returns `agentic_review_pending`.

        Daemon startup is one such proof: no in-process submit survives a
        restart. On a running daemon the proof is `is_finalizing_in_process`
        plus `FINALIZING_ORPHAN_GRACE_SECONDS` against `updated_at`, scoped to
        rows whose reviewer run belongs to this machine.

        The compare-and-swap on `updated_at` makes a lost race a clean no-op:
        `None` means some other transition already moved the row.
        """
        now = datetime.now(UTC)
        with self.db.transaction() as conn:
            row = conn.execute(
                f"""
                UPDATE task_close_reviews
                SET status = 'error', result_payload = %s::jsonb, error = %s,
                    completed_at = %s, updated_at = %s
                WHERE id = %s AND status = 'finalizing' AND updated_at = %s
                RETURNING {_COLUMNS}
                """,  # nosec B608 - static column fragment
                (
                    _json(result_payload),
                    error,
                    now,
                    now,
                    review_id,
                    expected_updated_at,
                ),
            ).fetchone()
        return _review_from_row(row) if row is not None else None

    def _finish(
        self,
        review_id: str,
        *,
        status: TerminalTaskCloseReviewStatus,
        result_payload: Mapping[str, Any],
        error: str | None,
        eligible_statuses: tuple[ActiveTaskCloseReviewStatus, ...],
    ) -> TaskCloseReview | None:
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
                    list(eligible_statuses),
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
        diff_sha=str(row["diff_sha"]) if row["diff_sha"] is not None else None,
        test_bodies_sha=(
            str(row["test_bodies_sha"]) if row["test_bodies_sha"] is not None else None
        ),
        stable_facts=_json_object(row["stable_facts"]),
    )


__all__ = [
    "ACTIVE_TASK_CLOSE_REVIEW_STATUSES",
    "FINALIZING_ORPHAN_GRACE_SECONDS",
    "QueuedAgentRunSpec",
    "SUBMITTED_VERDICT_KIND",
    "TERMINAL_TASK_CLOSE_REVIEW_STATUSES",
    "ActiveTaskCloseReviewStatus",
    "TaskCloseReview",
    "TaskCloseReviewStatus",
    "TaskCloseReviewStore",
    "TerminalTaskCloseReviewStatus",
    "REVIEWER_RUN_ENDED_SUCCESS_ERROR",
    "finalizing_in_process",
    "is_finalizing_in_process",
]

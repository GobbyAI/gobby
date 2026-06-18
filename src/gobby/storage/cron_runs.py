"""Cron run storage methods."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from gobby.storage.cron_children import (
    active_children_for_job as project_active_children_for_job,
)
from gobby.storage.cron_children import (
    hydrate_run_children,
)
from gobby.storage.cron_children import (
    reconcile_interrupted_runs as reconcile_interrupted_cron_runs,
)
from gobby.storage.cron_constants import MIN_CRON_INTERVAL_SECONDS
from gobby.storage.cron_models import CronRun
from gobby.storage.hub.protocol import CronRunAdmission, HubDatabase
from gobby.utils.id import generate_prefixed_id

logger = logging.getLogger(__name__)


class CronRunStorageMixin:
    """Cron run persistence methods mixed into CronJobStorage."""

    db: HubDatabase

    def create_run(self, cron_job_id: str) -> CronRun | None:
        """Create a cron run unless this job already has pending/running work."""
        run_id = generate_prefixed_id("cr", length=12)
        now = datetime.now(UTC).isoformat()

        candidate = CronRun(
            id=run_id,
            cron_job_id=cron_job_id,
            triggered_at=now,
            created_at=now,
        )

        row = self.db.fetchone(
            """
            INSERT INTO cron_runs (
                id, cron_job_id, triggered_at, started_at, completed_at,
                status, output, error, agent_run_id,
                pipeline_execution_id, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (cron_job_id) WHERE status IN ('pending', 'running')
            DO NOTHING
            RETURNING *
            """,
            (
                candidate.id,
                candidate.cron_job_id,
                candidate.triggered_at,
                candidate.started_at,
                candidate.completed_at,
                candidate.status,
                candidate.output,
                candidate.error,
                candidate.agent_run_id,
                candidate.pipeline_execution_id,
                candidate.created_at,
            ),
        )
        if row is None:
            return None
        return self._hydrate_run(CronRun.from_row(row))

    def create_run_if_admitted(
        self,
        cron_job_id: str,
        *,
        max_concurrent_jobs: int,
    ) -> tuple[CronRun | None, int]:
        """Create a cron run after atomically checking global active-run capacity."""
        if max_concurrent_jobs < 1:
            raise ValueError("max_concurrent_jobs must be positive")

        run_id = generate_prefixed_id("cr", length=12)
        now = datetime.now(UTC).isoformat()

        candidate = CronRun(
            id=run_id,
            cron_job_id=cron_job_id,
            triggered_at=now,
            created_at=now,
        )

        with self.db.transaction_immediate(lock=CronRunAdmission()) as conn:
            count_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM cron_runs WHERE status IN ('pending', 'running')"
            ).fetchone()
            active_count = int(count_row["cnt"]) if count_row else 0
            if active_count >= max_concurrent_jobs:
                return None, active_count

            row = conn.execute(
                """
                INSERT INTO cron_runs (
                    id, cron_job_id, triggered_at, started_at, completed_at,
                    status, output, error, agent_run_id,
                    pipeline_execution_id, created_at
                )
                SELECT %s, id, %s, %s, %s, %s, %s, %s, %s, %s, %s
                  FROM cron_jobs
                 WHERE id = %s
                ON CONFLICT (cron_job_id) WHERE status IN ('pending', 'running')
                DO NOTHING
                RETURNING *
                """,
                (
                    candidate.id,
                    candidate.triggered_at,
                    candidate.started_at,
                    candidate.completed_at,
                    candidate.status,
                    candidate.output,
                    candidate.error,
                    candidate.agent_run_id,
                    candidate.pipeline_execution_id,
                    candidate.created_at,
                    candidate.cron_job_id,
                ),
            ).fetchone()

        if row is None:
            return None, active_count
        return self._hydrate_run(CronRun.from_row(row)), active_count

    def update_run(self, run_id: str, **fields: Any) -> CronRun | None:
        """Update a cron run's fields."""
        valid_fields = frozenset(
            {
                "started_at",
                "completed_at",
                "status",
                "output",
                "error",
                "agent_run_id",
                "pipeline_execution_id",
            }
        )

        invalid = set(fields.keys()) - valid_fields
        if invalid:
            raise ValueError(f"Invalid run field names: {invalid}")

        if not fields:
            return self.get_run(run_id)

        set_clause = ", ".join(f"{key} = %s" for key in fields.keys())
        values = list(fields.values()) + [run_id]

        self.db.execute(
            f"UPDATE cron_runs SET {set_clause} WHERE id = %s",  # nosec B608
            tuple(values),
        )

        return self.get_run(run_id)

    def get_run(self, run_id: str) -> CronRun | None:
        """Get a cron run by ID."""
        row = self.db.fetchone("SELECT * FROM cron_runs WHERE id = %s", (run_id,))
        if not row:
            return None
        return self._hydrate_run(CronRun.from_row(row))

    def list_runs(self, cron_job_id: str, limit: int = 20) -> list[CronRun]:
        """List runs for a cron job, most recent first."""
        rows = self.db.fetchall(
            """
            SELECT * FROM cron_runs
            WHERE cron_job_id = %s
            ORDER BY triggered_at DESC
            LIMIT %s
            """,
            (cron_job_id, limit),
        )
        return hydrate_run_children(self.db, [CronRun.from_row(row) for row in rows])

    def count_running(self) -> int:
        """Count cron-owned slots currently consumed by pending/running runs."""
        row = self.db.fetchone(
            "SELECT COUNT(*) as cnt FROM cron_runs WHERE status IN ('pending', 'running')"
        )
        return row["cnt"] if row else 0

    def has_running_run(self, cron_job_id: str) -> bool:
        """Return whether a cron job already has pending/running cron-owned work."""
        row = self.db.fetchone(
            """
            SELECT 1
              FROM cron_runs
             WHERE cron_job_id = %s
               AND status IN ('pending', 'running')
             LIMIT 1
            """,
            (cron_job_id,),
        )
        return row is not None

    def active_children_for_job(self, job_id: str, action_type: str) -> list[dict[str, Any]]:
        """Return active dispatched child work for a job/action pair."""
        children = project_active_children_for_job(self.db, job_id, action_type)
        return [child.to_dict() for child in children]

    def reconcile_interrupted_runs(self) -> dict[str, int]:
        """Normalize active cron rows left behind by a previous scheduler process."""
        return reconcile_interrupted_cron_runs(self.db)

    def _hydrate_run(self, run: CronRun) -> CronRun:
        return hydrate_run_children(self.db, [run])[0]

    def fail_stale_running_runs(self, timeout_seconds: int) -> int:
        """Mark stale running cron runs failed so they stop consuming scheduler slots."""
        timeout_seconds = max(timeout_seconds, MIN_CRON_INTERVAL_SECONDS)
        now = datetime.now(UTC)
        cutoff = (now - timedelta(seconds=timeout_seconds)).isoformat()
        cursor = self.db.execute(
            """
            UPDATE cron_runs
               SET status = 'failed',
                   completed_at = %s,
                   error = %s
             WHERE status = 'running'
               AND COALESCE(started_at, triggered_at, created_at) < %s
            """,
            (
                now.isoformat(),
                f"Cron run exceeded running timeout ({timeout_seconds}s)",
                cutoff,
            ),
        )
        return cursor.rowcount

    def fail_running_runs(self, error: str) -> int:
        """Mark all currently running cron runs failed.

        This is used when a scheduler process starts. In-process cron tasks do not
        survive daemon restart, so any persisted running row at scheduler startup is
        orphaned and must not keep consuming concurrency slots.
        """
        now = datetime.now(UTC).isoformat()
        cursor = self.db.execute(
            """
            UPDATE cron_runs
               SET status = 'failed',
                   completed_at = %s,
                   error = %s
             WHERE status = 'running'
            """,
            (now, error[:5000]),
        )
        return cursor.rowcount

    def fail_pending_runs(self, error: str) -> int:
        """Mark pending cron runs failed.

        Pending rows from a previous daemon cannot be safely replayed because they
        only prove stale user intent, not a currently owned execution.
        """
        now = datetime.now(UTC).isoformat()
        cursor = self.db.execute(
            """
            UPDATE cron_runs
               SET status = 'failed',
                   completed_at = %s,
                   error = %s
             WHERE status = 'pending'
            """,
            (now, error[:5000]),
        )
        return cursor.rowcount

    def cleanup_old_runs(self, days: int) -> int:
        """Delete runs older than the given number of days."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        cursor = self.db.execute(
            "DELETE FROM cron_runs WHERE created_at < %s",
            (cutoff,),
        )
        deleted = cursor.rowcount
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} cron runs older than {days} days")
        return deleted

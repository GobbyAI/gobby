"""Cron run storage methods."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Collection
from datetime import timedelta
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
from gobby.storage.cron_models import CronRun
from gobby.storage.hub.protocol import CronRunAdmission, HubDatabase
from gobby.utils.datetime import utc_now
from gobby.utils.machine_id import get_machine_id

logger = logging.getLogger(__name__)


class CronRunStorageMixin:
    """Cron run persistence methods mixed into CronJobStorage."""

    db: HubDatabase

    def create_run(
        self,
        cron_job_id: str,
        *,
        scheduler_owner: str | None = None,
        start_immediately: bool = False,
    ) -> CronRun | None:
        """Create a cron run unless this job already has pending/running work."""
        run_id = str(uuid.uuid4())
        now = utc_now()
        machine_id = get_machine_id()
        if machine_id is None:
            raise RuntimeError("Local machine identity is required to create a cron run")

        candidate = CronRun(
            id=run_id,
            cron_job_id=cron_job_id,
            machine_id=machine_id,
            triggered_at=now,
            created_at=now,
            started_at=now if start_immediately else None,
            status="running" if start_immediately else "pending",
        )

        row = self.db.fetchone(
            """
            INSERT INTO cron_runs (
                id, cron_job_id, machine_id, triggered_at, started_at, completed_at,
                status, output, error, agent_run_id,
                pipeline_execution_id, scheduler_owner
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (cron_job_id) WHERE status IN ('pending', 'running')
            DO NOTHING
            RETURNING *
            """,
            (
                candidate.id,
                candidate.cron_job_id,
                candidate.machine_id,
                now,
                candidate.started_at,
                candidate.completed_at,
                candidate.status,
                candidate.output,
                candidate.error,
                candidate.agent_run_id,
                candidate.pipeline_execution_id,
                scheduler_owner,
            ),
        )
        if row is None:
            return None
        return self._hydrate_run(CronRun.from_row(row))

    def create_run_if_admitted(
        self,
        cron_job_id: str,
        *,
        machine_id: str,
        max_concurrent_jobs: int,
        scheduler_owner: str | None = None,
    ) -> tuple[CronRun | None, int, bool]:
        """Create a run after atomic admission.

        Returns the run, the pre-insert active count, and whether the target job
        already had active work.
        """
        if max_concurrent_jobs < 1:
            raise ValueError("max_concurrent_jobs must be positive")

        run_id = str(uuid.uuid4())
        now = utc_now()
        candidate = CronRun(
            id=run_id,
            cron_job_id=cron_job_id,
            machine_id=machine_id,
            triggered_at=now,
            created_at=now,
        )

        with self.db.transaction_immediate(lock=CronRunAdmission()) as conn:
            count_row = conn.execute(
                """
                SELECT COUNT(*) as cnt
                  FROM cron_runs
                 WHERE status IN ('pending', 'running')
                   AND machine_id = %s
                """,
                (machine_id,),
            ).fetchone()
            active_count = int(count_row["cnt"]) if count_row else 0
            active_job_row = conn.execute(
                """
                SELECT 1
                  FROM cron_runs
                 WHERE cron_job_id = %s
                   AND status IN ('pending', 'running')
                 LIMIT 1
                """,
                (cron_job_id,),
            ).fetchone()
            if active_job_row is not None:
                return None, active_count, True
            if active_count >= max_concurrent_jobs:
                return None, active_count, False

            row = conn.execute(
                """
                INSERT INTO cron_runs (
                    id, cron_job_id, machine_id, triggered_at, started_at, completed_at,
                    status, output, error, agent_run_id,
                    pipeline_execution_id, scheduler_owner
                )
                SELECT %s, id, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                  FROM cron_jobs
                 WHERE id = %s
                ON CONFLICT (cron_job_id) WHERE status IN ('pending', 'running')
                DO NOTHING
                RETURNING *
                """,
                (
                    candidate.id,
                    candidate.machine_id,
                    now,
                    candidate.started_at,
                    candidate.completed_at,
                    candidate.status,
                    candidate.output,
                    candidate.error,
                    candidate.agent_run_id,
                    candidate.pipeline_execution_id,
                    scheduler_owner,
                    candidate.cron_job_id,
                ),
            ).fetchone()

        if row is None:
            return None, active_count, True
        return self._hydrate_run(CronRun.from_row(row)), active_count, False

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

    def count_running(self, machine_id: str) -> int:
        """Count active cron slots owned by one machine."""
        row = self.db.fetchone(
            """
            SELECT COUNT(*) as cnt
              FROM cron_runs
             WHERE status IN ('pending', 'running')
               AND machine_id = %s
            """,
            (machine_id,),
        )
        return row["cnt"] if row else 0

    def fail_stale_running_runs(
        self,
        timeout_seconds: int,
        *,
        machine_id: str,
        exclude_run_ids: Collection[str] | None = None,
    ) -> int:
        """Fail running cron rows older than the configured execution timeout."""
        if (
            not isinstance(timeout_seconds, int)
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive integer")

        now = utc_now()
        cutoff = now - timedelta(seconds=timeout_seconds)
        excluded = list(dict.fromkeys(exclude_run_ids or ()))
        cursor = self.db.execute(
            """
            UPDATE cron_runs
               SET status = 'failed',
                   completed_at = %s,
                   error = %s
             WHERE status = 'running'
               AND COALESCE(started_at, triggered_at, created_at) < %s
               AND machine_id = %s
               AND NOT (id = ANY(%s::uuid[]))
            """,
            (
                now,
                f"Cron run exceeded running timeout ({timeout_seconds}s)",
                cutoff,
                machine_id,
                excluded,
            ),
        )
        return cursor.rowcount

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

    def list_active_runs(self, *, scheduler_owner: str | None = None) -> list[CronRun]:
        """Return all pending/running cron runs across jobs."""
        owner_clause = ""
        params: tuple[str, ...] = ()
        if scheduler_owner is not None:
            owner_clause = "AND scheduler_owner = %s"
            params = (scheduler_owner,)
        rows = self.db.fetchall(
            f"""
            SELECT * FROM cron_runs
             WHERE status IN ('pending', 'running')
               {owner_clause}
             ORDER BY created_at ASC
            """,  # nosec B608
            params,
        )
        return hydrate_run_children(self.db, [CronRun.from_row(row) for row in rows])

    def active_children_for_job(self, job_id: str, action_type: str) -> list[dict[str, Any]]:
        """Return active dispatched child work for a job/action pair."""
        children = project_active_children_for_job(self.db, job_id, action_type)
        return [child.to_dict() for child in children]

    def reconcile_interrupted_runs(self, machine_id: str) -> dict[str, int]:
        """Normalize active cron rows left behind by this machine's scheduler."""
        return reconcile_interrupted_cron_runs(self.db, machine_id)

    def _hydrate_run(self, run: CronRun) -> CronRun:
        return hydrate_run_children(self.db, [run])[0]

    def fail_run_if_active(self, run_id: str, error: str) -> bool:
        """Atomically fail a run only while it is still pending/running.

        Returns True when this call transitioned the run to failed; False when
        the run was already terminal (or does not exist), leaving it untouched.
        """
        now = utc_now()
        row = self.db.fetchone(
            """
            UPDATE cron_runs
               SET status = 'failed',
                   completed_at = %s,
                   error = %s
             WHERE id = %s
               AND status IN ('pending', 'running')
            RETURNING id
            """,
            (now, error, run_id),
        )
        return row is not None

    def cleanup_old_runs(self, days: int) -> int:
        """Delete runs older than the given number of days."""
        if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
            raise ValueError("days must be a positive integer")

        cutoff = utc_now() - timedelta(days=days)
        cursor = self.db.execute(
            """
            DELETE FROM cron_runs
            WHERE created_at < %s
              AND status NOT IN ('pending', 'running')
            """,
            (cutoff,),
        )
        deleted = cursor.rowcount
        if deleted > 0:
            logger.info("Cleaned up %s non-active cron runs older than %s days", deleted, days)
        return deleted

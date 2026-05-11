"""Cron job storage manager.

Provides CRUD operations for cron jobs and their execution runs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from gobby.storage.cron_models import CronJob, CronRun
from gobby.storage.database import DatabaseProtocol
from gobby.utils.id import generate_prefixed_id

logger = logging.getLogger(__name__)

MIN_CRON_INTERVAL_SECONDS = 60
CRON_JOB_NAME_PRIORITIES = {
    "gobby:pipeline-heartbeat": 0,
    "gobby:dispatcher": 1,
}
DEFAULT_CRON_JOB_PRIORITY = 2


SYSTEM_ROW_UPDATE_ALLOWED_FIELDS = frozenset(
    {
        "enabled",
        "schedule_type",
        "cron_expr",
        "interval_seconds",
        "run_at",
        "timezone",
    }
)


class SystemRowProtected(ValueError):
    """Raised when operator-facing code tries to mutate a system cron row."""


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_interval_seconds(schedule_type: str, interval_seconds: int | None) -> int | None:
    if schedule_type != "interval" or interval_seconds is None:
        return interval_seconds
    return max(interval_seconds, MIN_CRON_INTERVAL_SECONDS)


def _cron_job_priority(job: CronJob) -> int:
    return CRON_JOB_NAME_PRIORITIES.get(job.name, DEFAULT_CRON_JOB_PRIORITY)


def compute_next_run(job: CronJob) -> datetime | None:
    """Compute the next run time for a cron job.

    Args:
        job: CronJob instance

    Returns:
        Next run datetime (UTC) or None if job is disabled or expired one-shot.
    """
    if not job.enabled:
        return None

    try:
        tz = ZoneInfo(job.timezone) if job.timezone else ZoneInfo("UTC")
    except ZoneInfoNotFoundError:
        logger.warning(f"Invalid timezone {job.timezone!r} for job {job.id}, falling back to UTC")
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)

    if job.schedule_type == "cron":
        if not job.cron_expr:
            return None
        try:
            cron = croniter(job.cron_expr, now)
            next_dt: datetime = cron.get_next(datetime)
            return next_dt.astimezone(ZoneInfo("UTC"))
        except (ValueError, KeyError):
            # Invalid cron expression
            return None

    elif job.schedule_type == "interval":
        if not job.interval_seconds:
            return None
        interval_seconds = max(job.interval_seconds, MIN_CRON_INTERVAL_SECONDS)
        # Always compute from now to prevent double-fire when last_run_at
        # is stale (close to current time after execution).
        next_interval: datetime = now + timedelta(seconds=interval_seconds)
        return next_interval.astimezone(ZoneInfo("UTC"))

    elif job.schedule_type == "once":
        if not job.run_at:
            logger.debug(f"Job {job.id}: schedule_type='once' but run_at is missing")
            return None
        try:
            run_at = datetime.fromisoformat(job.run_at)
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=tz)
            run_at_utc = run_at.astimezone(ZoneInfo("UTC"))
            # Expired one-shot
            now_utc = datetime.now(ZoneInfo("UTC"))
            if run_at_utc <= now_utc:
                logger.debug(
                    f"Job {job.id}: one-shot run_at {run_at_utc} is in the past (now={now_utc})"
                )
                return None
            return run_at_utc
        except (ValueError, TypeError) as e:
            logger.warning(f"Job {job.id}: invalid run_at format '{job.run_at}': {e}")
            return None

    return None


class CronJobStorage:
    """Manager for cron job storage."""

    def __init__(self, db: DatabaseProtocol):
        self.db = db

    def create_job(
        self,
        project_id: str,
        name: str,
        schedule_type: Literal["cron", "interval", "once"],
        action_type: Literal["agent_spawn", "pipeline", "shell", "handler", "dispatcher"],
        action_config: dict[str, Any],
        description: str | None = None,
        cron_expr: str | None = None,
        interval_seconds: int | None = None,
        run_at: str | None = None,
        timezone: str = "UTC",
        enabled: bool = True,
        is_system: bool = False,
    ) -> CronJob:
        """Create a new cron job."""
        job_id = generate_prefixed_id("cj", length=12)
        now = datetime.now(UTC).isoformat()

        interval_seconds = _normalize_interval_seconds(schedule_type, interval_seconds)

        job = CronJob(
            id=job_id,
            project_id=project_id,
            name=name,
            schedule_type=schedule_type,
            action_type=action_type,
            action_config=action_config,
            created_at=now,
            updated_at=now,
            description=description,
            cron_expr=cron_expr,
            interval_seconds=interval_seconds,
            run_at=run_at,
            timezone=timezone,
            enabled=enabled,
            is_system=is_system,
        )

        # Compute initial next_run_at
        next_run = compute_next_run(job)
        if next_run:
            job.next_run_at = next_run.isoformat()

        self.db.execute(
            """
            INSERT INTO cron_jobs (
                id, project_id, name, description, schedule_type,
                cron_expr, interval_seconds, run_at, timezone,
                action_type, action_config, enabled, is_system, next_run_at,
                last_run_at, last_status, consecutive_failures,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.id,
                job.project_id,
                job.name,
                job.description,
                job.schedule_type,
                job.cron_expr,
                job.interval_seconds,
                job.run_at,
                job.timezone,
                job.action_type,
                json.dumps(job.action_config),
                1 if job.enabled else 0,
                1 if job.is_system else 0,
                job.next_run_at,
                job.last_run_at,
                job.last_status,
                job.consecutive_failures,
                job.created_at,
                job.updated_at,
            ),
        )

        return job

    def get_job(self, job_id: str) -> CronJob | None:
        """Get a cron job by ID."""
        row = self.db.fetchone("SELECT * FROM cron_jobs WHERE id = ?", (job_id,))
        return CronJob.from_row(row) if row else None

    def get_job_by_name(self, name: str) -> CronJob | None:
        """Get a cron job by name.

        Args:
            name: Job name (e.g., "gobby:pipeline-heartbeat")

        Returns:
            CronJob or None if not found
        """
        row = self.db.fetchone("SELECT * FROM cron_jobs WHERE name = ?", (name,))
        return CronJob.from_row(row) if row else None

    def mark_as_system_job(self, job_id: str) -> None:
        """Mark an existing cron row as gobby-managed system infrastructure."""
        self.db.execute("UPDATE cron_jobs SET is_system = 1 WHERE id = ?", (job_id,))

    def list_jobs(
        self,
        project_id: str | None = None,
        enabled: bool | None = None,
        is_system: bool | None = None,
        limit: int = 50,
    ) -> list[CronJob]:
        """List cron jobs with optional filters."""
        conditions: list[str] = []
        params: list[Any] = []

        if project_id:
            conditions.append("project_id = ?")
            params.append(project_id)
        if enabled is not None:
            conditions.append("enabled = ?")
            params.append(1 if enabled else 0)
        if is_system is not None:
            conditions.append("is_system = ?")
            params.append(1 if is_system else 0)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)

        rows = self.db.fetchall(
            f"""
            SELECT * FROM cron_jobs
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT ?
            """,  # nosec B608
            tuple(params),
        )
        return [CronJob.from_row(row) for row in rows]

    _VALID_UPDATE_FIELDS = frozenset(
        {
            "name",
            "description",
            "schedule_type",
            "cron_expr",
            "interval_seconds",
            "run_at",
            "timezone",
            "action_type",
            "action_config",
            "enabled",
            "next_run_at",
            "last_run_at",
            "last_status",
            "consecutive_failures",
            "updated_at",
        }
    )
    _SYSTEM_BOOKKEEPING_FIELDS = frozenset(
        {
            "next_run_at",
            "last_run_at",
            "last_status",
            "consecutive_failures",
        }
    )

    def _update_job_fields(self, job_id: str, **fields: Any) -> CronJob | None:
        """Update trusted cron row fields without the public operator policy."""
        if not fields:
            return self.get_job(job_id)

        invalid_fields = set(fields.keys()) - self._VALID_UPDATE_FIELDS
        if invalid_fields:
            raise ValueError(f"Invalid field names: {invalid_fields}")

        if "action_config" in fields and isinstance(fields["action_config"], dict):
            fields["action_config"] = json.dumps(fields["action_config"])

        set_clause = ", ".join(f"{key} = ?" for key in fields.keys())
        values = list(fields.values()) + [job_id]
        self.db.execute(
            f"UPDATE cron_jobs SET {set_clause} WHERE id = ?",  # nosec B608
            tuple(values),
        )

        return self.get_job(job_id)

    def _normalize_update_fields(self, job: CronJob, fields: dict[str, Any]) -> None:
        schedule_type = str(fields.get("schedule_type", job.schedule_type))
        should_normalize = schedule_type == "interval" and (
            "schedule_type" in fields
            or "interval_seconds" in fields
            or (
                job.schedule_type == "interval"
                and job.interval_seconds is not None
                and job.interval_seconds < MIN_CRON_INTERVAL_SECONDS
            )
        )
        if not should_normalize:
            return
        interval_seconds = fields.get("interval_seconds", job.interval_seconds)
        fields["interval_seconds"] = _normalize_interval_seconds(
            schedule_type,
            interval_seconds,
        )

    def update_job(self, job_id: str, **fields: Any) -> CronJob | None:
        """Update cron job fields."""
        if not fields:
            return self.get_job(job_id)

        invalid_fields = set(fields.keys()) - self._VALID_UPDATE_FIELDS
        if invalid_fields:
            raise ValueError(f"Invalid field names: {invalid_fields}")

        job = self.get_job(job_id)
        if job is None:
            return None
        if job.is_system:
            disallowed_fields = set(fields.keys()) - SYSTEM_ROW_UPDATE_ALLOWED_FIELDS
            if disallowed_fields:
                field = sorted(disallowed_fields)[0]
                raise SystemRowProtected(
                    f"Cron row {job_id} is a gobby-managed system-managed row; "
                    "operator update rejected "
                    f"for field {field!r}. Use update_system_job_bookkeeping for "
                    "scheduler state or reconcile_system_job_definition for bundled "
                    "action repair."
                )

        self._normalize_update_fields(job, fields)
        fields["updated_at"] = _utc_now_iso()

        return self._update_job_fields(job_id, **fields)

    def update_system_job_bookkeeping(
        self,
        job_id: str,
        *,
        next_run_at: object = UNSET,
        last_run_at: object = UNSET,
        last_status: object = UNSET,
        consecutive_failures: object = UNSET,
        **invalid_fields: object,
    ) -> CronJob | None:
        """Update scheduler-owned fields on a system cron job."""
        job = self.get_job(job_id)
        if job is None:
            return None
        if not job.is_system:
            raise SystemRowProtected(
                f"Cron row {job_id} is non-system; update_system_job_bookkeeping "
                "is reserved for gobby-managed system cron rows."
            )

        if invalid_fields:
            field = sorted(invalid_fields)[0]
            raise SystemRowProtected(
                f"Cron row {job_id} is a gobby-managed system-managed row; "
                "system bookkeeping update "
                f"rejected field {field!r}. Only scheduler state fields are allowed."
            )

        fields = {
            "next_run_at": next_run_at,
            "last_run_at": last_run_at,
            "last_status": last_status,
            "consecutive_failures": consecutive_failures,
        }
        update_fields = {key: value for key, value in fields.items() if value is not UNSET}

        return self._update_job_fields(job_id, **update_fields)

    def park_system_job(self, job_id: str) -> CronJob | None:
        """Park an enabled system cron row by clearing its next scheduled run."""
        job = self.get_job(job_id)
        if job is None:
            return None
        if not job.is_system:
            raise SystemRowProtected(
                f"Cron row {job_id} is non-system; park_system_job "
                "is reserved for gobby-managed system cron rows."
            )
        return self.update_system_job_bookkeeping(job_id, next_run_at=None)

    def wake_system_job(self, job_id: str) -> CronJob | None:
        """Wake an enabled system cron row by recomputing its next scheduled run."""
        job = self.get_job(job_id)
        if job is None:
            return None
        if not job.is_system:
            raise SystemRowProtected(
                f"Cron row {job_id} is non-system; wake_system_job "
                "is reserved for gobby-managed system cron rows."
            )
        if not job.enabled:
            return job
        next_run = compute_next_run(job)
        return self.update_system_job_bookkeeping(
            job_id,
            next_run_at=next_run.isoformat() if next_run else None,
        )

    def reconcile_system_job_definition(
        self,
        job_id: str,
        *,
        action_type: Literal["agent_spawn", "pipeline", "shell", "handler", "dispatcher"],
        action_config: dict[str, Any],
        description: str | None | _Unset = UNSET,
        schedule_type: Literal["cron", "interval", "once"] | _Unset = UNSET,
        cron_expr: str | None | _Unset = UNSET,
        interval_seconds: int | None | _Unset = UNSET,
        run_at: str | None | _Unset = UNSET,
        timezone: str | _Unset = UNSET,
    ) -> CronJob | None:
        """Repair bundled definition fields on an existing system cron job."""
        job = self.get_job(job_id)
        if job is None:
            return None
        if not job.is_system:
            raise SystemRowProtected(
                f"Cron row {job_id} is non-system; reconcile_system_job_definition "
                "is reserved for gobby-managed system cron rows."
            )

        fields: dict[str, Any] = {}
        desired: dict[str, Any] = {
            "action_type": action_type,
            "action_config": action_config,
        }
        optional = {
            "description": description,
            "schedule_type": schedule_type,
            "cron_expr": cron_expr,
            "interval_seconds": interval_seconds,
            "run_at": run_at,
            "timezone": timezone,
        }
        desired.update({key: value for key, value in optional.items() if value is not UNSET})
        self._normalize_update_fields(job, desired)
        for key, value in desired.items():
            if getattr(job, key) != value:
                fields[key] = value

        if not fields:
            return job

        candidate = replace(job, **fields)
        next_run = compute_next_run(candidate) if candidate.enabled else None
        fields["next_run_at"] = next_run.isoformat() if next_run else None
        fields["updated_at"] = _utc_now_iso()
        return self._update_job_fields(job_id, **fields)

    def delete_job(self, job_id: str) -> bool:
        """Delete a cron job and its runs."""
        job = self.get_job(job_id)
        if job is not None and job.is_system:
            raise SystemRowProtected(
                f"Cron row {job_id} is a gobby-managed system-managed row; "
                "operator delete rejected."
            )

        with self.db.transaction() as conn:
            # Delete runs first (foreign key)
            conn.execute("DELETE FROM cron_runs WHERE cron_job_id = ?", (job_id,))
            cursor = conn.execute("DELETE FROM cron_jobs WHERE id = ?", (job_id,))
        return cursor.rowcount > 0

    def toggle_job(self, job_id: str) -> CronJob | None:
        """Toggle a cron job's enabled state."""
        job = self.get_job(job_id)
        if not job:
            return None

        new_enabled = not job.enabled
        updates: dict[str, Any] = {"enabled": 1 if new_enabled else 0}

        # Recompute next_run when enabling
        if new_enabled:
            from dataclasses import replace

            enabled_job = replace(job, enabled=True)
            next_run = compute_next_run(enabled_job)
            updates["next_run_at"] = next_run.isoformat() if next_run else None
        else:
            updates["next_run_at"] = None

        if job.is_system:
            updated = self._update_job_fields(
                job_id,
                enabled=updates["enabled"],
                updated_at=_utc_now_iso(),
            )
            if updated is None:
                return None
            return self.update_system_job_bookkeeping(
                job_id,
                next_run_at=updates["next_run_at"],
            )

        return self.update_job(job_id, **updates)

    def get_due_jobs(self) -> list[CronJob]:
        """Get enabled jobs whose next_run_at has passed."""
        now = datetime.now(UTC).isoformat()
        rows = self.db.fetchall(
            """
            SELECT * FROM cron_jobs
            WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
            ORDER BY next_run_at ASC, created_at ASC
            """,
            (now,),
        )
        jobs = [CronJob.from_row(row) for row in rows]
        return sorted(
            jobs,
            key=lambda job: (
                _cron_job_priority(job),
                job.next_run_at or "",
                job.created_at,
                job.id,
            ),
        )

    # --- CronRun methods ---

    def create_run(self, cron_job_id: str) -> CronRun:
        """Create a new cron run record."""
        run_id = generate_prefixed_id("cr", length=12)
        now = datetime.now(UTC).isoformat()

        run = CronRun(
            id=run_id,
            cron_job_id=cron_job_id,
            triggered_at=now,
            created_at=now,
        )

        self.db.execute(
            """
            INSERT INTO cron_runs (
                id, cron_job_id, triggered_at, started_at, completed_at,
                status, output, error, agent_run_id,
                pipeline_execution_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.id,
                run.cron_job_id,
                run.triggered_at,
                run.started_at,
                run.completed_at,
                run.status,
                run.output,
                run.error,
                run.agent_run_id,
                run.pipeline_execution_id,
                run.created_at,
            ),
        )

        return run

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

        set_clause = ", ".join(f"{key} = ?" for key in fields.keys())
        values = list(fields.values()) + [run_id]

        self.db.execute(
            f"UPDATE cron_runs SET {set_clause} WHERE id = ?",  # nosec B608
            tuple(values),
        )

        return self.get_run(run_id)

    def get_run(self, run_id: str) -> CronRun | None:
        """Get a cron run by ID."""
        row = self.db.fetchone("SELECT * FROM cron_runs WHERE id = ?", (run_id,))
        return CronRun.from_row(row) if row else None

    def list_runs(self, cron_job_id: str, limit: int = 20) -> list[CronRun]:
        """List runs for a cron job, most recent first."""
        rows = self.db.fetchall(
            """
            SELECT * FROM cron_runs
            WHERE cron_job_id = ?
            ORDER BY triggered_at DESC
            LIMIT ?
            """,
            (cron_job_id, limit),
        )
        return [CronRun.from_row(row) for row in rows]

    def count_running(self) -> int:
        """Count currently running cron runs."""
        row = self.db.fetchone("SELECT COUNT(*) as cnt FROM cron_runs WHERE status = 'running'")
        return row["cnt"] if row else 0

    def has_running_run(self, cron_job_id: str) -> bool:
        """Return whether a cron job already has an active run."""
        row = self.db.fetchone(
            """
            SELECT 1
              FROM cron_runs
             WHERE cron_job_id = ?
               AND status = 'running'
             LIMIT 1
            """,
            (cron_job_id,),
        )
        return row is not None

    def fail_stale_running_runs(self, timeout_seconds: int) -> int:
        """Mark stale running cron runs failed so they stop consuming scheduler slots."""
        timeout_seconds = max(timeout_seconds, MIN_CRON_INTERVAL_SECONDS)
        now = datetime.now(UTC)
        cutoff = (now - timedelta(seconds=timeout_seconds)).isoformat()
        cursor = self.db.execute(
            """
            UPDATE cron_runs
               SET status = 'failed',
                   completed_at = ?,
                   error = ?
             WHERE status = 'running'
               AND COALESCE(started_at, triggered_at, created_at) < ?
            """,
            (
                now.isoformat(),
                f"Cron run exceeded running timeout ({timeout_seconds}s)",
                cutoff,
            ),
        )
        return cursor.rowcount

    def cleanup_old_runs(self, days: int) -> int:
        """Delete runs older than the given number of days."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        cursor = self.db.execute(
            "DELETE FROM cron_runs WHERE created_at < ?",
            (cutoff,),
        )
        deleted = cursor.rowcount
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} cron runs older than {days} days")
        return deleted

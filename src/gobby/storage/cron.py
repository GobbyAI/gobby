"""Cron job storage manager.

Provides CRUD operations for cron jobs and their execution runs.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from gobby.storage.cron_constants import MIN_CRON_INTERVAL_SECONDS
from gobby.storage.cron_models import CronJob
from gobby.storage.cron_runs import CronRunStorageMixin
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import parse_stored_datetime, resolve_local_timezone, utc_now

logger = logging.getLogger(__name__)

REMOVED_AUTOMATION_JOB_NAMES = frozenset({"gobby:dispatcher", "gobby:pipeline-heartbeat"})
CODEWIKI_NIGHTLY_JOB_PREFIX = "gobby:codewiki-nightly:"
# Per-project automation whose handlers were retired; rows are kept dormant
# for the wiki redesign but must never list, dispatch, or re-enable.
RETIRED_AUTOMATION_JOB_NAME_PREFIXES = (CODEWIKI_NIGHTLY_JOB_PREFIX,)
CRON_JOB_NAME_PRIORITIES = {
    "gobby:pipeline-heartbeat": 0,
    "gobby:dispatcher": 1,
    "gobby:code-index-prune": 3,
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
        # Renaming the display label is safe on system rows: the identifier
        # (name) stays stable, so scheduler lookups are unaffected.
        "display_name",
    }
)


class SystemRowProtected(ValueError):
    """Raised when operator-facing code tries to mutate a system cron row."""


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()


def _db_timestamp(value: object) -> object:
    if value is None or value is UNSET:
        return value
    if isinstance(value, datetime | str):
        return parse_stored_datetime(value)
    return value


def _normalize_timestamp_fields(fields: dict[str, Any]) -> None:
    for field in ("run_at", "next_run_at", "last_run_at", "created_at", "updated_at"):
        if field in fields:
            fields[field] = _db_timestamp(fields[field])


def _normalize_interval_seconds(schedule_type: str, interval_seconds: int | None) -> int | None:
    if schedule_type != "interval" or interval_seconds is None:
        return interval_seconds
    return max(interval_seconds, MIN_CRON_INTERVAL_SECONDS)


def _cron_job_priority(job: CronJob) -> int:
    return CRON_JOB_NAME_PRIORITIES.get(job.name, DEFAULT_CRON_JOB_PRIORITY)


def is_removed_automation_job(job: CronJob) -> bool:
    return job.name in REMOVED_AUTOMATION_JOB_NAMES or job.name.startswith(
        RETIRED_AUTOMATION_JOB_NAME_PREFIXES
    )


def _escape_like_prefix(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


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
        logger.warning("Invalid timezone %r for job %s, falling back to UTC", job.timezone, job.id)
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
            logger.debug("Job %s: schedule_type='once' but run_at is missing", job.id)
            return None
        run_at_utc = job.run_at.astimezone(ZoneInfo("UTC"))
        # Expired one-shot
        now_utc = datetime.now(ZoneInfo("UTC"))
        if run_at_utc <= now_utc:
            logger.debug(
                "Job %s: one-shot run_at %s is in the past (now=%s)",
                job.id,
                run_at_utc,
                now_utc,
            )
            return None
        return run_at_utc

    return None


class CronJobStorage(CronRunStorageMixin):
    """Manager for cron job storage."""

    def __init__(self, db: HubDatabase):
        self.db = db

    def create_job(
        self,
        project_id: str,
        name: str,
        schedule_type: Literal["cron", "interval", "once"],
        action_type: Literal["agent_spawn", "pipeline", "shell", "handler", "dispatcher"],
        action_config: dict[str, Any],
        display_name: str | None = None,
        description: str | None = None,
        cron_expr: str | None = None,
        interval_seconds: int | None = None,
        run_at: str | None = None,
        timezone: str | None = None,
        enabled: bool = True,
        is_system: bool = False,
    ) -> CronJob:
        """Create a new cron job.

        An omitted ``timezone`` resolves to the host zone: a wall-clock
        expression like ``0 2 * * *`` means 2 AM where the daemon runs.
        Timestamps are still stored as UTC.
        """
        if schedule_type == "cron" and (not cron_expr or not croniter.is_valid(cron_expr)):
            raise ValueError(f"Invalid cron expression: {cron_expr!r}")

        job_id = str(uuid.uuid4())
        now = utc_now()
        run_at_value = parse_stored_datetime(run_at)

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
            display_name=(display_name or "").strip() or None,
            description=description,
            cron_expr=cron_expr,
            interval_seconds=interval_seconds,
            run_at=run_at_value,
            timezone=resolve_local_timezone(timezone),
            enabled=enabled,
            is_system=is_system,
        )

        # Compute initial next_run_at
        next_run = compute_next_run(job)
        if enabled and next_run is None:
            raise ValueError("enabled cron job requires a valid future schedule")
        job.next_run_at = next_run

        row = self.db.fetchone(
            """
            INSERT INTO cron_jobs (
                id, project_id, name, display_name, description, schedule_type,
                cron_expr, interval_seconds, run_at, timezone,
                action_type, action_config, enabled, is_system, next_run_at,
                last_run_at, last_status, consecutive_failures
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING created_at, updated_at
            """,
            (
                job.id,
                job.project_id,
                job.name,
                job.display_name,
                job.description,
                job.schedule_type,
                job.cron_expr,
                job.interval_seconds,
                _db_timestamp(job.run_at),
                job.timezone,
                job.action_type,
                json.dumps(job.action_config),
                bool(job.enabled),
                bool(job.is_system),
                next_run,
                job.last_run_at,
                job.last_status,
                job.consecutive_failures,
            ),
        )
        if row is not None:
            job.created_at = row["created_at"]
            job.updated_at = row["updated_at"]

        return job

    def get_job(self, job_id: str) -> CronJob | None:
        """Get a cron job by ID."""
        row = self.db.fetchone("SELECT * FROM cron_jobs WHERE id = %s", (job_id,))
        return CronJob.from_row(row) if row else None

    def get_job_by_name(self, name: str) -> CronJob | None:
        """Get a cron job by name.

        Args:
            name: Job name (e.g., "gobby:pipeline-heartbeat")

        Returns:
            CronJob or None if not found
        """
        row = self.db.fetchone("SELECT * FROM cron_jobs WHERE name = %s", (name,))
        return CronJob.from_row(row) if row else None

    def mark_as_system_job(self, job_id: str) -> None:
        """Mark an existing cron row as gobby-managed system infrastructure."""
        self.db.execute("UPDATE cron_jobs SET is_system = TRUE WHERE id = %s", (job_id,))

    def list_jobs(
        self,
        project_id: str | None = None,
        enabled: bool | None = None,
        is_system: bool | None = None,
        exclude_removed_automation: bool = False,
        limit: int = 50,
    ) -> list[CronJob]:
        """List cron jobs with optional filters."""
        conditions: list[str] = []
        params: list[Any] = []

        if project_id:
            conditions.append("project_id = %s")
            params.append(project_id)
        if enabled is not None:
            conditions.append("enabled = %s")
            params.append(bool(enabled))
        if is_system is not None:
            conditions.append("is_system = %s")
            params.append(bool(is_system))
        if exclude_removed_automation and REMOVED_AUTOMATION_JOB_NAMES:
            removed_names = sorted(REMOVED_AUTOMATION_JOB_NAMES)
            placeholders = ", ".join(["%s"] * len(removed_names))
            conditions.append(f"name NOT IN ({placeholders})")
            params.extend(removed_names)
        if exclude_removed_automation:
            for retired_prefix in RETIRED_AUTOMATION_JOB_NAME_PREFIXES:
                conditions.append("name NOT LIKE %s ESCAPE '\\'")
                params.append(_escape_like_prefix(retired_prefix))

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)

        rows = self.db.fetchall(
            f"""
            SELECT * FROM cron_jobs
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT %s
            """,  # nosec B608
            tuple(params),
        )
        return [CronJob.from_row(row) for row in rows]

    def list_system_jobs_by_name_prefix(
        self,
        prefix: str,
        *,
        enabled: bool | None = None,
    ) -> list[CronJob]:
        """List gobby-managed system cron rows whose name starts with prefix."""
        if not prefix:
            raise ValueError("prefix must not be empty")
        pattern = _escape_like_prefix(prefix)
        conditions = ["is_system = TRUE", "name LIKE %s ESCAPE '\\'"]
        params: list[Any] = [pattern]
        if enabled is not None:
            conditions.append("enabled = %s")
            params.append(bool(enabled))

        rows = self.db.fetchall(
            f"""
            SELECT * FROM cron_jobs
            WHERE {" AND ".join(conditions)}
            ORDER BY name
            """,  # nosec B608
            tuple(params),
        )
        return [CronJob.from_row(row) for row in rows]

    def list_jobs_by_name_prefix(
        self,
        prefix: str,
        *,
        enabled: bool | None = None,
    ) -> list[CronJob]:
        """List cron rows whose name starts with prefix, regardless of ownership."""
        if not prefix:
            raise ValueError("prefix must not be empty")
        pattern = _escape_like_prefix(prefix)
        conditions = ["name LIKE %s ESCAPE '\\'"]
        params: list[Any] = [pattern]
        if enabled is not None:
            conditions.append("enabled = %s")
            params.append(bool(enabled))

        rows = self.db.fetchall(
            f"""
            SELECT * FROM cron_jobs
            WHERE {" AND ".join(conditions)}
            ORDER BY name
            """,  # nosec B608
            tuple(params),
        )
        return [CronJob.from_row(row) for row in rows]

    _VALID_UPDATE_FIELDS = frozenset(
        {
            "name",
            "display_name",
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
        _normalize_timestamp_fields(fields)

        set_clause = ", ".join(f"{key} = %s" for key in fields.keys())
        values = list(fields.values()) + [job_id]
        self.db.execute(
            f"UPDATE cron_jobs SET {set_clause} WHERE id = %s",  # nosec B608
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
        if fields.get("enabled") and is_removed_automation_job(job):
            raise SystemRowProtected(
                f"Cron row {job_id} targets retired automation {job.name!r}; "
                "re-enabling is rejected because its handler no longer exists."
            )
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

        if "display_name" in fields:
            # Empty string clears the override back to the generated default.
            fields["display_name"] = (fields["display_name"] or "").strip() or None

        self._normalize_update_fields(job, fields)
        schedule_fields = {
            "schedule_type",
            "cron_expr",
            "interval_seconds",
            "run_at",
            "timezone",
            "enabled",
        }
        if schedule_fields.intersection(fields):
            candidate_fields = {key: fields[key] for key in schedule_fields if key in fields}
            if "run_at" in candidate_fields:
                candidate_fields["run_at"] = parse_stored_datetime(candidate_fields["run_at"])
                fields["run_at"] = candidate_fields["run_at"]
            candidate = replace(job, **candidate_fields)
            next_run = compute_next_run(candidate)
            if candidate.enabled and next_run is None:
                raise ValueError("enabled cron job requires a valid future schedule")
            fields["next_run_at"] = next_run
        fields["updated_at"] = utc_now()

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

    def reconcile_system_job_identity(
        self,
        job_id: str,
        *,
        name: str | _Unset = UNSET,
        enabled: bool | _Unset = UNSET,
        next_run_at: str | None | _Unset = UNSET,
    ) -> CronJob | None:
        """Repair identity fields on an existing system cron job.

        This does not recompute schedules; callers enabling a parked system
        job must pass the repaired ``next_run_at`` explicitly.
        """
        job = self.get_job(job_id)
        if job is None:
            return None
        if not job.is_system:
            raise SystemRowProtected(
                f"Cron row {job_id} is non-system; reconcile_system_job_identity "
                "is reserved for gobby-managed system cron rows."
            )

        fields: dict[str, Any] = {
            "name": name,
            "enabled": enabled,
            "next_run_at": next_run_at,
        }
        update_fields = {key: value for key, value in fields.items() if value is not UNSET}
        if not update_fields:
            return job
        resulting_enabled = update_fields.get("enabled", job.enabled)
        resulting_next_run_at = update_fields.get("next_run_at", job.next_run_at)
        if resulting_enabled and resulting_next_run_at is None:
            raise ValueError(
                "enabled=True requires next_run_at when repairing system cron identity"
            )

        update_fields["updated_at"] = utc_now()
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
            next_run_at=next_run,
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
        }
        desired.update({key: value for key, value in optional.items() if value is not UNSET})
        # Bundled schedules are wall-clock local, so an omitted timezone repairs
        # the row to the host zone instead of leaving a stale one in place.
        desired["timezone"] = resolve_local_timezone(
            None if isinstance(timezone, _Unset) else timezone
        )
        self._normalize_update_fields(job, desired)
        for key, value in desired.items():
            if getattr(job, key) != value:
                fields[key] = value

        if not fields:
            return job

        candidate = replace(job, **fields)
        next_run = compute_next_run(candidate) if candidate.enabled else None
        fields["next_run_at"] = next_run
        fields["updated_at"] = utc_now()
        return self._update_job_fields(job_id, **fields)

    def normalize_system_job_timezones(self) -> int:
        """Repoint bundled wall-clock schedules at the host zone.

        Rows installed before local scheduling still carry ``UTC``, and their
        registrar only rewrites them when some other definition field drifts.
        Returns the number of rows repaired.
        """
        local_timezone = resolve_local_timezone()
        repaired = 0
        rows = self.db.fetchall(
            "SELECT * FROM cron_jobs "
            "WHERE is_system IS TRUE AND schedule_type = 'cron' AND timezone <> %s",
            (local_timezone,),
        )
        for row in rows:
            job = CronJob.from_row(row)
            self.reconcile_system_job_definition(
                job.id,
                action_type=job.action_type,
                action_config=job.action_config,
                timezone=local_timezone,
            )
            repaired += 1
        if repaired:
            logger.info(
                "Repaired %s system cron row(s) to local schedule zone %s",
                repaired,
                local_timezone,
            )
        return repaired

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
            conn.execute("DELETE FROM cron_runs WHERE cron_job_id = %s", (job_id,))
            cursor = conn.execute("DELETE FROM cron_jobs WHERE id = %s", (job_id,))
        return cursor.rowcount > 0

    def disable_project_jobs(self, project_id: str) -> list[CronJob]:
        """Park every cron row for a project before its runs are drained."""
        if not project_id:
            raise ValueError("project_id must not be empty")
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                UPDATE cron_jobs
                   SET enabled = FALSE, next_run_at = NULL, updated_at = %s
                 WHERE project_id = %s
                RETURNING *
                """,
                (utc_now(), project_id),
            ).fetchall()
        return [CronJob.from_row(row) for row in rows]

    def delete_project_jobs(self, job_ids: list[str]) -> int:
        """Delete drained project cron rows and their run history."""
        if not job_ids:
            return 0
        placeholders = ", ".join(["%s"] * len(job_ids))
        with self.db.transaction() as conn:
            conn.execute(
                f"DELETE FROM cron_runs WHERE cron_job_id IN ({placeholders})",  # nosec B608
                tuple(job_ids),
            )
            cursor = conn.execute(
                f"DELETE FROM cron_jobs WHERE id IN ({placeholders})",  # nosec B608
                tuple(job_ids),
            )
        return cursor.rowcount

    def delete_removed_automation_jobs(self) -> int:
        """Delete stale bundled automation cron rows that no longer have executors."""
        names = tuple(REMOVED_AUTOMATION_JOB_NAMES)
        if not names:
            return 0
        placeholders = ", ".join(["%s"] * len(names))
        with self.db.transaction() as conn:
            # Placeholders are generated from constant cardinality.
            conn.execute(
                f"""
                DELETE FROM cron_runs
                 WHERE cron_job_id IN (
                    SELECT id
                      FROM cron_jobs
                     WHERE is_system = TRUE
                       AND name IN ({placeholders})
                 )
                """,  # nosec B608
                names,
            )
            # Placeholders are generated from constant cardinality.
            cursor = conn.execute(
                f"""
                DELETE FROM cron_jobs
                 WHERE is_system = TRUE
                   AND name IN ({placeholders})
                """,  # nosec B608
                names,
            )
        return cursor.rowcount

    def delete_system_jobs_by_project_and_name_prefix(
        self,
        project_id: str,
        prefix: str,
    ) -> int:
        """Hard-delete one project's matching system jobs and run history."""
        if not project_id:
            raise ValueError("project_id must not be empty")
        if not prefix:
            raise ValueError("prefix must not be empty")
        pattern = _escape_like_prefix(prefix)
        params = (project_id, pattern)
        with self.db.transaction() as conn:
            conn.execute(
                """
                DELETE FROM cron_runs
                 WHERE cron_job_id IN (
                    SELECT id
                      FROM cron_jobs
                     WHERE project_id = %s
                       AND is_system = TRUE
                       AND name LIKE %s ESCAPE '\\'
                 )
                """,
                params,
            )
            cursor = conn.execute(
                """
                DELETE FROM cron_jobs
                 WHERE project_id = %s
                   AND is_system = TRUE
                   AND name LIKE %s ESCAPE '\\'
                """,
                params,
            )
        return cursor.rowcount

    def toggle_job(self, job_id: str) -> CronJob | None:
        """Toggle a cron job's enabled state."""
        from dataclasses import replace

        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM cron_jobs WHERE id = %s FOR UPDATE", (job_id,)
            ).fetchone()
            if row is None:
                return None
            job = CronJob.from_row(row)
            if not job.enabled and is_removed_automation_job(job):
                raise SystemRowProtected(
                    f"Cron row {job_id} targets retired automation {job.name!r}; "
                    "re-enabling is rejected because its handler no longer exists."
                )
            if job.is_system:
                raise SystemRowProtected(
                    f"Cron row {job_id} is system-managed; toggle_job is operator-facing. "
                    "Use park_system_job or wake_system_job instead."
                )
            new_enabled = not job.enabled
            next_run = compute_next_run(replace(job, enabled=True)) if new_enabled else None
            conn.execute(
                """UPDATE cron_jobs
                   SET enabled = %s, next_run_at = %s, updated_at = %s
                   WHERE id = %s""",
                (new_enabled, next_run, utc_now(), job_id),
            )
        return self.get_job(job_id)

    def get_due_jobs(self) -> list[CronJob]:
        """Get enabled jobs whose next_run_at has passed."""
        now = utc_now()
        rows = self.db.fetchall(
            """
            SELECT * FROM cron_jobs
            WHERE enabled = TRUE AND next_run_at IS NOT NULL AND next_run_at <= %s
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

    def claim_due_job(
        self,
        job_id: str,
        *,
        expected_next_run_at: datetime,
        next_run_at: datetime | None,
        disable: bool = False,
    ) -> bool:
        """Advance a due job only when its schedule still matches the selected row."""
        now = utc_now()
        cursor = self.db.execute(
            """
            UPDATE cron_jobs
               SET enabled = %s,
                   next_run_at = %s,
                   updated_at = %s
             WHERE id = %s
               AND enabled = TRUE
               AND next_run_at = %s
               AND next_run_at <= %s
            """,
            (
                not disable,
                _db_timestamp(next_run_at),
                now,
                job_id,
                _db_timestamp(expected_next_run_at),
                now,
            ),
        )
        return cursor.rowcount == 1

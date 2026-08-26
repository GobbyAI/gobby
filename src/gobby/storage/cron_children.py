"""Cron run child projection helpers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from typing import Literal

from gobby.storage.agents import ACTIVE_AGENT_RUN_STATUSES, TERMINAL_AGENT_RUN_STATUSES
from gobby.storage.cron_models import CronRun, CronRunChild
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import utc_now

PIPELINE_ACTIVE_STATUSES = ("pending", "running", "waiting_approval", "interrupted")
PIPELINE_TERMINAL_STATUSES = ("completed", "failed", "cancelled")
CHILD_STATUS_TABLES = frozenset({"pipeline_executions", "agent_runs"})
INTERRUPTED_RUN_ERROR = "Cron run was interrupted by a daemon restart"
# Re-queue delay for a job whose run a dead daemon left active: long enough for
# the daemon to finish starting, short enough that a checkpointed sweep (memory
# dream) resumes tonight instead of at its next scheduled slot.
INTERRUPTED_RUN_RETRY_DELAY_SECONDS = 60

ChildActionType = Literal["agent_spawn", "pipeline", "shell", "handler", "dispatcher"]


def hydrate_run_children(db: HubDatabase, runs: Sequence[CronRun]) -> list[CronRun]:
    """Attach child status projection to cron runs that reference child work."""
    if not runs:
        return []

    pipeline_ids = [run.pipeline_execution_id for run in runs if run.pipeline_execution_id]
    agent_ids = [run.agent_run_id for run in runs if run.agent_run_id]
    pipeline_statuses = _fetch_statuses(db, "pipeline_executions", pipeline_ids)
    agent_statuses = _fetch_statuses(db, "agent_runs", agent_ids)

    hydrated: list[CronRun] = []
    for run in runs:
        child: CronRunChild | None = None
        if run.pipeline_execution_id:
            child = _pipeline_child(run.pipeline_execution_id, pipeline_statuses)
        elif run.agent_run_id:
            child = _agent_child(run.agent_run_id, agent_statuses)
        hydrated.append(replace(run, child=child))
    return hydrated


def active_children_for_job(
    db: HubDatabase,
    job_id: str,
    action_type: ChildActionType | str,
) -> list[CronRunChild]:
    """Return active child work launched by dispatched runs for one cron job."""
    if action_type == "pipeline":
        return _active_pipeline_children(db, job_id)
    if action_type == "agent_spawn":
        return _active_agent_children(db, job_id)
    return []


def reconcile_interrupted_runs(db: HubDatabase, machine_id: str) -> dict[str, int]:
    """Normalize active cron rows owned by one scheduler machine.

    Runs with live durable children become ``dispatched``. Every other active
    row belonged to a daemon incarnation that is gone: it is closed as
    ``interrupted`` — an interruption is not a failure, so the job's backoff
    counter is untouched — and the job is re-queued for a near-term retry so
    checkpointed work resumes instead of waiting for its next schedule slot.
    """
    dispatched = _reconcile_linked_pipeline_runs(
        db,
        machine_id,
    ) + _reconcile_linked_agent_runs(db, machine_id)
    interrupted_job_ids = _interrupt_remaining_active_runs(db, machine_id)
    requeued = _requeue_interrupted_jobs(db, interrupted_job_ids)
    return {
        "dispatched": dispatched,
        "interrupted": len(interrupted_job_ids),
        "requeued": requeued,
    }


def _fetch_statuses(db: HubDatabase, table: str, ids: Sequence[str | None]) -> dict[str, str]:
    if table not in CHILD_STATUS_TABLES:
        raise ValueError(f"unsupported cron child status table: {table}")
    unique_ids = sorted({value for value in ids if value})
    if not unique_ids:
        return {}
    placeholders = ", ".join("%s" for _ in unique_ids)
    rows = db.fetchall(
        f"SELECT id, status FROM {table} WHERE id IN ({placeholders})",  # nosec B608
        tuple(unique_ids),
    )
    return {row["id"]: row["status"] for row in rows}


def _pipeline_child(child_id: str, statuses: dict[str, str]) -> CronRunChild:
    status = statuses.get(child_id)
    return CronRunChild(
        type="pipeline_execution",
        id=child_id,
        status=status,
        terminal=status in PIPELINE_TERMINAL_STATUSES if status is not None else False,
        missing=status is None,
    )


def _agent_child(child_id: str, statuses: dict[str, str]) -> CronRunChild:
    status = statuses.get(child_id)
    return CronRunChild(
        type="agent_run",
        id=child_id,
        status=status,
        terminal=status in TERMINAL_AGENT_RUN_STATUSES if status is not None else False,
        missing=status is None,
    )


def _active_pipeline_children(db: HubDatabase, job_id: str) -> list[CronRunChild]:
    placeholders = ", ".join("%s" for _ in PIPELINE_ACTIVE_STATUSES)
    rows = db.fetchall(
        f"""
        SELECT DISTINCT pe.id, pe.status
          FROM cron_runs cr
          JOIN pipeline_executions pe ON pe.id = cr.pipeline_execution_id
         WHERE cr.cron_job_id = %s
           AND cr.status = 'dispatched'
           AND pe.status IN ({placeholders})
        """,  # nosec B608
        (job_id, *PIPELINE_ACTIVE_STATUSES),
    )
    return [
        CronRunChild(
            type="pipeline_execution",
            id=row["id"],
            status=row["status"],
            terminal=False,
            missing=False,
        )
        for row in rows
    ]


def _active_agent_children(db: HubDatabase, job_id: str) -> list[CronRunChild]:
    placeholders = ", ".join("%s" for _ in ACTIVE_AGENT_RUN_STATUSES)
    rows = db.fetchall(
        f"""
        SELECT DISTINCT ar.id, ar.status
          FROM cron_runs cr
          JOIN agent_runs ar ON ar.id = cr.agent_run_id
         WHERE cr.cron_job_id = %s
           AND cr.status = 'dispatched'
           AND ar.status IN ({placeholders})
        """,  # nosec B608
        (job_id, *ACTIVE_AGENT_RUN_STATUSES),
    )
    return [
        CronRunChild(
            type="agent_run",
            id=row["id"],
            status=row["status"],
            terminal=False,
            missing=False,
        )
        for row in rows
    ]


def _reconcile_linked_pipeline_runs(db: HubDatabase, machine_id: str) -> int:
    placeholders = ", ".join("%s" for _ in PIPELINE_ACTIVE_STATUSES)
    cursor = db.execute(
        f"""
        UPDATE cron_runs cr
           SET status = 'dispatched',
               completed_at = COALESCE(cr.completed_at, NOW()),
               error = NULL
          FROM cron_jobs cj, pipeline_executions pe
         WHERE cr.cron_job_id = cj.id
           AND cj.action_type = 'pipeline'
           AND pe.id = cr.pipeline_execution_id
           AND pe.status IN ({placeholders})
           AND cr.status IN ('pending', 'running')
           AND cr.machine_id = %s
        """,  # nosec B608
        (*PIPELINE_ACTIVE_STATUSES, machine_id),
    )
    return cursor.rowcount


def _reconcile_linked_agent_runs(db: HubDatabase, machine_id: str) -> int:
    placeholders = ", ".join("%s" for _ in ACTIVE_AGENT_RUN_STATUSES)
    cursor = db.execute(
        f"""
        UPDATE cron_runs cr
           SET status = 'dispatched',
               completed_at = COALESCE(cr.completed_at, NOW()),
               error = NULL
          FROM cron_jobs cj, agent_runs ar
         WHERE cr.cron_job_id = cj.id
           AND cj.action_type = 'agent_spawn'
           AND ar.id = cr.agent_run_id
           AND ar.status IN ({placeholders})
           AND cr.status IN ('pending', 'running')
           AND cr.machine_id = %s
        """,  # nosec B608
        (*ACTIVE_AGENT_RUN_STATUSES, machine_id),
    )
    return cursor.rowcount


def _interrupt_remaining_active_runs(db: HubDatabase, machine_id: str) -> list[str]:
    rows = db.fetchall(
        """
        UPDATE cron_runs
           SET status = 'interrupted',
               completed_at = COALESCE(completed_at, NOW()),
               error = COALESCE(error, %s)
         WHERE status IN ('pending', 'running')
           AND machine_id = %s
        RETURNING cron_job_id
        """,
        (INTERRUPTED_RUN_ERROR, machine_id),
    )
    return [str(row["cron_job_id"]) for row in rows]


def _requeue_interrupted_jobs(db: HubDatabase, job_ids: Sequence[str]) -> int:
    """Pull each interrupted job's next run forward to a near-term retry.

    Only enabled, scheduled jobs move; a parked system row (``next_run_at``
    NULL) or a disabled one-shot stays where its owner left it, and a schedule
    that is already sooner is kept.
    """
    if not job_ids:
        return 0
    retry_at = utc_now() + timedelta(seconds=INTERRUPTED_RUN_RETRY_DELAY_SECONDS)
    cursor = db.execute(
        """
        UPDATE cron_jobs
           SET next_run_at = %s
         WHERE id = ANY(%s::uuid[])
           AND enabled
           AND next_run_at > %s
        """,
        (retry_at, sorted(set(job_ids)), retry_at),
    )
    return cursor.rowcount

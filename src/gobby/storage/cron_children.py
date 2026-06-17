"""Cron run child projection helpers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Literal

from gobby.storage.agents import ACTIVE_AGENT_RUN_STATUSES, TERMINAL_AGENT_RUN_STATUSES
from gobby.storage.cron_models import CronRun, CronRunChild
from gobby.storage.hub.protocol import HubDatabase

PIPELINE_ACTIVE_STATUSES = ("pending", "running", "waiting_approval", "interrupted")
PIPELINE_TERMINAL_STATUSES = ("completed", "failed", "cancelled")
CHILD_STATUS_TABLES = frozenset({"pipeline_executions", "agent_runs"})

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


def reconcile_interrupted_runs(db: HubDatabase) -> dict[str, int]:
    """Normalize active cron rows left behind by a previous process."""
    dispatched = _reconcile_linked_pipeline_runs(db) + _reconcile_linked_agent_runs(db)
    failed = _fail_remaining_active_runs(db)
    return {"dispatched": dispatched, "failed": failed}


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


def _reconcile_linked_pipeline_runs(db: HubDatabase) -> int:
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
        """,  # nosec B608
        PIPELINE_ACTIVE_STATUSES,
    )
    return cursor.rowcount


def _reconcile_linked_agent_runs(db: HubDatabase) -> int:
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
        """,  # nosec B608
        ACTIVE_AGENT_RUN_STATUSES,
    )
    return cursor.rowcount


def _fail_remaining_active_runs(db: HubDatabase) -> int:
    cursor = db.execute(
        """
        UPDATE cron_runs
           SET status = 'failed',
               completed_at = COALESCE(completed_at, NOW()),
               error = COALESCE(
                   error,
                   'Cron run was still active when the scheduler started'
               )
         WHERE status IN ('pending', 'running')
        """
    )
    return cursor.rowcount

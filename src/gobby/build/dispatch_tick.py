"""Dispatcher heartbeat burst helper for build entry points."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from gobby.build.claim_recovery import recover_safe_build_claims
from gobby.runner import install_dispatcher_cron_row
from gobby.storage.build_history import best_effort_record_event, best_effort_record_run
from gobby.storage.cron import CronJobStorage
from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

PIPELINE_HEARTBEAT_CRON_JOB_NAME = "gobby:pipeline-heartbeat"


@dataclass
class DispatcherTickSummary:
    """Structured dispatcher heartbeat summary returned by build entry points."""

    ticks: int = 0
    scanned: int = 0
    executed: int = 0
    skipped: int = 0
    cap_reached: bool = False
    reason: str | None = None


async def kick_dispatcher_tick(
    db: HubDatabase | None = None,
    project_id: str | None = None,
    *,
    dispatcher_enabled: bool | None = None,
    services: object | None = None,
    max_ticks: int | None = None,
    max_active_agents: int | None = None,
) -> DispatcherTickSummary:
    """Fire a bounded dispatcher heartbeat burst when the bundled cron row is enabled."""
    woke_system_jobs = False
    if db is not None and project_id is not None and dispatcher_enabled is not False:
        dispatcher_enabled = _wake_build_system_jobs(db, project_id)
        woke_system_jobs = dispatcher_enabled
    elif dispatcher_enabled is None:
        dispatcher_enabled = True

    if not dispatcher_enabled:
        logger.info(
            "dispatcher_tick_skipped",
            extra={"project_id": project_id, "reason": "dispatcher_cron_disabled"},
        )
        summary = DispatcherTickSummary(reason="dispatcher_cron_disabled")
        _record_dispatcher_tick_history(db, project_id, summary)
        return summary

    if db is None:
        return DispatcherTickSummary(ticks=0, reason="database_missing")

    recover_safe_build_claims(db, project_id=project_id)

    from gobby.dispatch.dispatcher import run_heartbeat

    summary = DispatcherTickSummary()
    for _ in range(max_ticks or 3):
        result = await run_heartbeat(
            db=db,
            project_id=project_id,
            services=services,
            max_active_agents=max_active_agents,
        )
        reason = result.reason or ("cap_reached" if result.cap_reached else summary.reason)
        summary = DispatcherTickSummary(
            ticks=summary.ticks + 1,
            scanned=summary.scanned + result.scanned,
            executed=summary.executed + result.executed,
            skipped=summary.skipped + result.skipped,
            cap_reached=summary.cap_reached or result.cap_reached,
            reason=reason,
        )
        if result.executed == 0 or result.cap_reached or result.reason:
            break
    if woke_system_jobs:
        _wake_existing_system_job(CronJobStorage(db), PIPELINE_HEARTBEAT_CRON_JOB_NAME)
    _record_dispatcher_tick_history(db, project_id, summary)
    return summary


def _wake_build_system_jobs(db: HubDatabase, project_id: str) -> bool:
    storage = CronJobStorage(db)
    dispatcher = install_dispatcher_cron_row(db, project_id=project_id)
    if not dispatcher.enabled:
        return False
    storage.wake_system_job(dispatcher.id)
    _wake_existing_system_job(storage, PIPELINE_HEARTBEAT_CRON_JOB_NAME)
    return True


def _wake_existing_system_job(storage: CronJobStorage, name: str) -> None:
    job = storage.get_job_by_name(name)
    if job is None or not job.is_system:
        return
    storage.wake_system_job(job.id)


def _record_dispatcher_tick_history(
    db: HubDatabase | None,
    project_id: str | None,
    summary: DispatcherTickSummary,
) -> None:
    if db is None or project_id is None:
        return
    payload = {
        "ticks": summary.ticks,
        "scanned": summary.scanned,
        "executed": summary.executed,
        "skipped": summary.skipped,
        "cap_reached": summary.cap_reached,
        "reason": summary.reason,
    }
    run = best_effort_record_run(
        db,
        project_id=project_id,
        action="dispatcher_tick",
        status="completed" if summary.reason is None else "skipped",
        actor="dispatcher",
        summary=payload,
    )
    best_effort_record_event(
        db,
        run_id=run.id if run is not None else None,
        project_id=project_id,
        event_type="dispatcher_tick",
        action="dispatcher_tick",
        message=summary.reason,
        payload=payload,
    )

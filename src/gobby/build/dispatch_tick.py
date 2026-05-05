"""Dispatcher heartbeat burst helper for build entry points."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from gobby.runner import install_dispatcher_cron_row
from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)


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
    db: DatabaseProtocol | None = None,
    project_id: str | None = None,
    *,
    dispatcher_enabled: bool | None = None,
    services: object | None = None,
    max_ticks: int | None = None,
    max_active_agents: int | None = None,
) -> DispatcherTickSummary:
    """Fire a bounded dispatcher heartbeat burst when the bundled cron row is enabled."""
    if dispatcher_enabled is None:
        if db is None or project_id is None:
            dispatcher_enabled = True
        else:
            job = install_dispatcher_cron_row(db, project_id=project_id)
            dispatcher_enabled = job.enabled

    if not dispatcher_enabled:
        logger.info(
            "dispatcher_tick_skipped",
            extra={"project_id": project_id, "reason": "dispatcher_cron_disabled"},
        )
        return DispatcherTickSummary(reason="dispatcher_cron_disabled")

    if db is None:
        return DispatcherTickSummary(ticks=0, reason="database_missing")

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
    return summary

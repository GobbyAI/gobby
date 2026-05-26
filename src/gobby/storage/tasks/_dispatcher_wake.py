"""Helpers that wake lifecycle dispatch after task state changes."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


def wake_dispatcher_for_task_change(db: HubDatabase, task_id: str) -> bool:
    """Wake the project dispatcher cron row after an automated task changes state.

    Build dispatch is project-scoped. When the dispatcher has parked itself after an
    idle scan, leaf stage transitions need to mark that system row due again so the
    daemon discovers newly-ready review, merge, parent, or downstream work.
    """
    row = db.fetchone(
        """
        SELECT project_id, allow_automation
          FROM tasks
         WHERE id = ?
        """,
        (task_id,),
    )
    if row is None or not bool(row["allow_automation"]):
        return False

    project_id = str(row["project_id"])
    if not project_id:
        return False

    from gobby.runner import install_dispatcher_cron_row
    from gobby.storage.cron import CronJobStorage

    storage = CronJobStorage(db)
    job = install_dispatcher_cron_row(db, project_id=project_id)
    if not job.enabled:
        return False

    storage.update_system_job_bookkeeping(
        job.id,
        next_run_at=datetime.now(UTC).isoformat(),
    )
    logger.debug(
        "dispatcher_cron_woken_for_task_change",
        extra={"task_id": task_id, "project_id": project_id, "job_id": job.id},
    )
    return True

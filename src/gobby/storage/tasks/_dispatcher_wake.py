"""Helpers that wake lifecycle dispatch after task state changes."""

from __future__ import annotations

import logging

from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


def wake_dispatcher_for_task_change(db: HubDatabase, task_id: str) -> bool:
    """Schedule a direct project dispatch tick after an automated task changes state."""
    row = db.fetchone(
        """
        SELECT project_id, allow_automation
          FROM tasks
         WHERE id = %s
        """,
        (task_id,),
    )
    if row is None or not bool(row["allow_automation"]):
        return False

    project_id = str(row["project_id"])
    if not project_id:
        return False

    from gobby.build.dispatch_tick import schedule_dispatcher_tick_for_project

    scheduled = schedule_dispatcher_tick_for_project(
        db,
        project_id=project_id,
        reason="task_change",
    )
    logger.debug(
        "dispatcher_tick_scheduled_for_task_change",
        extra={"task_id": task_id, "project_id": project_id, "scheduled": scheduled},
    )
    return scheduled

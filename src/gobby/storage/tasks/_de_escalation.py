"""Task de-escalation transitions."""

from __future__ import annotations

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._models import UNSET, Task
from gobby.storage.tasks._read import get_task
from gobby.storage.tasks._updates import update_task


def de_escalate_task(
    db: HubDatabase,
    task_id: str,
    *,
    reason: str,
    reset_validation: bool = False,
) -> Task:
    """Clear escalation state while preserving the claim."""
    task = get_task(db, task_id)
    if not task.is_escalated:
        raise ValueError(f"Task {task_id} is not escalated")

    description = (
        f"{task.description}\n\nDe-escalated: {reason}"
        if task.description
        else f"De-escalated: {reason}"
    )

    with db.transaction():
        update_task(
            db,
            task_id,
            description=description,
            escalated_at=None,
            escalation_reason=None,
            validation_fail_count=0 if reset_validation else UNSET,
        )
    return get_task(db, task_id)

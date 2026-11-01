"""Task de-escalation transitions."""

from __future__ import annotations

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._models import Task
from gobby.storage.tasks._read import get_task
from gobby.storage.tasks._updates import update_task


def de_escalate_task(
    db: HubDatabase,
    task_id: str,
    *,
    reason: str,
) -> Task:
    """Clear explicit escalation state while preserving the claim and evidence."""
    with db.transaction() as conn:
        conn.execute("SELECT id FROM tasks WHERE id = %s FOR UPDATE", (task_id,))
        task = get_task(db, task_id)
        if not task.is_escalated:
            raise ValueError(f"Task {task_id} is not escalated")
        note = f"De-escalated: {reason}"
        update_task(
            db,
            task_id,
            description=f"{task.description}\n\n{note}" if task.description else note,
            escalated_at=None,
            escalation_reason=None,
        )
    return get_task(db, task_id)

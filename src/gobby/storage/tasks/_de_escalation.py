"""Task de-escalation transitions."""

from __future__ import annotations

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._models import UNSET, Task
from gobby.storage.tasks._read import get_task
from gobby.storage.tasks._updates import update_task

# The third rejection in one review epoch escalates; only an audited reset reopens review.
REVIEW_REJECTION_LIMIT = 3
REVIEW_EXHAUSTED = "Three independent review rejections in the current epoch"


def de_escalate_task(
    db: HubDatabase,
    task_id: str,
    *,
    reason: str,
) -> Task:
    """Clear escalation state while preserving the claim.

    De-escalating an exhausted review is the audited reset: it starts a new review
    epoch, and step records from earlier epochs stay as history.
    """
    with db.transaction() as conn:
        conn.execute("SELECT id FROM tasks WHERE id = %s FOR UPDATE", (task_id,))
        task = get_task(db, task_id)
        if not task.is_escalated:
            raise ValueError(f"Task {task_id} is not escalated")
        reset = task.review_fail_count >= REVIEW_REJECTION_LIMIT
        note = f"De-escalated: {reason}"
        if reset:
            note += f" (review epoch {task.review_epoch + 1})"
        update_task(
            db,
            task_id,
            description=f"{task.description}\n\n{note}" if task.description else note,
            escalated_at=None,
            escalation_reason=None,
            review_epoch=task.review_epoch + 1 if reset else UNSET,
            review_fail_count=0 if reset else UNSET,
            review_status="pending" if reset else UNSET,
        )
    return get_task(db, task_id)

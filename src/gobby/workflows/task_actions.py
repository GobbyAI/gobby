"""Workflow-Task integration module.

Provides functions for integrating the task system with the workflow engine:
- update_task_from_workflow(): Update task fields from workflow state
"""

import logging
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task

logger = logging.getLogger(__name__)


def update_task_from_workflow(
    db: HubDatabase,
    task_id: str,
    validation_status: str | None = None,
    validation_feedback: str | None = None,
) -> Task | None:
    """Update a task based on workflow state changes.

    Called when workflow transitions or verifications occur to update the
    corresponding task record.

    Args:
        db: HubDatabase instance
        task_id: ID of the task to update
        validation_status: Validation status ('pending', 'valid', 'invalid', 'error')
        validation_feedback: Feedback from validation

    Returns:
        Updated Task object, or None if task not found
    """
    task_manager = LocalTaskManager(db)

    try:
        # Build update kwargs only for provided values
        update_kwargs: dict[str, Any] = {}
        if validation_status is not None:
            update_kwargs["validation_status"] = validation_status
        if validation_feedback is not None:
            update_kwargs["validation_feedback"] = validation_feedback

        if not update_kwargs:
            # No updates to apply, just return current task
            return task_manager.get_task(task_id)

        task = task_manager.update_task(task_id, **update_kwargs)
        logger.debug("Updated task %s from workflow: %s", task_id, list(update_kwargs.keys()))
        return task

    except ValueError as e:
        logger.warning("Task %s not found for workflow update: %s", task_id, e)
        return None
    except Exception as e:
        logger.error("Failed to update task %s from workflow: %s", task_id, e)
        return None

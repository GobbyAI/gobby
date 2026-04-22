"""Condition helper functions for rule engine expressions.

These functions are registered as allowed_funcs in SafeExpressionEvaluator
so they can be called from rule ``when`` conditions, e.g.:

    when: "task_tree_complete(variables.session_task)"
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def is_task_complete(task: Any) -> bool:
    """Check if a task counts as complete for workflow purposes.

    A task is complete only when status is 'closed'.
    """
    return bool(task.status == "closed")


def task_needs_human_review(task_manager: Any, task_id: str | int | None) -> bool:
    """Check if a task has been escalated for human review.

    Returns True when the task has status 'escalated'.

    Used in rule conditions like:
        when: "task_needs_human_review(variables.session_task)"
    """
    if not task_id:
        return False
    if not task_manager:
        return False

    normalized = _normalize_task_id(task_id)
    task = task_manager.get_task(normalized)
    if not task:
        logger.warning(f"task_needs_human_review: Task '{normalized}' not found")
        return False

    return bool(task.status == "escalated")


def _normalize_task_id(task_id: Any) -> str:
    """Normalize a task_id to string format.

    Handles int seq_nums (e.g. 9438 from auto_task_ref) by converting to '#9438'.
    """
    if isinstance(task_id, int):
        return f"#{task_id}"
    return str(task_id)


def task_tree_complete(task_manager: Any, task_id: str | int | list[str | int] | None) -> bool:
    """Check if a task tree is complete (all work is done).

    A task tree is complete when either:
    - The task is explicitly closed, OR
    - The task has subtasks and ALL subtasks are recursively complete

    Used in rule conditions like:
        when: "task_tree_complete(variables.session_task)"
        when: "task_tree_complete(variables.auto_task_ref)"
    """
    if not task_id:
        return True

    if not task_manager:
        logger.warning("task_tree_complete: No task_manager available")
        return False

    if isinstance(task_id, str | int):
        task_ids = [_normalize_task_id(task_id)]
    elif isinstance(task_id, list):
        task_ids = [_normalize_task_id(t) for t in task_id]
    else:
        logger.warning(f"task_tree_complete: Unexpected task_id type: {type(task_id)}")
        return False

    for tid in task_ids:
        if not _is_tree_complete(task_manager, tid):
            return False

    return True


def task_has_label_prefix(task_manager: Any, task_id: str | int | None, prefix: str) -> bool:
    """Check if a task has any label starting with the given prefix.

    Used by the block-front-half-on-interactive-lock rule to detect whether
    a planning parent is under an interactive session-owned lock (labels of
    the form `interactive:planning-in-progress:<session_id>`). The rule does
    not care which session holds the lock — any lock blocks autonomous
    front_half_tick.

    Returns False on missing task_manager, missing task_id, or unresolvable
    task — fail-open so rule evaluation never crashes on bad input.
    """
    if not task_id:
        return False
    if not task_manager:
        return False

    normalized = _normalize_task_id(task_id)
    task = task_manager.get_task(normalized)
    if not task:
        logger.debug(f"task_has_label_prefix: Task '{normalized}' not found")
        return False

    labels = getattr(task, "labels", None) or []
    return any(isinstance(label, str) and label.startswith(prefix) for label in labels)


def task_status_in(task_manager: Any, task_id: str | int | None, *statuses: str) -> bool:
    """Check whether the task's current status is in the provided set."""
    if not task_id or not statuses:
        return False
    if not task_manager:
        return False

    normalized = _normalize_task_id(task_id)
    task = task_manager.get_task(normalized)
    if not task:
        logger.debug(f"task_status_in: Task '{normalized}' not found")
        return False

    normalized_statuses = {status.strip() for status in statuses if isinstance(status, str)}
    return bool(getattr(task, "status", None) in normalized_statuses)


def _is_tree_complete(task_manager: Any, task_id: str) -> bool:
    """Check if a single task and its subtree are complete."""
    task = task_manager.get_task(task_id)
    if not task:
        logger.warning(f"task_tree_complete: Task '{task_id}' not found")
        return False

    task_closed = is_task_complete(task)
    subtasks = task_manager.list_tasks(parent_task_id=task_id)

    if not subtasks:
        if not task_closed:
            logger.debug(
                f"task_tree_complete: Leaf task '{task_id}' is not complete (status={task.status})"
            )
        return task_closed

    for subtask in subtasks:
        if not _is_tree_complete(task_manager, subtask.id):
            return False

    if not task_closed:
        logger.debug(
            f"task_tree_complete: Task '{task_id}' not explicitly closed but all "
            f"{len(subtasks)} subtask(s) complete — tree is complete"
        )

    return True

"""Condition helper functions for rule engine expressions.

These functions are registered as allowed_funcs in SafeExpressionEvaluator
so they can be called from rule ``when`` conditions, e.g.:

    when: "task_tree_complete(variables.session_task)"
"""

import logging
from typing import Any

from gobby.tasks.state_semantics import projected_task_state

logger = logging.getLogger(__name__)


def is_task_complete(task: Any) -> bool:
    """Check if a task counts as complete for workflow purposes.

    A task is complete only when closure metadata projects to closed.
    """
    return projected_task_state(task) == "closed"


def task_needs_human_review(task_manager: Any, task_id: str | int | None) -> bool:
    """Check if a task has been escalated for human review.

    Returns True when escalation metadata projects to escalated.

    Used in rule conditions like:
        when: "task_needs_human_review(variables.session_task)"
    """
    if not task_id:
        return False
    if not task_manager:
        return False

    normalized = _normalize_task_id(task_id)
    task = _get_task(task_manager, normalized)
    if not task:
        logger.warning(f"task_needs_human_review: Task '{normalized}' not found")
        return False

    return projected_task_state(task) == "escalated"


def _normalize_task_id(task_id: Any) -> str:
    """Normalize a task_id to string format.

    Handles int seq_nums (e.g. 9438 from auto_task_ref) by converting to '#9438'.
    """
    if isinstance(task_id, int):
        return f"#{task_id}"
    return str(task_id)


def _get_task(task_manager: Any, task_id: str) -> Any | None:
    try:
        return task_manager.get_task(task_id)
    except ValueError:
        pass
    if not (task_id.startswith("#") or task_id.isdigit()):
        return None
    try:
        seq_num = int(task_id[1:] if task_id.startswith("#") else task_id)
    except ValueError:
        return None
    db = getattr(task_manager, "db", None)
    if db is None:
        return None
    rows = db.fetchall("SELECT id FROM tasks WHERE seq_num = ?", (seq_num,))
    if len(rows) != 1:
        return None
    try:
        return task_manager.get_task(rows[0]["id"])
    except ValueError:
        return None


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
    task = _get_task(task_manager, normalized)
    if not task:
        logger.debug(f"task_has_label_prefix: Task '{normalized}' not found")
        return False

    labels = getattr(task, "labels", None) or []
    return any(isinstance(label, str) and label.startswith(prefix) for label in labels)


def task_state_in(task_manager: Any, task_id: str | int | None, *states: str) -> bool:
    """Check whether the task's projected stage-native state is in the provided set."""
    if not task_id or not states:
        return False
    if not task_manager:
        return False

    normalized = _normalize_task_id(task_id)
    task = _get_task(task_manager, normalized)
    if not task:
        logger.debug(f"task_state_in: Task '{normalized}' not found")
        return False

    normalized_states = {state.strip() for state in states if isinstance(state, str)}
    return projected_task_state(task) in normalized_states


def task_status_in(task_manager: Any, task_id: str | int | None, *statuses: str) -> bool:
    """Compatibility alias for older rules; prefer task_state_in."""
    legacy_state_map = {"open": "ready"}
    states = tuple(legacy_state_map.get(status, status) for status in statuses)
    return task_state_in(task_manager, task_id, *states)


def _is_tree_complete(task_manager: Any, task_id: str) -> bool:
    """Check if a single task and its subtree are complete."""
    task = _get_task(task_manager, task_id)
    if not task:
        logger.warning(f"task_tree_complete: Task '{task_id}' not found")
        return False

    task_closed = is_task_complete(task)
    subtasks = task_manager.list_tasks(parent_task_id=task_id)

    if not subtasks:
        if not task_closed:
            logger.debug(
                "task_tree_complete: Leaf task '%s' is not complete (state=%s)",
                task_id,
                projected_task_state(task),
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

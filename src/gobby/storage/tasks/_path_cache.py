"""Path cache computation and management utilities.

This module provides functions for computing and updating task path caches,
which represent the hierarchical position of a task as a dotted seq_num path.
"""

import logging

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import utc_now

logger = logging.getLogger(__name__)

MAX_TASK_HIERARCHY_DEPTH = 100


def compute_path_cache(db: HubDatabase, task_id: str) -> str | None:
    """Compute the hierarchical path for a task.

    Traverses up the parent chain to build a dotted path from seq_nums.
    Format: 'ancestor_seq.parent_seq.task_seq' (e.g., '1.3.47')

    Args:
        db: Database protocol instance
        task_id: The task ID to compute path for

    Returns:
        Dotted path string (e.g., '1.3.47'), or None if task not found
        or any task in the chain is missing a seq_num.
    """
    # Build path by walking up parent chain
    path_parts: list[str] = []
    current_id: str | None = task_id

    visited: set[str] = set()
    depth = 0

    while current_id and depth < MAX_TASK_HIERARCHY_DEPTH:
        if current_id in visited:
            logger.warning("Task %s has a cycle in its parent chain at %s", task_id, current_id)
            return None
        visited.add(current_id)
        row = db.fetchone(
            "SELECT seq_num, parent_task_id FROM tasks WHERE id = %s",
            (current_id,),
        )
        if not row:
            # Task not found
            return None

        seq_num = row["seq_num"]
        if seq_num is None:
            # seq_num not yet assigned
            return None

        path_parts.append(str(seq_num))
        current_id = row["parent_task_id"]
        depth += 1

    if current_id is not None:
        logger.warning(
            "Task %s exceeded max depth (%s) when computing path",
            task_id,
            MAX_TASK_HIERARCHY_DEPTH,
        )
        return None

    # Reverse to get root-to-leaf order
    path_parts.reverse()
    return ".".join(path_parts)


def update_path_cache(db: HubDatabase, task_id: str) -> str | None:
    """Compute and store the path_cache for a task.

    Args:
        db: Database protocol instance
        task_id: The task ID to update

    Returns:
        The computed path, or None if computation failed
    """
    path = compute_path_cache(db, task_id)
    if path is not None:
        now = utc_now()
        db.execute(
            "UPDATE tasks SET path_cache = %s, updated_at = %s WHERE id = %s",
            (path, now, task_id),
        )
    return path


def update_descendant_paths(db: HubDatabase, task_id: str) -> int:
    """Update path_cache for a task and all its descendants.

    Use this after reparenting a task to cascade path updates.

    Args:
        db: Database protocol instance
        task_id: The root task ID to start updating from

    Returns:
        Number of tasks updated
    """
    task_ids: list[str] = []
    pending: list[tuple[str, int]] = [(task_id, 0)]
    visited: set[str] = set()

    while pending:
        current_id, depth = pending.pop()
        if current_id in visited:
            raise ValueError(f"Cycle detected while updating descendant paths at task {current_id}")
        if depth >= MAX_TASK_HIERARCHY_DEPTH:
            raise ValueError(
                f"Task hierarchy exceeded max depth ({MAX_TASK_HIERARCHY_DEPTH}) "
                f"while updating descendants of {task_id}"
            )
        visited.add(current_id)
        task_ids.append(current_id)
        children = db.fetchall(
            "SELECT id FROM tasks WHERE parent_task_id = %s",
            (current_id,),
        )
        pending.extend((str(child["id"]), depth + 1) for child in children)

    return sum(update_path_cache(db, current_id) is not None for current_id in task_ids)

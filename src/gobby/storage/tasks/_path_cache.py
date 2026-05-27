"""Path cache computation and management utilities.

This module provides functions for computing and updating task path caches,
which represent the hierarchical position of a task as a dotted seq_num path.
"""

import logging
from datetime import UTC, datetime

from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


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

    # Safety limit to prevent infinite loops (max 100 levels deep)
    max_depth = 100
    depth = 0

    while current_id and depth < max_depth:
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

    if depth >= max_depth:
        logger.warning(f"Task {task_id} exceeded max depth ({max_depth}) when computing path")
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
        now = datetime.now(UTC).isoformat()
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
    updated_count = 0

    # Update the task itself
    if update_path_cache(db, task_id):
        updated_count += 1

    # Find and update all descendants (recursive)
    children = db.fetchall(
        "SELECT id FROM tasks WHERE parent_task_id = %s",
        (task_id,),
    )
    for child in children:
        updated_count += update_descendant_paths(db, child["id"])

    return updated_count

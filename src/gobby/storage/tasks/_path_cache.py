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
    with db.transaction() as conn:
        root_path = compute_path_cache(db, task_id)
        now = utc_now()
        row = conn.execute(
            """
            WITH RECURSIVE subtree(id, path_cache, depth, traversal_path, cycle) AS (
                SELECT id, %s::text, 0, ARRAY[id], FALSE
                FROM tasks
                WHERE id = %s
                UNION ALL
                SELECT child.id,
                       CASE
                           WHEN parent.path_cache IS NULL OR child.seq_num IS NULL THEN NULL
                           ELSE parent.path_cache || '.' || child.seq_num::text
                       END,
                       parent.depth + 1,
                       parent.traversal_path || child.id,
                       child.id = ANY(parent.traversal_path)
                FROM tasks child
                JOIN subtree parent ON child.parent_task_id = parent.id
                WHERE NOT parent.cycle
                  AND parent.depth < %s
            ),
            updated AS (
                UPDATE tasks
                SET path_cache = subtree.path_cache, updated_at = %s
                FROM subtree
                WHERE tasks.id = subtree.id
                  AND subtree.path_cache IS NOT NULL
                  AND NOT subtree.cycle
                  AND subtree.depth < %s
                RETURNING tasks.id
            )
            SELECT (SELECT COUNT(*) FROM updated) AS updated_count,
                   COALESCE(BOOL_OR(cycle), FALSE) AS cycle_detected,
                   COALESCE(BOOL_OR(depth >= %s), FALSE) AS depth_exceeded
            FROM subtree
            """,
            (
                root_path,
                task_id,
                MAX_TASK_HIERARCHY_DEPTH,
                now,
                MAX_TASK_HIERARCHY_DEPTH,
                MAX_TASK_HIERARCHY_DEPTH,
            ),
        ).fetchone()

        if row and row["cycle_detected"]:
            raise ValueError(f"Cycle detected while updating descendant paths at task {task_id}")
        if row and row["depth_exceeded"]:
            raise ValueError(
                f"Task hierarchy exceeded max depth ({MAX_TASK_HIERARCHY_DEPTH}) "
                f"while updating descendants of {task_id}"
            )

        return int(row["updated_count"]) if row else 0

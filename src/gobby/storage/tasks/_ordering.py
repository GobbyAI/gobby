"""Hierarchical task ordering utilities.

This module provides functions for ordering tasks hierarchically,
with parents appearing before their children and siblings sorted
topologically by dependencies.
"""

import heapq
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import count

from gobby.storage.tasks._models import Task, normalize_priority


@dataclass(frozen=True)
class TaskOrderKey:
    """The only fields hierarchy ordering reads from a task.

    A whole task row carries its description, criteria, and escalation prose;
    ordering a project-sized set needs none of it. Keeping the algorithm on
    this projection lets a caller order 15k tasks without fetching, converting,
    or hydrating 15k task objects to return 20 (#20840).
    """

    id: str
    parent_task_id: str | None
    priority: int | None
    created_at: datetime
    blocked_by: tuple[str, ...] = field(default=())


def order_task_keys(keys: Sequence[TaskOrderKey]) -> list[str]:
    """Return task ids in hierarchy order: the contract, over a projection.

    Parents precede their children; roots and siblings are ordered by
    (priority ASC, created_at ASC); and siblings that block one another are
    ordered topologically first, so a blocker precedes what it blocks even
    when its own sort key would place it later.
    """
    if not keys:
        return []

    key_by_id: dict[str, TaskOrderKey] = {k.id: k for k in keys}
    children_by_parent: dict[str | None, list[TaskOrderKey]] = {}

    for key in keys:
        parent_id = key.parent_task_id
        # Only group under parent if parent is in the result set
        if parent_id and parent_id not in key_by_id:
            parent_id = None
        if parent_id not in children_by_parent:
            children_by_parent[parent_id] = []
        children_by_parent[parent_id].append(key)

    def sort_siblings(siblings: list[TaskOrderKey]) -> list[TaskOrderKey]:
        """Sort siblings topologically with priority tie-breaking."""
        if not siblings:
            return []

        # 1. Build local dependency graph for these siblings
        sibling_ids = {t.id for t in siblings}
        graph: dict[str, list[str]] = {t.id: [] for t in siblings}
        in_degree: dict[str, int] = {t.id: 0 for t in siblings}

        for task in siblings:
            # Check who blocks this task (Local dependencies only)
            # task.blocked_by contains IDs of tasks that block 'task'
            # If A blocks B, we want A -> B order.
            # So graph edge is A -> B.
            # task.blocked_by = {A} means B depends on A.

            for blocker_id in task.blocked_by:
                if blocker_id in sibling_ids:
                    graph[blocker_id].append(task.id)
                    in_degree[task.id] += 1

        # 2. Kahn's algorithm over a heap keyed by (priority, created_at, seq).
        # Priority 0 is highest. The monotonic seq breaks ties in insertion
        # order (matching the previous stable-sort behavior) and keeps the key
        # objects out of heap comparisons. A heap keeps each pop O(log n);
        # re-sorting a list per popped node is O(n^2 log n) and starves the
        # daemon on large sibling groups (#19878).
        seq = count()
        heap = [
            (normalize_priority(t.priority), t.created_at, next(seq), t)
            for t in siblings
            if in_degree[t.id] == 0
        ]
        heapq.heapify(heap)

        sorted_siblings: list[TaskOrderKey] = []

        while heap:
            _, _, _, current = heapq.heappop(heap)
            sorted_siblings.append(current)

            # Decrease in-degree of neighbors; push newly available ones.
            for neighbor_id in graph[current.id]:
                in_degree[neighbor_id] -= 1
                if in_degree[neighbor_id] == 0:
                    neighbor = key_by_id[neighbor_id]
                    heapq.heappush(
                        heap,
                        (
                            normalize_priority(neighbor.priority),
                            neighbor.created_at,
                            next(seq),
                            neighbor,
                        ),
                    )

        # Check for cycles (remaining nodes with >0 in-degree)
        if len(sorted_siblings) < len(siblings):
            # Cycle detected. Append remaining nodes sorted by priority.
            sorted_ids = {key.id for key in sorted_siblings}
            remaining = [key for key in siblings if key.id not in sorted_ids]
            remaining.sort(key=lambda t: (normalize_priority(t.priority), t.created_at))
            sorted_siblings.extend(remaining)

        return sorted_siblings

    # Sort children within each parent group
    for parent_id, children in children_by_parent.items():
        children_by_parent[parent_id] = sort_siblings(children)

    # Build result with DFS traversal
    result: list[str] = []

    def add_with_children(key: TaskOrderKey) -> None:
        result.append(key.id)
        for child in children_by_parent.get(key.id, []):
            add_with_children(child)

    # Start with root tasks (no parent or parent not in result set)
    for root_key in children_by_parent.get(None, []):
        add_with_children(root_key)

    return result


def order_tasks_hierarchically(tasks: list[Task]) -> list[Task]:
    """Reorder whole tasks so parents appear before their children.

    Thin wrapper over :func:`order_task_keys`, for callers that already hold
    the task objects and whose result sets are small enough that fetching them
    was never the problem.
    """
    if not tasks:
        return []
    task_by_id = {task.id: task for task in tasks}
    keys = [
        TaskOrderKey(
            id=task.id,
            parent_task_id=task.parent_task_id,
            priority=task.priority,
            created_at=task.created_at,
            blocked_by=tuple(task.blocked_by),
        )
        for task in tasks
    ]
    return [task_by_id[task_id] for task_id in order_task_keys(keys)]

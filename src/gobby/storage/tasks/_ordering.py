"""Hierarchical task ordering utilities.

This module provides functions for ordering tasks hierarchically,
with parents appearing before their children and siblings sorted
topologically by dependencies.
"""

from gobby.storage.tasks._models import Task, normalize_priority


def order_tasks_hierarchically(tasks: list[Task]) -> list[Task]:
    """
    Reorder tasks so parents appear before their children.

    The ordering is: parent -> children (recursively), then next parent -> children, etc.
    Root tasks (no parent) are sorted by priority ASC, then created_at ASC.
    Children are sorted by priority ASC, then created_at ASC within their parent.

    Returns a new list with tasks ordered hierarchically.
    """
    if not tasks:
        return []

    # Build lookup structures
    task_by_id: dict[str, Task] = {t.id: t for t in tasks}
    children_by_parent: dict[str | None, list[Task]] = {}

    for task in tasks:
        parent_id = task.parent_task_id
        # Only group under parent if parent is in the result set
        if parent_id and parent_id not in task_by_id:
            parent_id = None
        if parent_id not in children_by_parent:
            children_by_parent[parent_id] = []
        children_by_parent[parent_id].append(task)

    def sort_siblings(siblings: list[Task]) -> list[Task]:
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

        # 2. Initialize queue with tasks having 0 in-degree (no local blockers)
        # We want to process high priority tasks first among available ones.
        # Priority 0 is highest, so valid sort key is (priority, created_at).
        # We sort the initial list to ensure deterministic order for stable sort
        queue = [t for t in siblings if in_degree[t.id] == 0]
        # Sort queue by priority/created_at so we pop high priority first
        queue.sort(key=lambda t: (normalize_priority(t.priority), t.created_at))

        sorted_siblings: list[Task] = []

        while queue:
            # Pop the first (highest priority available)
            current = queue.pop(0)
            sorted_siblings.append(current)

            # Decrease in-degree of neighbors
            neighbors = graph[current.id]
            # Neighbors might become available. Collect them.
            newly_available = []
            for neighbor_id in neighbors:
                in_degree[neighbor_id] -= 1
                if in_degree[neighbor_id] == 0:
                    newly_available.append(task_by_id[neighbor_id])

            # Sort newly available nodes by priority and add to queue
            # We need to re-sort queue every time or insert in order.
            # Since N is small (siblings usually < 50), simple re-sort of queue is fine.
            newly_available.sort(key=lambda t: (normalize_priority(t.priority), t.created_at))

            # Add newly available to queue. We want to maintain global order in queue
            # based on priority.
            # Merging two sorted lists is O(N).
            queue.extend(newly_available)
            queue.sort(key=lambda t: (normalize_priority(t.priority), t.created_at))

        # Check for cycles (remaining nodes with >0 in-degree)
        if len(sorted_siblings) < len(siblings):
            # Cycle detected. Append remaining nodes sorted by priority.
            sorted_ids = {task.id for task in sorted_siblings}
            remaining = [task for task in siblings if task.id not in sorted_ids]
            remaining.sort(key=lambda t: (normalize_priority(t.priority), t.created_at))
            sorted_siblings.extend(remaining)

        return sorted_siblings

    # Sort children within each parent group
    for parent_id, children in children_by_parent.items():
        children_by_parent[parent_id] = sort_siblings(children)

    # Build result with DFS traversal
    result: list[Task] = []

    def add_with_children(task: Task) -> None:
        result.append(task)
        for child in children_by_parent.get(task.id, []):
            add_with_children(child)

    # Start with root tasks (no parent or parent not in result set)
    for root_task in children_by_parent.get(None, []):
        add_with_children(root_task)

    return result

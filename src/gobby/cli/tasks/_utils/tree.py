"""Tree-walking helpers for hierarchical task display."""

from gobby.storage.tasks import LocalTaskManager, Task


def collect_ancestors(
    tasks: list[Task], task_manager: LocalTaskManager
) -> tuple[list[Task], set[str]]:
    """Collect ancestor tasks to maintain tree hierarchy.

    When filtering tasks (e.g., --ready), we may have tasks whose parents
    are not in the filtered list. This function fetches those ancestors
    so the tree structure is preserved.

    Args:
        tasks: The filtered list of tasks
        task_manager: Task manager for fetching ancestors

    Returns:
        Tuple of (combined task list with ancestors, set of original task IDs)
    """
    task_by_id = {t.id: t for t in tasks}
    original_ids = set(task_by_id.keys())
    ancestors_to_fetch: set[str] = set()

    # Find all ancestors that are missing from the list
    for task in tasks:
        parent_id = task.parent_task_id
        while parent_id and parent_id not in task_by_id:
            ancestors_to_fetch.add(parent_id)
            # We need to fetch the parent to check its parent
            try:
                parent = task_manager.get_task(parent_id)
                task_by_id[parent_id] = parent
                parent_id = parent.parent_task_id
            except ValueError:
                break

    # Combine original tasks with ancestors
    combined = list(tasks)
    for ancestor_id in ancestors_to_fetch:
        if ancestor_id in task_by_id:
            combined.append(task_by_id[ancestor_id])

    return combined, original_ids


def _group_children_by_parent(
    tasks: list[Task],
) -> tuple[dict[str, int], dict[str | None, list[Task]]]:
    """Build (input_order, children_by_parent) maps used for tree traversal.

    Treats parents that aren't in the supplied list as roots (parent_id=None).
    Each parent's children are sorted by their position in `tasks` so the
    storage layer's topological order is preserved.
    """
    task_by_id = {t.id: t for t in tasks}
    input_order = {t.id: i for i, t in enumerate(tasks)}

    children_by_parent: dict[str | None, list[Task]] = {}
    for task in tasks:
        parent_id = task.parent_task_id
        if parent_id and parent_id not in task_by_id:
            parent_id = None
        children_by_parent.setdefault(parent_id, []).append(task)

    for children in children_by_parent.values():
        children.sort(key=lambda t: input_order.get(t.id, float("inf")))

    return input_order, children_by_parent


def sort_tasks_for_tree(tasks: list[Task]) -> list[Task]:
    """Sort tasks for tree display (parent before children, depth-first).

    Returns a new list with tasks sorted in tree traversal order.
    Preserves the input order within each parent group (respecting
    topological sort from storage layer).
    """
    _input_order, children_by_parent = _group_children_by_parent(tasks)

    # Build sorted list via depth-first traversal
    sorted_tasks: list[Task] = []

    def traverse(task: Task) -> None:
        sorted_tasks.append(task)
        for child in children_by_parent.get(task.id, []):
            traverse(child)

    for root_task in children_by_parent.get(None, []):
        traverse(root_task)

    return sorted_tasks


def compute_tree_prefixes(
    tasks: list[Task], primary_ids: set[str] | None = None
) -> dict[str, tuple[str, bool]]:
    """Compute tree-style prefixes for each task in the hierarchy.

    Args:
        tasks: List of tasks to compute prefixes for
        primary_ids: Optional set of "primary" task IDs. Tasks not in this set
                     are considered ancestors (shown muted). If None, all tasks
                     are considered primary.

    Returns:
        Dict mapping task_id -> (prefix string, is_primary).
        prefix is e.g., "├── ", "│   └── "
        is_primary is True if task is in primary_ids (or primary_ids is None)
    """
    task_by_id = {t.id: t for t in tasks}
    _input_order, children_by_parent = _group_children_by_parent(tasks)
    if primary_ids is None:
        primary_ids = set(task_by_id.keys())

    prefixes: dict[str, tuple[str, bool]] = {}

    def compute_prefix(task: Task, ancestor_continues: list[bool]) -> None:
        """Recursively compute prefix for task and its children."""
        is_primary = task.id in primary_ids

        if not task.parent_task_id or task.parent_task_id not in task_by_id:
            # Root task - no prefix
            prefixes[task.id] = ("", is_primary)
        else:
            # Build prefix from ancestor continuation markers
            prefix_parts = []
            for continues in ancestor_continues[:-1]:
                prefix_parts.append("│   " if continues else "    ")
            # Add the branch for this task
            if ancestor_continues:
                is_last = not ancestor_continues[-1]
                prefix_parts.append("└── " if is_last else "├── ")
            prefixes[task.id] = ("".join(prefix_parts), is_primary)

        # Process children
        children = children_by_parent.get(task.id, [])
        for i, child in enumerate(children):
            is_last_child = i == len(children) - 1
            compute_prefix(child, ancestor_continues + [not is_last_child])

    # Start with root tasks
    for root_task in children_by_parent.get(None, []):
        compute_prefix(root_task, [])

    return prefixes


def get_all_descendants(manager: LocalTaskManager, task_id: str) -> list[Task]:
    """Recursively get all descendants of a task (children, grandchildren, etc.).

    Returns tasks in depth-first order (parent before children).

    Args:
        manager: The task manager
        task_id: UUID of the parent task

    Returns:
        List of all descendant tasks
    """
    descendants: list[Task] = []

    def collect_children(parent_id: str) -> None:
        children = manager.list_tasks(parent_task_id=parent_id)
        for child in children:
            descendants.append(child)
            collect_children(child.id)  # Recurse into grandchildren

    collect_children(task_id)
    return descendants

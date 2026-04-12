"""
Shared utilities for task CLI commands.
"""

import json
import logging
import sys
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import click
from wcwidth import wcswidth

from gobby.config.app import load_config
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.sync.tasks import TaskSyncManager
from gobby.tasks.state_semantics import serialize_task_state
from gobby.utils.project_context import get_project_context

if TYPE_CHECKING:
    pass  # LocalTaskManager already imported above

logger = logging.getLogger(__name__)


def check_tasks_enabled() -> None:
    """Check if gobby-tasks is enabled, exit if not."""
    try:
        config = load_config()
        if not config.gobby_tasks.enabled:
            click.echo("Error: gobby-tasks is disabled in configuration", err=True)
            sys.exit(1)
    except (FileNotFoundError, AttributeError, ImportError):
        # Expected errors if config missing or invalid
        # Fail open to allow CLI to work even if config is borked
        pass
    except Exception as e:
        # Unexpected errors handling config
        logger.warning(f"Error checking tasks config: {e}")
        pass


def get_task_manager() -> LocalTaskManager:
    """Get initialized task manager."""
    db = LocalDatabase()
    run_migrations(db)
    return LocalTaskManager(db)


def get_sync_manager() -> TaskSyncManager:
    """Get initialized sync manager."""
    manager = get_task_manager()
    return TaskSyncManager(manager, export_path=".gobby/tasks.jsonl")


def normalize_status(status: str) -> str:
    """Normalize status values for user-friendly CLI input.

    Converts hyphen-separated status names to underscore format:
      in-progress -> in_progress
      needs-review -> needs_review

    Also handles common variations.
    """
    # Replace hyphens with underscores for user convenience
    return status.replace("-", "_")


def get_claimed_task_ids() -> set[str]:
    """Get task IDs that are claimed by active sessions via session_task variable.

    Queries workflow_states for active sessions that have a session_task variable set,
    indicating the task is being actively worked on by that session.

    Supports session_task in multiple formats:
      - #N: Resolved to UUID via seq_num lookup
      - UUID: Used directly
      - Partial UUID prefix: Used for prefix matching

    Returns:
        Set of task UUIDs claimed by active sessions
    """
    try:
        db = LocalDatabase()
        try:
            # Join workflow_states with sessions to find active sessions with session_task
            rows = db.fetchall(
                """
                SELECT ws.variables, s.project_id
                FROM workflow_states ws
                JOIN sessions s ON ws.session_id = s.id
                WHERE s.status = 'active'
                AND ws.variables IS NOT NULL
                AND ws.variables != '{}'
                """
            )

            claimed_ids: set[str] = set()

            def resolve_task_ref(ref: str, project_id: str | None) -> str | None:
                """Resolve a task reference to UUID."""
                if not ref or ref == "*":
                    return None

                # #N format - resolve via seq_num
                if ref.startswith("#"):
                    try:
                        seq_num = int(ref[1:])
                        row = db.fetchone(
                            "SELECT id FROM tasks WHERE project_id = ? AND seq_num = ?",
                            (project_id, seq_num),
                        )
                        return row["id"] if row else None
                    except (ValueError, TypeError):
                        return None

                # Check if it looks like a UUID (36 chars with dashes)
                if len(ref) == 36 and ref.count("-") == 4:
                    return ref

                # Partial UUID prefix - find matching task
                row = db.fetchone(
                    "SELECT id FROM tasks WHERE id LIKE ? AND project_id = ?",
                    (f"%{ref}%", project_id),
                )
                return row["id"] if row else None

            for row in rows:
                try:
                    variables = json.loads(row["variables"]) if row["variables"] else {}
                    project_id = row["project_id"]
                    if session_task := variables.get("session_task"):
                        # session_task can be: string, list of strings, or "*" (wildcard)
                        if isinstance(session_task, list):
                            for task_ref in session_task:
                                if resolved := resolve_task_ref(task_ref, project_id):
                                    claimed_ids.add(resolved)
                        elif session_task != "*":
                            if resolved := resolve_task_ref(session_task, project_id):
                                claimed_ids.add(resolved)
                except (json.JSONDecodeError, TypeError):
                    continue

            return claimed_ids
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"Failed to get claimed task IDs: {e}")
        return set()


def pad_to_width(text: str, width: int) -> str:
    """Pad a string to a visual width, accounting for wide characters like emoji."""
    visual_width: int = wcswidth(text)
    if visual_width < 0:
        visual_width = len(text)  # Fallback if wcswidth fails
    padding: int = width - visual_width
    return text + " " * max(0, padding)


def collect_ancestors(
    tasks: list[Task], task_manager: "LocalTaskManager"
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
            except (ValueError, Exception):
                break

    # Combine original tasks with ancestors
    combined = list(tasks)
    for ancestor_id in ancestors_to_fetch:
        if ancestor_id in task_by_id:
            combined.append(task_by_id[ancestor_id])

    return combined, original_ids


def sort_tasks_for_tree(tasks: list[Task]) -> list[Task]:
    """Sort tasks for tree display (parent before children, depth-first).

    Returns a new list with tasks sorted in tree traversal order.
    Preserves the input order within each parent group (respecting
    topological sort from storage layer).
    """
    task_by_id = {t.id: t for t in tasks}
    # Preserve input order via index lookup
    input_order = {t.id: i for i, t in enumerate(tasks)}

    # Group children by parent
    children_by_parent: dict[str | None, list[Task]] = {}
    for task in tasks:
        parent_id = task.parent_task_id
        if parent_id and parent_id not in task_by_id:
            parent_id = None
        if parent_id not in children_by_parent:
            children_by_parent[parent_id] = []
        children_by_parent[parent_id].append(task)

    # Sort children within each parent by input order (preserves topological sort)
    for children in children_by_parent.values():
        children.sort(key=lambda t: input_order.get(t.id, float("inf")))

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
    # Preserve input order via index lookup
    input_order = {t.id: i for i, t in enumerate(tasks)}
    if primary_ids is None:
        primary_ids = set(task_by_id.keys())

    # Group children by parent
    children_by_parent: dict[str | None, list[Task]] = {}
    for task in tasks:
        parent_id = task.parent_task_id
        if parent_id and parent_id not in task_by_id:
            parent_id = None
        if parent_id not in children_by_parent:
            children_by_parent[parent_id] = []
        children_by_parent[parent_id].append(task)

    # Sort children within each parent by input order (preserves topological sort)
    for children in children_by_parent.values():
        children.sort(key=lambda t: input_order.get(t.id, float("inf")))

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


# Column widths for task table
COL_STATUS = 1  # Status icon
COL_PRIORITY = 2  # Priority emoji (2 visual chars)
COL_ID = 6  # #N format (e.g., #1234)
COL_LIFECYCLE = 15
COL_OWNER = 10
COL_FLAGS = 18


def format_task_row(
    task: Task,
    tree_prefix: str = "",
    is_primary: bool = True,
    muted: bool = False,
    claimed_task_ids: set[str] | None = None,
) -> str:
    """Format a task for list output.

    Args:
        task: The task to format
        tree_prefix: Tree-style prefix (e.g., "├── ", "│   └── ")
        is_primary: If False, task is an ancestor shown for context (muted style)
        muted: Explicit muted flag (overrides is_primary)
        claimed_task_ids: Set of task IDs claimed by active sessions
    """
    show_muted = muted or not is_primary
    state = serialize_task_state(task)
    is_claimed = state["is_claimed"] or (
        claimed_task_ids is not None and task.id in claimed_task_ids
    )
    owner_session_id = state["owner_session_id"] or (
        getattr(task, "assignee", None) if is_claimed else None
    )
    lifecycle_display = state["lifecycle_stage"] or "open"
    blocked = state["is_blocked"] or getattr(task, "status", None) == "blocked"
    escalated = state["is_escalated"]
    closed = state["is_closed"]
    merge_ready = state["is_merge_ready"]

    # Status icons:
    # ○ = open, unclaimed
    # ◐ = open, claimed by active session
    # ● = in_progress
    # ✓ = closed
    # ⊗ = blocked
    # ⚠ = escalated
    if closed:
        status_icon = "✓"
    elif escalated:
        status_icon = "⚠"
    elif blocked:
        status_icon = "⊗"
    elif lifecycle_display == "open" and is_claimed:
        status_icon = "◐"  # Open but claimed by active session
    else:
        status_icon = {
            "open": "○",
            "in_progress": "●",
            "needs_review": "◌",
            "review_approved": "◆",
        }.get(lifecycle_display, "?")

    priority_icon = {
        0: "🟣",  # Critical
        1: "🔴",  # High
        2: "🟡",  # Medium
        3: "🔵",  # Low
        4: "⚪",  # Backlog
    }.get(task.priority, "⚪")

    # Build row with proper visual width padding
    status_col = pad_to_width(status_icon, COL_STATUS)
    priority_col = pad_to_width(priority_icon, COL_PRIORITY)
    # Use #N format for display (seq_num), fallback to short UUID prefix
    task_ref = f"#{task.seq_num}" if task.seq_num else task.id[:8]
    id_col = pad_to_width(task_ref, COL_ID)
    lifecycle_col = pad_to_width(lifecycle_display, COL_LIFECYCLE)
    owner_col = pad_to_width(owner_session_id[:8] if owner_session_id else "-", COL_OWNER)

    flags: list[str] = []
    if blocked:
        flags.append("blocked")
    if escalated:
        flags.append("escalated")
    if merge_ready:
        flags.append("merge-ready")
    if closed:
        flags.append("closed")
    flags_col = pad_to_width(",".join(flags) if flags else "-", COL_FLAGS)

    title = task.title
    if show_muted:
        # Use dim ANSI escape for muted ancestors
        # \033[2m = dim, \033[0m = reset
        title = f"\033[2m{task.title}\033[0m"

    return (
        f"{status_col} {priority_col} {id_col} {lifecycle_col} "
        f"{owner_col} {flags_col} {tree_prefix}{title}"
    )


def format_task_header() -> str:
    """Return header row for task list."""
    status_col = pad_to_width("", COL_STATUS)
    priority_col = pad_to_width("", COL_PRIORITY)
    id_col = pad_to_width("#", COL_ID)
    lifecycle_col = pad_to_width("LIFECYCLE", COL_LIFECYCLE)
    owner_col = pad_to_width("OWNER", COL_OWNER)
    flags_col = pad_to_width("FLAGS", COL_FLAGS)

    return f"{status_col} {priority_col} {id_col} {lifecycle_col} {owner_col} {flags_col} TITLE"


def resolve_task_id(
    manager: LocalTaskManager, task_id: str, project_id: str | None = None
) -> Task | None:
    """Resolve a task ID to a Task with user-friendly errors.

    Supports multiple reference formats:
      - #N: Project-scoped seq_num (e.g., #1, #47) - requires project_id
      - 1.2.3: Path cache format - requires project_id
      - UUID: Direct UUID lookup
      - Prefix: ID prefix matching for partial UUIDs

    Args:
        manager: The task manager
        task_id: Task reference in any supported format
        project_id: Project ID for scoped lookups (#N and path formats).
                   If not provided, will try to get from project context.

    Returns:
        The resolved Task, or None if not found (with error message printed)
    """
    from pathlib import Path

    from gobby.storage.tasks import TaskNotFoundError

    # Get project_id from context if not provided
    if project_id is None:
        ctx = get_project_context(cwd=Path.cwd())
        project_id = ctx.get("id") if ctx else None

    # Try #N format, numeric format (treated as #N), or path format (requires project_id)
    if project_id and (task_id.startswith("#") or task_id.isdigit() or _is_path_format(task_id)):
        # Auto-prefix numeric IDs with #
        if task_id.isdigit():
            task_id = f"#{task_id}"

        try:
            resolved_uuid = manager.resolve_task_reference(task_id, project_id)
            return manager.get_task(resolved_uuid)
        except TaskNotFoundError as e:
            click.echo(f"Task '{task_id}' not found: {e}", err=True)
            return None
        except ValueError as e:
            # Deprecation or format errors
            click.echo(f"Error: {e}", err=True)
            return None

    # Try exact UUID match
    try:
        return manager.get_task(task_id)
    except ValueError:
        pass

    # Try prefix matching for partial UUIDs
    matches = manager.find_tasks_by_prefix(task_id)

    if len(matches) == 0:
        click.echo(f"Task '{task_id}' not found", err=True)
        return None
    elif len(matches) == 1:
        return matches[0]
    else:
        click.echo(f"Ambiguous task ID '{task_id}' matches {len(matches)} tasks:", err=True)
        for task in matches[:5]:
            click.echo(f"  {task.id}: {task.title}", err=True)
        if len(matches) > 5:
            click.echo(f"  ... and {len(matches) - 5} more", err=True)
        return None


def _is_path_format(ref: str) -> bool:
    """Check if a reference is in path format (e.g., 1.2.3)."""
    if "." not in ref:
        return False
    parts = ref.split(".")
    return all(part.isdigit() for part in parts)


class _CascadeIterator:
    """Iterator wrapper that handles errors via callback."""

    def __init__(
        self,
        tasks: list[Task],
        label: str,
        on_error: Callable[[Task, Exception], bool] | None,
    ):
        self._tasks = tasks
        self._label = label
        self._on_error = on_error
        self._index = 0
        self._total = len(tasks)
        self._stop = False
        self._current_task: Task | None = None
        self._pending_error: Exception | None = None
        self._completed_count = 0

    def __iter__(self) -> "_CascadeIterator":
        return self

    def __next__(self) -> tuple[Task, Callable[[], None]]:
        # Handle any pending error from previous iteration
        if self._pending_error is not None:
            error = self._pending_error
            self._pending_error = None
            task = self._current_task

            if self._on_error is not None and task is not None:
                should_continue = self._on_error(task, error)
                if not should_continue:
                    self._stop = True
                    raise StopIteration
            else:
                raise error

        if self._stop or self._index >= self._total:
            raise StopIteration

        task = self._tasks[self._index]
        self._current_task = task
        self._index += 1

        task_ref = f"#{task.seq_num}" if task.seq_num else task.id[:8]

        # Truncate long titles
        max_title_len = 40
        title = task.title
        if len(title) > max_title_len:
            title = title[: max_title_len - 3] + "..."

        # Print progress line with label
        progress_str = f"{self._label} [{self._index}/{self._total}] {task_ref}: {title}"
        click.echo(progress_str)

        def update() -> None:
            """Mark the current task as completed."""
            self._completed_count += 1

        return task, update

    def report_error(self, error: Exception) -> None:
        """Report an error for the current task."""
        self._pending_error = error


@contextmanager
def cascade_progress(
    tasks: list[Task],
    label: str = "Processing",
    on_error: Callable[[Task, Exception], bool] | None = None,
) -> Generator[Iterator[tuple[Task, Callable[[], None]]]]:
    """Context manager for cascade operations with progress display.

    Yields (task, update) pairs for each task. Call update() after
    processing each task to advance the progress bar.

    Args:
        tasks: List of tasks to process
        label: Label to show before progress bar (e.g., "Expanding")
        on_error: Optional callback for errors. Receives (task, error).
                  Return True to continue, False to stop processing.

    Yields:
        Iterator of (task, update_fn) tuples

    Example:
        with cascade_progress(tasks, label="Expanding") as progress:
            for task, update in progress:
                await expand_task(task)
                update()  # Mark complete
    """
    if not tasks:
        yield iter([])
        return

    iterator = _CascadeIterator(tasks, label, on_error)
    try:
        yield iterator
    except KeyboardInterrupt:
        click.echo("\nOperation interrupted by user.")
        raise
    except Exception as e:
        # Handle error on current task
        if on_error is not None and iterator._current_task is not None:
            # Call on_error callback for logging, but always re-raise
            # The on_error return value is only used in next-iteration logic
            on_error(iterator._current_task, e)
        raise
    finally:
        # Handle any pending error from final iteration (report_error called on last task)
        if iterator._pending_error is not None:
            error = iterator._pending_error
            iterator._pending_error = None
            task = iterator._current_task
            # Capture any exception from the iterator body to preserve as __cause__
            body_exception = sys.exc_info()[1]
            if on_error is not None and task is not None:
                # Call on_error callback for pending error, preserving both exceptions if callback fails
                try:
                    on_error(task, error)
                    # Error handled via callback, don't re-raise
                except Exception as callback_exc:
                    # Chain exceptions: pending error from body exception (if any) or callback failure
                    if body_exception is not None:
                        raise error from body_exception
                    raise error from callback_exc
            else:
                # No on_error callback - re-raise the pending error chained to body exception if present
                if body_exception is not None:
                    raise error from body_exception
                raise error from None


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


def parse_task_refs(refs: tuple[str, ...]) -> list[str]:
    """Parse task references from various CLI input formats.

    Handles multiple input formats commonly used in CLI:
    - Single reference: "42", "#42", "abc123-def"
    - Comma-separated: "#42,#43,#44" or "42,43,44"
    - Space-separated: passed as tuple from Click variadic args
    - Mixed: "#42,#43 #44" with both separators

    Numeric references are normalized to #N format.
    UUID-like references are passed through unchanged.

    Args:
        refs: Tuple of reference strings from Click variadic argument

    Returns:
        List of normalized task references
    """
    result: list[str] = []

    for arg in refs:
        # Split on commas first
        parts = arg.split(",")
        for part in parts:
            ref = part.strip()
            if not ref:
                continue

            # Normalize pure numeric to #N format
            if ref.isdigit():
                ref = f"#{ref}"

            result.append(ref)

    return result

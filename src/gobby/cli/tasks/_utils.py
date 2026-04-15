"""
Shared utilities for task CLI commands.
"""

import json
import logging
import shutil
import sys
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import click
from wcwidth import wcswidth

from gobby.config.app import load_config
from gobby.storage.database import DatabaseProtocol, LocalDatabase
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


# Column widths for compact task table
COL_PRIORITY = 2  # Priority emoji (2 visual chars)
COL_STATUS_LETTER = 1  # Single-letter lifecycle code
COL_FLAGS = 3  # Up to 3 flag letters (B/E/M)
COL_ID_MIN = 6  # #N format minimum (e.g., #1234)
COL_SESSION_MIN = 6  # #N format minimum for session refs
PREFIX_W = COL_PRIORITY + COL_STATUS_LETTER + COL_FLAGS  # 6 visual cols before #id
_DIM_ANSI = "\033[2m"
_RESET_ANSI = "\033[0m"

# Lifecycle stage -> single-letter code (O/P/R/A/C)
_LIFECYCLE_LETTER: dict[str, str] = {
    "open": "O",
    "in_progress": "P",
    "needs_review": "R",
    "review_approved": "A",
    "closed": "C",
}

# Priority level -> emoji (2 visual cols)
_PRIORITY_ICON: dict[int, str] = {
    0: "🟣",  # Critical
    1: "🔴",  # High
    2: "🟡",  # Medium
    3: "🔵",  # Low
    4: "⚪",  # Backlog
}


@dataclass(frozen=True)
class _RenderedRow:
    """A task row with all display fields resolved but not yet padded."""

    priority_icon: str
    lifecycle_letter: str
    flags_letters: str
    task_ref: str
    title: str
    session_ref: str | None
    tree_prefix: str
    is_muted: bool


def _visual_width(text: str) -> int:
    """Visual width of a string accounting for emoji/CJK, with a safe fallback."""
    width = wcswidth(text)
    return width if width >= 0 else len(text)


def _truncate_to_width(text: str, width: int) -> str:
    """Truncate a string from the right to fit a visual width, appending an ellipsis if cut.

    Edge cases: returns "" for width <= 0; returns "…" for width == 1.
    """
    if width <= 0:
        return ""
    if _visual_width(text) <= width:
        return text
    if width == 1:
        return "…"
    ellipsis = "…"
    out = ""
    used = 0
    for ch in text:
        ch_w = _visual_width(ch)
        if used + ch_w > width - 1:
            break
        out += ch
        used += ch_w
    return out + ellipsis


def _build_rendered_row(
    task: Task,
    *,
    tree_prefix: str = "",
    is_primary: bool = True,
    muted: bool = False,
    claimed_task_ids: set[str] | None = None,
    session_ref_map: dict[str, str] | None = None,
) -> _RenderedRow:
    """Resolve the display fields for a single task row.

    ``session_ref_map`` is an optional pre-resolved ``owner_session_id -> '#N'``
    lookup produced by :func:`_resolve_session_refs`. When provided, the row's
    session column is populated from the map; otherwise it's left unresolved
    (the caller can suppress or fall back to an assignee prefix).
    """
    is_muted = muted or not is_primary
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

    lifecycle_letter = _LIFECYCLE_LETTER.get(lifecycle_display, "?")
    if closed:
        lifecycle_letter = "C"

    flags = ""
    if blocked:
        flags += "B"
    if escalated:
        flags += "E"
    if merge_ready:
        flags += "M"

    priority_icon = _PRIORITY_ICON.get(getattr(task, "priority", 4), "⚪")
    task_ref = f"#{task.seq_num}" if getattr(task, "seq_num", None) else task.id[:8]

    session_ref: str | None = None
    if owner_session_id:
        if session_ref_map and owner_session_id in session_ref_map:
            session_ref = session_ref_map[owner_session_id]
        else:
            # Fallback: first 8 chars of UUID prefixed so it's obvious it's not a seq_num
            session_ref = owner_session_id[:8]

    # Closed tasks render dim alongside ancestors
    row_is_muted = is_muted or closed

    return _RenderedRow(
        priority_icon=priority_icon,
        lifecycle_letter=lifecycle_letter,
        flags_letters=flags,
        task_ref=task_ref,
        title=task.title,
        session_ref=session_ref,
        tree_prefix=tree_prefix,
        is_muted=row_is_muted,
    )


def _render_row(
    row: _RenderedRow,
    *,
    id_w: int,
    title_w: int,
    session_w: int,
) -> str:
    """Render a single prepared row to its final string form.

    All widths are resolved up front by :func:`format_task_list` so the session
    column lines up across every row in a block — that's what makes it a column
    instead of a trailing suffix.
    """
    pri = pad_to_width(row.priority_icon, COL_PRIORITY)
    stat = pad_to_width(row.lifecycle_letter, COL_STATUS_LETTER)
    flags = pad_to_width(row.flags_letters, COL_FLAGS)
    id_col = pad_to_width(row.task_ref, id_w)

    title_text = f"{row.tree_prefix}{row.title}"
    title_text = _truncate_to_width(title_text, title_w)
    title_col = pad_to_width(title_text, title_w)

    core = f"{pri}{stat}{flags} {id_col}  {title_col}"

    if session_w > 0:
        if row.session_ref:
            session_text = pad_to_width(row.session_ref, session_w)
            # Dim the session ref so it doesn't compete with the title
            session_col = f"{_DIM_ANSI}{session_text}{_RESET_ANSI}"
        else:
            session_col = " " * session_w
        core = f"{core}  {session_col}"

    if row.is_muted:
        # Dim the whole row for ancestors / closed tasks; session dim is already applied
        return f"{_DIM_ANSI}{core}{_RESET_ANSI}"
    return core


def _get_term_width(default: int = 100) -> int:
    """Return the current terminal width (columns)."""
    try:
        return shutil.get_terminal_size(fallback=(default, 24)).columns
    except (OSError, ValueError):
        return default


def _resolve_session_refs(
    session_ids: set[str], db: DatabaseProtocol | None = None
) -> dict[str, str]:
    """Batch-resolve ``session_id`` UUIDs to their ``#seq_num`` refs.

    Returns a map ``{uuid: "#N"}``. Missing rows are omitted (callers fall back
    to an 8-char UUID prefix).
    """
    if not session_ids:
        return {}
    owner_db: DatabaseProtocol = db or LocalDatabase()
    try:
        placeholders = ",".join("?" * len(session_ids))
        rows = owner_db.fetchall(
            f"SELECT id, seq_num FROM sessions WHERE id IN ({placeholders})",
            tuple(session_ids),
        )
        return {row["id"]: f"#{row['seq_num']}" for row in rows if row["seq_num"] is not None}
    except Exception as e:
        logger.debug(f"Failed to batch-resolve session refs: {e}")
        return {}
    finally:
        if db is None:
            owner_db.close()


def _resolve_project_names(
    project_ids: set[str], db: DatabaseProtocol | None = None
) -> dict[str, str]:
    """Batch-resolve ``project_id`` UUIDs to project names."""
    if not project_ids:
        return {}
    owner_db: DatabaseProtocol = db or LocalDatabase()
    try:
        placeholders = ",".join("?" * len(project_ids))
        rows = owner_db.fetchall(
            f"SELECT id, name FROM projects WHERE id IN ({placeholders})",
            tuple(project_ids),
        )
        return {row["id"]: row["name"] for row in rows}
    except Exception as e:
        logger.debug(f"Failed to batch-resolve project names: {e}")
        return {}
    finally:
        if db is None:
            owner_db.close()


def format_task_row(
    task: Task,
    tree_prefix: str = "",
    is_primary: bool = True,
    muted: bool = False,
    claimed_task_ids: set[str] | None = None,
) -> str:
    """Format a single task for list output (compact layout, no session column).

    This is the single-row primitive used by callers that render one task at a
    time (dependency trees, blocked-task details). For multi-row renders,
    prefer :func:`format_task_list` — it computes column widths across all rows
    and attaches the session column when any row is claimed.

    Args:
        task: The task to format
        tree_prefix: Tree-style prefix (e.g., "├── ", "│   └── ")
        is_primary: If False, task is an ancestor shown for context (muted)
        muted: Explicit muted flag (overrides is_primary)
        claimed_task_ids: Set of task IDs claimed by active sessions
    """
    row = _build_rendered_row(
        task,
        tree_prefix=tree_prefix,
        is_primary=is_primary,
        muted=muted,
        claimed_task_ids=claimed_task_ids,
    )
    id_w = max(COL_ID_MIN, _visual_width(row.task_ref))
    title_text = f"{row.tree_prefix}{row.title}"
    title_w = max(1, _visual_width(title_text))
    return _render_row(row, id_w=id_w, title_w=title_w, session_w=0)


def format_task_header() -> str:
    """Return a compact header row.

    Kept for compatibility with single-table callers and tests. The new
    :func:`format_task_list` renderer does not emit a header in compact mode.
    """
    pri = pad_to_width("", COL_PRIORITY)
    stat = pad_to_width("", COL_STATUS_LETTER)
    flags = pad_to_width("", COL_FLAGS)
    id_col = pad_to_width("#", COL_ID_MIN)
    return f"{pri}{stat}{flags} {id_col}  TITLE"


def format_task_list(
    tasks: list[Task],
    *,
    claimed_task_ids: set[str] | None = None,
    primary_ids: set[str] | None = None,
    tree_prefixes: dict[str, tuple[str, bool]] | None = None,
    group_by: str | None = None,
    term_width: int | None = None,
    db: DatabaseProtocol | None = None,
) -> str:
    """Render a list of tasks as one compact block.

    Columns:
        ``[pri][status][flags] #id  title  #session``

    The session column is present iff at least one row has an owner. Column
    widths are computed once across all rows so the session column is a true
    column (fixed x-position), not a trailing suffix.

    Args:
        tasks: Tasks in the render order the caller wants.
        claimed_task_ids: Task IDs claimed by active sessions (for owner hint).
        primary_ids: If given, tasks not in this set are rendered as muted
            ancestors. If omitted, every task is primary.
        tree_prefixes: Precomputed ``task_id -> (prefix, is_primary)`` from
            :func:`compute_tree_prefixes`. When omitted, no tree prefix is used.
        group_by: ``"project"``, ``"lifecycle"``, or ``None``.
        term_width: Terminal width override (for tests). Defaults to the live
            terminal size.
        db: Optional database handle for batch lookups. A fresh
            :class:`LocalDatabase` is opened (and closed) if not supplied.

    Returns:
        A newline-joined block, ready to pass to :func:`click.echo`.
    """
    if not tasks:
        return ""

    tw = term_width if term_width is not None else _get_term_width()

    # First pass: resolve session refs (one query) and project names (one query
    # when grouping by project).
    session_ids: set[str] = set()
    project_ids: set[str] = set()
    for task in tasks:
        state = serialize_task_state(task)
        is_claimed = state["is_claimed"] or (
            claimed_task_ids is not None and task.id in claimed_task_ids
        )
        owner = state["owner_session_id"] or (
            getattr(task, "assignee", None) if is_claimed else None
        )
        if owner:
            session_ids.add(owner)
        if group_by == "project":
            pid = getattr(task, "project_id", None)
            if pid:
                project_ids.add(pid)

    owner_db: DatabaseProtocol = db or LocalDatabase()
    try:
        session_ref_map = _resolve_session_refs(session_ids, db=owner_db)
        project_name_map: dict[str, str] = {}
        if group_by == "project":
            project_name_map = _resolve_project_names(project_ids, db=owner_db)
    finally:
        if db is None:
            owner_db.close()

    # Second pass: build rendered rows
    rendered: list[_RenderedRow] = []
    for task in tasks:
        prefix_info = (tree_prefixes or {}).get(task.id, ("", True))
        tree_prefix, is_primary_from_tree = prefix_info
        is_primary = (primary_ids is None or task.id in primary_ids) and is_primary_from_tree
        row = _build_rendered_row(
            task,
            tree_prefix=tree_prefix,
            is_primary=is_primary,
            claimed_task_ids=claimed_task_ids,
            session_ref_map=session_ref_map,
        )
        rendered.append(row)

    # Widths computed once across every row in the block
    id_w = max(COL_ID_MIN, *(_visual_width(r.task_ref) for r in rendered))
    has_session_col = any(r.session_ref for r in rendered)
    session_w = (
        max(COL_SESSION_MIN, *(_visual_width(r.session_ref or "") for r in rendered))
        if has_session_col
        else 0
    )

    # Separator budget matches the template in _render_row:
    #   "{pri}{stat}{flags} {id}  {title}" → 3 spaces of separators (1 + 2)
    #   optional trailing "  {session}"    → +2 more when a session column fits
    separators = 3 + (2 if has_session_col else 0)
    safety_margin = 1  # avoid wrapping at the terminal edge
    title_w = max(1, tw - PREFIX_W - id_w - session_w - separators - safety_margin)

    # Grouping — compute groups keyed by group_by or None for ungrouped.
    def _project_key(t: Task, _r: _RenderedRow) -> str:
        return project_name_map.get(getattr(t, "project_id", "") or "", "(no project)")

    def _lifecycle_key(t: Task, _r: _RenderedRow) -> str:
        state = serialize_task_state(t)
        if state["is_closed"]:
            return "closed"
        return str(state["lifecycle_stage"] or "open")

    def _null_key(_t: Task, _r: _RenderedRow) -> str:
        return ""

    group_key: Callable[[Task, _RenderedRow], str]
    if group_by == "project":
        group_key = _project_key
    elif group_by == "lifecycle":
        group_key = _lifecycle_key
    else:
        group_key = _null_key

    groups: dict[str, list[tuple[Task, _RenderedRow]]] = {}
    group_order: list[str] = []
    for task, row in zip(tasks, rendered, strict=True):
        key = group_key(task, row)
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append((task, row))

    # When grouping by project and no explicit order, sort groups by the
    # most-recently-updated task in each group (descending).
    if group_by == "project":

        def _group_recency(key: str) -> Any:
            rows = groups[key]
            return max(
                (getattr(t, "updated_at", "") for t, _ in rows),
                default="",
            )

        group_order.sort(key=_group_recency, reverse=True)

    lines: list[str] = []
    for idx, key in enumerate(group_order):
        rows_in_group = groups[key]
        if group_by is not None:
            if idx > 0:
                lines.append("")
            header = f"{key} ({len(rows_in_group)})"
            lines.append(f"{_DIM_ANSI}{header}{_RESET_ANSI}")
        for _task, row in rows_in_group:
            lines.append(
                _render_row(
                    row,
                    id_w=id_w,
                    title_w=title_w,
                    session_w=session_w,
                )
            )

    return "\n".join(lines)


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

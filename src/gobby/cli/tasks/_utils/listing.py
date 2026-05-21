"""Block-formatter for task lists: assembles rendered rows into a single
compact text block with optional grouping and session column."""

from collections.abc import Callable
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.hub.runtime import open_runtime_hub_database
from gobby.storage.tasks import Task
from gobby.tasks.state_semantics import serialize_task_state

from .rendering import (
    _DIM_ANSI,
    _RESET_ANSI,
    COL_ID_MIN,
    COL_SESSION_MIN,
    PREFIX_W,
    _build_rendered_row,
    _get_term_width,
    _render_row,
    _RenderedRow,
    _resolve_project_names,
    _resolve_session_refs,
    _visual_width,
)


def format_task_list(
    tasks: list[Task],
    *,
    claimed_task_ids: set[str] | None = None,
    primary_ids: set[str] | None = None,
    tree_prefixes: dict[str, tuple[str, bool]] | None = None,
    group_by: str | None = None,
    term_width: int | None = None,
    db: HubDatabase | None = None,
) -> str:
    """Render a list of tasks as one compact block.

    Columns:
        ``[pri][stage][flags] #id  title  #session``

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
        group_by: ``"project"``, ``"stage"``, or ``None``.
        term_width: Terminal width override (for tests). Defaults to the live
            terminal size.
        db: Optional database handle for batch lookups. A fresh active hub
            handle is opened (and closed) if not supplied.

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

    owner_db: HubDatabase = db or open_runtime_hub_database(apply_migrations=False)
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

    def _stage_key(t: Task, _r: _RenderedRow) -> str:
        state = serialize_task_state(t)
        if state["is_closed"]:
            return "closed"
        current_stage = state["current_stage"]
        if not current_stage:
            return "ready"
        return f"{current_stage['name']}:{current_stage['state']}"

    def _null_key(_t: Task, _r: _RenderedRow) -> str:
        return ""

    group_key: Callable[[Task, _RenderedRow], str]
    if group_by == "project":
        group_key = _project_key
    elif group_by == "stage":
        group_key = _stage_key
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

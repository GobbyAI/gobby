"""Row-rendering primitives for the task CLI: constants, padding, and
single-row formatters used by :mod:`gobby.cli.tasks._utils.listing`."""

import logging
import shutil
from dataclasses import dataclass

from wcwidth import wcswidth

from gobby.storage.database import DatabaseProtocol, LocalDatabase
from gobby.storage.tasks import Task
from gobby.tasks.state_semantics import serialize_task_state

logger = logging.getLogger(__name__)


def pad_to_width(text: str, width: int) -> str:
    """Pad a string to a visual width, accounting for wide characters like emoji."""
    visual_width: int = wcswidth(text)
    if visual_width < 0:
        visual_width = len(text)  # Fallback if wcswidth fails
    padding: int = width - visual_width
    return text + " " * max(0, padding)


# Column widths for compact task table
COL_PRIORITY = 2  # Priority emoji (2 visual chars)
COL_STAGE_LETTER = 1  # Single-letter current-stage code
COL_FLAGS = 3  # Up to 3 flag letters (B/E/M)
COL_ID_MIN = 6  # #N format minimum (e.g., #1234)
COL_SESSION_MIN = 6  # #N format minimum for session refs
PREFIX_W = COL_PRIORITY + COL_STAGE_LETTER + COL_FLAGS  # 6 visual cols before #id
_DIM_ANSI = "\033[2m"
_RESET_ANSI = "\033[0m"

# Current stage state -> single-letter code (O/P/R/A/C)
_STAGE_STATE_LETTER: dict[str, str] = {
    "ready": "O",
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
    stage_letter: str
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
    current_stage = state["current_stage"]
    stage_state = current_stage["state"] if current_stage else "ready"
    blocked = state["is_blocked"]
    escalated = state["is_escalated"]
    closed = state["is_closed"]

    stage_letter = _STAGE_STATE_LETTER.get(stage_state, "?")
    if closed:
        stage_letter = "C"

    flags = ""
    if blocked:
        flags += "B"
    if escalated:
        flags += "E"

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
        stage_letter=stage_letter,
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
    stat = pad_to_width(row.stage_letter, COL_STAGE_LETTER)
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
    stat = pad_to_width("", COL_STAGE_LETTER)
    flags = pad_to_width("", COL_FLAGS)
    id_col = pad_to_width("#", COL_ID_MIN)
    return f"{pri}{stat}{flags} {id_col}  TITLE"

"""Deterministic persisted session-title helpers."""

from __future__ import annotations

PROVISIONAL_TITLE_SOURCE = "provisional"
TASK_TITLE_SOURCE = "task"
MANUAL_TITLE_SOURCE = "manual"


def manual_title_source(title: object) -> str | None:
    """Return the manual source marker for a nonblank explicit title."""
    return MANUAL_TITLE_SOURCE if isinstance(title, str) and title.strip() else None


def format_provisional_session_title(seq_num: int) -> str:
    """Return the deterministic title for a session without an open claim."""
    return f"(gobby): S#{seq_num}"


def format_task_session_title(seq_num: int, title: str) -> str:
    """Return the deterministic title for a successfully claimed task."""
    return f"(gobby): Task #{seq_num} - {title.strip()}"

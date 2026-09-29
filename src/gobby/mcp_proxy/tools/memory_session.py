"""Session and claimed-task resolution shared by the memory tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gobby.storage.session_resolution import resolve_session_reference

if TYPE_CHECKING:
    from gobby.storage.sessions import SessionManager

# ``{memory_id, task_id}`` records ``get_memory`` appends and the task review reads.
ACCESSED_MEMORY_IDS_VARIABLE = "accessed_memory_ids"


def resolve_session(
    session_manager: SessionManager,
    session_id: str,
) -> tuple[str, Any] | None:
    """Resolve a session UUID or reference to ``(session_id, session)``, or None."""
    session = session_manager.get(session_id)
    if session is not None:
        return str(session.id), session

    try:
        resolved_id = resolve_session_reference(session_manager.db, session_id)
    except ValueError:
        return None
    session = session_manager.get(resolved_id)
    return (resolved_id, session) if session is not None else None


def resolve_claimed_task_id(db: Any, session_id: str) -> str | None:
    """Return the session's most recently updated open claimed task, or None."""
    from gobby.storage.tasks import LocalTaskManager

    claimed = LocalTaskManager(db).list_tasks(
        claimed_by_session_id=session_id,
        closed=False,
        sort_by="updated_at",
        sort_order="desc",
    )
    return str(claimed[0].id) if claimed else None

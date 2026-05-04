"""Shared parent-session helpers for daemon-initiated agent launches."""

from __future__ import annotations

from typing import TYPE_CHECKING

from gobby.utils.machine_id import get_machine_id

if TYPE_CHECKING:
    from gobby.storage.sessions import SessionManager


def get_or_create_launcher_session(
    session_manager: SessionManager,
    project_id: str,
    source: str,
    title: str,
) -> str:
    """Return a persistent top-level launcher session id for a project/source pair."""
    sessions = session_manager.list(project_id=project_id, source=source)
    for session in sessions:
        if isinstance(session.id, str):
            return session.id

    created = session_manager.register(
        external_id=f"{source}-{project_id[:8]}",
        machine_id=get_machine_id() or source,
        source=source,
        project_id=project_id,
        title=title,
        agent_depth=0,
    )
    return str(created.id)

"""Shared parent-session helpers for daemon-initiated agent launches."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.storage.sessions import SessionManager


def get_or_create_launcher_session(
    session_manager: SessionManager,
    project_id: str,
    source: str,
) -> str:
    """Return a persistent top-level launcher session id for a project/source pair."""
    machine_id = require_machine_id()
    sessions = session_manager.list(
        project_id=project_id, source=source, machine_id=machine_id, status="active"
    )
    for session in sessions:
        session_manager.touch(session.id)
        return str(session.id)

    created = session_manager.register(
        # Expired terminal identities cannot be reactivated by registration.
        external_id=f"{source}-{project_id[:8]}-{uuid4()}",
        machine_id=machine_id,
        source=source,
        project_id=project_id,
        agent_depth=0,
    )
    return str(created.id)


async def aget_or_create_launcher_session(
    session_manager: SessionManager,
    project_id: str,
    source: str,
) -> str:
    """Async wrapper for launcher session lookup in request handlers."""
    return await asyncio.to_thread(
        get_or_create_launcher_session,
        session_manager,
        project_id,
        source,
    )

"""Unified session-variable default loading."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from gobby.storage.definitions import SessionVariableDefaultManager
from gobby.storage.hub.protocol import HubDatabase


def resolve_session_project_id(db: HubDatabase, session_id: str) -> str | None:
    """Return the session row's project_id, or None when missing."""
    if not session_id:
        return None
    try:
        UUID(session_id)
    except ValueError:
        return None
    row = db.fetchone("SELECT project_id FROM sessions WHERE id = %s", (session_id,))
    if row is None or row["project_id"] is None:
        return None
    return str(row["project_id"])


def load_variable_defaults(db: HubDatabase, project_id: str | None) -> dict[str, Any]:
    """Project-first defaults with global fallback, deduplicated by name."""
    return SessionVariableDefaultManager(db).get_defaults_map(project_id)


def merge_unloaded_variable_defaults(
    db: HubDatabase,
    session_id: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    """Defaults to merge when a session has not loaded presets yet."""
    if "_variable_defaults_loaded" in variables:
        return {}
    project_id = resolve_session_project_id(db, session_id)
    defaults = {
        key: value
        for key, value in load_variable_defaults(db, project_id).items()
        if key not in variables
    }
    defaults["_variable_defaults_loaded"] = True
    return defaults

"""Current-project scope resolution for the memory tool surface.

Both the read tools in `memory.py` and the write tools in `memory_write.py`
fence their operations on the caller's current project. This module owns that
resolution so neither tool module has to import the other.
"""

from __future__ import annotations

from typing import Any

from gobby.storage.projects import PERSONAL_PROJECT_ID


def get_current_project_id() -> str | None:
    """Get the current project ID from context, or None if not in a project."""
    from gobby.utils.project_context import get_project_context

    ctx = get_project_context()
    if ctx and ctx.get("id"):
        return str(ctx["id"])
    return None


def memory_owned_by_current_project(memory: Any) -> bool:
    """Return whether a memory belongs to the caller's current project."""
    if memory is None:
        return False
    current_project_id = get_current_project_id() or PERSONAL_PROJECT_ID
    return bool(memory.project_id == current_project_id)

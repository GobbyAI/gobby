"""User profile seeding for session start."""

from __future__ import annotations

from typing import Any

from gobby.storage.projects import personal_project_path

USER_PROFILE_FILENAME = "USER.md"


def read_user_profile_content() -> str:
    """Read the global user profile, returning an empty string when absent."""
    path = personal_project_path() / USER_PROFILE_FILENAME
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def seed_user_profile_content(handler: Any, session_id: str | None) -> None:
    """Persist the global user profile content into session variables."""
    if not session_id or handler._session_manager is None:
        return

    from gobby.workflows.state_manager import SessionVariableManager

    try:
        content = read_user_profile_content()
    except OSError as exc:
        handler.logger.warning("Failed to read global user profile: %s", exc)
        content = ""

    SessionVariableManager(handler._session_manager.db).merge_variables(
        session_id,
        {"user_profile_content": content},
    )

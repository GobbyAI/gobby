"""User profile seeding for session start."""

from __future__ import annotations

from typing import Any

from gobby.paths import FilesHomeNotOnThisDaemonError, require_files_home
from gobby.workflows.state_manager import SessionVariableManager

USER_PROFILE_FILENAME = "USER.md"


def read_user_profile_content() -> str:
    """Read the hub-owner profile from files_home. Missing file returns empty."""
    path = require_files_home() / USER_PROFILE_FILENAME
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def seed_user_profile_content(handler: Any, session_id: str | None) -> None:
    """Persist the global user profile content into session variables."""
    session_manager = handler.get_session_manager()
    if not session_id or session_manager is None:
        return

    try:
        content = read_user_profile_content()
    except FilesHomeNotOnThisDaemonError:
        content = ""
    except OSError as exc:
        handler.logger.warning("Failed to read global user profile: %s", exc)
        content = ""

    SessionVariableManager(session_manager.db).merge_variables(
        session_id,
        {"user_profile_content": content},
    )

"""Session storage package."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._constants import (
    LIVE_SESSION_STATUS_ORDER,
    LIVE_SESSION_STATUSES,
    PROTECTED_SESSION_STATUSES,
    SYSTEM_SESSION_PROJECT_ID,
    SYSTEM_SESSION_SOURCE,
    SYSTEM_SESSION_TITLE,
    TERMINAL_SESSION_STATUSES,
    ensure_system_session,
    logger,
    system_session_external_id,
    system_session_id,
)

if TYPE_CHECKING:
    from ._manager import SessionManager

__all__ = [
    "SessionManager",
    "LIVE_SESSION_STATUSES",
    "LIVE_SESSION_STATUS_ORDER",
    "PROTECTED_SESSION_STATUSES",
    "SYSTEM_SESSION_PROJECT_ID",
    "SYSTEM_SESSION_SOURCE",
    "SYSTEM_SESSION_TITLE",
    "TERMINAL_SESSION_STATUSES",
    "ensure_system_session",
    "logger",
    "system_session_external_id",
    "system_session_id",
]


def __getattr__(name: str) -> object:
    """Load the manager lazily so lifecycle helpers can import package constants."""
    if name != "SessionManager":
        raise AttributeError(name)
    from ._manager import SessionManager

    return SessionManager

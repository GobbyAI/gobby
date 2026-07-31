"""Session storage package."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._constants import (
    SYSTEM_SESSION_EXTERNAL_ID,
    SYSTEM_SESSION_ID,
    SYSTEM_SESSION_MACHINE_ID,
    SYSTEM_SESSION_PROJECT_ID,
    SYSTEM_SESSION_SOURCE,
    SYSTEM_SESSION_TITLE,
    TERMINAL_SESSION_STATUSES,
    ensure_system_session,
    logger,
)

if TYPE_CHECKING:
    from ._manager import SessionManager

__all__ = [
    "SessionManager",
    "SYSTEM_SESSION_EXTERNAL_ID",
    "SYSTEM_SESSION_ID",
    "SYSTEM_SESSION_MACHINE_ID",
    "SYSTEM_SESSION_PROJECT_ID",
    "SYSTEM_SESSION_SOURCE",
    "SYSTEM_SESSION_TITLE",
    "TERMINAL_SESSION_STATUSES",
    "ensure_system_session",
    "logger",
]


def __getattr__(name: str) -> object:
    """Load the manager lazily so lifecycle helpers can import package constants."""
    if name != "SessionManager":
        raise AttributeError(name)
    from ._manager import SessionManager

    return SessionManager

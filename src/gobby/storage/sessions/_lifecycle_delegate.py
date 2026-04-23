"""Lifecycle delegation mixin for session storage."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol


class _ManagerState(Protocol):
    db: DatabaseProtocol


class _LifecycleDelegateMixin:
    def expire_stale_sessions(self: _ManagerState, timeout_hours: int = 24) -> int:
        """Mark sessions as expired if inactive too long. Delegates to session_lifecycle."""
        from gobby.storage.session_lifecycle import expire_stale_sessions as _expire

        return _expire(self.db, timeout_hours)

    def expire_orphaned_handoff_sessions(self: _ManagerState, timeout_minutes: int = 30) -> int:
        """Expire orphaned handoff_ready sessions. Delegates to session_lifecycle."""
        from gobby.storage.session_lifecycle import (
            expire_orphaned_handoff_sessions as _expire_orphaned,
        )

        return _expire_orphaned(self.db, timeout_minutes)

    def pause_inactive_active_sessions(self: _ManagerState, timeout_minutes: int = 30) -> int:
        """Pause active sessions inactive too long. Delegates to session_lifecycle."""
        from gobby.storage.session_lifecycle import pause_inactive_active_sessions as _pause

        return _pause(self.db, timeout_minutes)

    def expire_empty_sessions(self: _ManagerState, timeout_hours: int = 2) -> int:
        """Fast-expire zero-message sessions. Delegates to session_lifecycle."""
        from gobby.storage.session_lifecycle import expire_empty_sessions as _expire_empty

        return _expire_empty(self.db, timeout_hours)

    def prune_empty_sessions(self: _ManagerState, min_age_hours: int = 1) -> int:
        """Hard-delete old expired zero-message sessions. Delegates to session_lifecycle."""
        from gobby.storage.session_lifecycle import prune_empty_sessions as _prune_empty

        return _prune_empty(self.db, min_age_hours)

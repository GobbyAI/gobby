"""Lifecycle delegation mixin for session storage."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from gobby.sessions.status_events import SessionStatusTransition

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.session_lifecycle import SessionStateCleanupResult


class _ManagerState(Protocol):
    db: HubDatabase

    def _notify_status_transition(self, transition: SessionStatusTransition) -> None: ...


class _LifecycleDelegateMixin:
    def expire_stale_sessions(self: _ManagerState, timeout_hours: int = 24) -> int:
        """Mark sessions as expired if inactive too long. Delegates to session_lifecycle."""
        from gobby.storage.session_lifecycle import expire_stale_sessions as _expire

        return _expire(
            self.db,
            timeout_hours,
            status_notifier=self._notify_status_transition,
        )

    def expire_orphaned_handoff_sessions(self: _ManagerState, timeout_minutes: int = 30) -> int:
        """Expire orphaned handoff_ready sessions. Delegates to session_lifecycle."""
        from gobby.storage.session_lifecycle import (
            expire_orphaned_handoff_sessions as _expire_orphaned,
        )

        return _expire_orphaned(
            self.db,
            timeout_minutes,
            status_notifier=self._notify_status_transition,
        )

    def prune_stale_compact_workflow_instances(
        self: _ManagerState, retention_hours: int = 24
    ) -> int:
        """Reclaim typed agent-step instances from unresumed compact sessions."""
        from gobby.storage.session_lifecycle import (
            prune_stale_compact_workflow_instances as _prune_compact,
        )

        return _prune_compact(self.db, retention_hours)

    def cleanup_expired_session_state(self: _ManagerState) -> SessionStateCleanupResult:
        """Clear state belonging to sessions past the revival horizon."""
        from gobby.storage.session_lifecycle import cleanup_expired_session_state

        return cleanup_expired_session_state(self.db)

    def pause_inactive_active_sessions(self: _ManagerState, timeout_minutes: int = 30) -> int:
        """Pause active sessions inactive too long. Delegates to session_lifecycle."""
        from gobby.storage.session_lifecycle import pause_inactive_active_sessions as _pause

        return _pause(
            self.db,
            timeout_minutes,
            status_notifier=self._notify_status_transition,
        )

    def expire_empty_sessions(self: _ManagerState, timeout_hours: int = 2) -> int:
        """Fast-expire zero-message sessions. Delegates to session_lifecycle."""
        from gobby.storage.session_lifecycle import expire_empty_sessions as _expire_empty

        return _expire_empty(
            self.db,
            timeout_hours,
            status_notifier=self._notify_status_transition,
        )

    def prune_empty_sessions(self: _ManagerState, min_age_hours: int = 1) -> int:
        """Hard-delete old expired zero-message sessions. Delegates to session_lifecycle."""
        from gobby.storage.session_lifecycle import prune_empty_sessions as _prune_empty

        from ._registration_cache import invalidate_session_caches

        pruned = _prune_empty(self.db, min_age_hours)
        if pruned:
            invalidate_session_caches(self)
        return pruned

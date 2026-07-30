"""In-memory activity timestamps used by session idle detection."""

from __future__ import annotations

from datetime import UTC, datetime
from threading import Lock

SESSION_ACTIVITY_TTL_SECONDS = 86_400
SESSION_ACTIVITY_PRUNE_INTERVAL_SECONDS = 300

_SESSION_ACTIVITY_TIMESTAMPS: dict[str, datetime] = {}
_SESSION_ACTIVITY_LOCK = Lock()
_LAST_PRUNE_AT: datetime | None = None


def record_session_activity(session_id: str, when: datetime | None = None) -> None:
    """Record an activity pulse for a platform session id."""
    if not session_id:
        return

    effective_when = when or datetime.now(UTC)
    with _SESSION_ACTIVITY_LOCK:
        _SESSION_ACTIVITY_TIMESTAMPS[session_id] = effective_when
    prune_trackers(effective_when)


def last_session_activity(session_id: str) -> datetime | None:
    """Return the most recent activity timestamp, if any."""
    with _SESSION_ACTIVITY_LOCK:
        return _SESSION_ACTIVITY_TIMESTAMPS.get(session_id)


def prune_trackers(now: datetime) -> None:
    """Drop activity timestamps that have exceeded the retention window."""
    global _LAST_PRUNE_AT

    with _SESSION_ACTIVITY_LOCK:
        if _LAST_PRUNE_AT is not None:
            elapsed = (now - _LAST_PRUNE_AT).total_seconds()
            if elapsed < SESSION_ACTIVITY_PRUNE_INTERVAL_SECONDS:
                return

        cutoff = now.timestamp() - SESSION_ACTIVITY_TTL_SECONDS
        stale_ids = [
            session_id
            for session_id, timestamp in _SESSION_ACTIVITY_TIMESTAMPS.items()
            if timestamp.timestamp() < cutoff
        ]
        for session_id in stale_ids:
            _SESSION_ACTIVITY_TIMESTAMPS.pop(session_id, None)
        _LAST_PRUNE_AT = now


def clear_trackers(session_id: str) -> None:
    """Remove a session's activity state on teardown."""
    with _SESSION_ACTIVITY_LOCK:
        _SESSION_ACTIVITY_TIMESTAMPS.pop(session_id, None)


def reset_for_tests() -> None:
    """Clear all activity state. Intended for test fixtures."""
    global _LAST_PRUNE_AT

    with _SESSION_ACTIVITY_LOCK:
        _SESSION_ACTIVITY_TIMESTAMPS.clear()
        _LAST_PRUNE_AT = None

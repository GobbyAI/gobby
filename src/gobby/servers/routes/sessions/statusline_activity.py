"""In-memory per-session trackers used by the statusline gap detector.

The statusline POST endpoint writes into `_STATUSLINE_LAST_SEEN` on every
request, while unrelated session activity (hook events, etc.) writes into
`_SESSION_ACTIVITY_LAST_SEEN`. The gap-warning logic compares the two to
decide whether a silent statusline is actually anomalous.

The module is intentionally lightweight so `gobby.hooks.hook_manager` can
import it without pulling the FastAPI server graph into the hooks runtime.
"""

from __future__ import annotations

from datetime import UTC, datetime

STATUSLINE_GAP_WARNING_THRESHOLD_MS = 120_000
STATUSLINE_LAST_SEEN_TTL_SECONDS = 86_400
STATUSLINE_PRUNE_INTERVAL_SECONDS = 300

_STATUSLINE_LAST_SEEN: dict[str, datetime] = {}
_SESSION_ACTIVITY_LAST_SEEN: dict[str, datetime] = {}
_LAST_PRUNE_AT: datetime | None = None


def record_statusline_seen(session_id: str, when: datetime) -> datetime | None:
    """Store the latest statusline POST time and return the previous value."""
    previous = _STATUSLINE_LAST_SEEN.get(session_id)
    _STATUSLINE_LAST_SEEN[session_id] = when
    return previous


def record_session_activity(session_id: str, when: datetime | None = None) -> None:
    """Record a non-statusline activity pulse for a platform session id."""
    if not session_id:
        return
    _SESSION_ACTIVITY_LAST_SEEN[session_id] = when or datetime.now(UTC)


def last_session_activity(session_id: str) -> datetime | None:
    """Return the most recent non-statusline activity timestamp, if any."""
    return _SESSION_ACTIVITY_LAST_SEEN.get(session_id)


def prune_trackers(now: datetime) -> None:
    """Drop stale entries from both trackers so they stay bounded."""
    global _LAST_PRUNE_AT

    if _LAST_PRUNE_AT is not None:
        elapsed = (now - _LAST_PRUNE_AT).total_seconds()
        if elapsed < STATUSLINE_PRUNE_INTERVAL_SECONDS:
            return

    cutoff = now.timestamp() - STATUSLINE_LAST_SEEN_TTL_SECONDS
    for tracker in (_STATUSLINE_LAST_SEEN, _SESSION_ACTIVITY_LAST_SEEN):
        stale_ids = [sid for sid, seen_at in tracker.items() if seen_at.timestamp() < cutoff]
        for sid in stale_ids:
            tracker.pop(sid, None)
    _LAST_PRUNE_AT = now


def clear_trackers(session_id: str) -> None:
    """Remove a session's entries from both trackers on teardown."""
    _STATUSLINE_LAST_SEEN.pop(session_id, None)
    _SESSION_ACTIVITY_LAST_SEEN.pop(session_id, None)


def reset_for_tests() -> None:
    """Clear all state. Intended for test fixtures."""
    global _LAST_PRUNE_AT
    _STATUSLINE_LAST_SEEN.clear()
    _SESSION_ACTIVITY_LAST_SEEN.clear()
    _LAST_PRUNE_AT = None

"""Atomic per-session state for statusline gap detection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock

STATUSLINE_GAP_OBSERVATION_THRESHOLD_MS = 120_000
STATUSLINE_GAP_WARNING_THRESHOLD_MS = 600_000
STATUSLINE_GAP_WARNING_THROTTLE_SECONDS = 3_600
STATUSLINE_LAST_SEEN_TTL_SECONDS = 86_400
STATUSLINE_PRUNE_INTERVAL_SECONDS = 300


@dataclass(slots=True)
class StatuslineActivityState:
    """Mutable statusline interval state guarded by the module lock."""

    previous_statusline: datetime | None = None
    first_activity_since_statusline: datetime | None = None
    last_activity_since_statusline: datetime | None = None
    last_warning_emitted: datetime | None = None


@dataclass(frozen=True, slots=True)
class StatuslineGapSnapshot:
    """Atomic snapshot of the interval ending at a statusline arrival."""

    previous_statusline: datetime | None
    first_activity_since_statusline: datetime | None
    last_activity_since_statusline: datetime | None


_STATUSLINE_ACTIVITY_STATES: dict[str, StatuslineActivityState] = {}
_STATUSLINE_ACTIVITY_LOCK = Lock()
_LAST_PRUNE_AT: datetime | None = None


def _state_for(session_id: str) -> StatuslineActivityState:
    state = _STATUSLINE_ACTIVITY_STATES.get(session_id)
    if state is None:
        state = StatuslineActivityState()
        _STATUSLINE_ACTIVITY_STATES[session_id] = state
    return state


def record_statusline_seen(session_id: str, when: datetime) -> StatuslineGapSnapshot:
    """Close the current activity interval and begin a new statusline interval."""
    with _STATUSLINE_ACTIVITY_LOCK:
        state = _state_for(session_id)
        snapshot = StatuslineGapSnapshot(
            previous_statusline=state.previous_statusline,
            first_activity_since_statusline=state.first_activity_since_statusline,
            last_activity_since_statusline=state.last_activity_since_statusline,
        )
        state.previous_statusline = when
        state.first_activity_since_statusline = None
        state.last_activity_since_statusline = None
    prune_trackers(when)
    return snapshot


def record_session_activity(session_id: str, when: datetime | None = None) -> None:
    """Record a non-statusline activity pulse for a platform session id."""
    if not session_id:
        return
    effective_when = when or datetime.now(UTC)
    with _STATUSLINE_ACTIVITY_LOCK:
        state = _state_for(session_id)
        if state.first_activity_since_statusline is None:
            state.first_activity_since_statusline = effective_when
        state.last_activity_since_statusline = effective_when
    prune_trackers(effective_when)


def last_session_activity(session_id: str) -> datetime | None:
    """Return the most recent non-statusline activity timestamp, if any."""
    with _STATUSLINE_ACTIVITY_LOCK:
        state = _STATUSLINE_ACTIVITY_STATES.get(session_id)
        return state.last_activity_since_statusline if state is not None else None


def should_emit_statusline_gap_warning(session_id: str, when: datetime) -> bool:
    """Atomically claim one warning per throttle window for a session."""
    with _STATUSLINE_ACTIVITY_LOCK:
        state = _state_for(session_id)
        if state.last_warning_emitted is not None:
            elapsed = (when - state.last_warning_emitted).total_seconds()
            if elapsed < STATUSLINE_GAP_WARNING_THROTTLE_SECONDS:
                return False
        state.last_warning_emitted = when
        return True


def prune_trackers(now: datetime) -> None:
    """Drop unified session states whose newest timestamp is stale."""
    global _LAST_PRUNE_AT

    with _STATUSLINE_ACTIVITY_LOCK:
        if _LAST_PRUNE_AT is not None:
            elapsed = (now - _LAST_PRUNE_AT).total_seconds()
            if elapsed < STATUSLINE_PRUNE_INTERVAL_SECONDS:
                return

        cutoff = now.timestamp() - STATUSLINE_LAST_SEEN_TTL_SECONDS
        stale_ids: list[str] = []
        for session_id, state in _STATUSLINE_ACTIVITY_STATES.items():
            timestamps = (
                state.previous_statusline,
                state.first_activity_since_statusline,
                state.last_activity_since_statusline,
                state.last_warning_emitted,
            )
            newest = max(
                (timestamp.timestamp() for timestamp in timestamps if timestamp), default=0
            )
            if newest < cutoff:
                stale_ids.append(session_id)
        for session_id in stale_ids:
            _STATUSLINE_ACTIVITY_STATES.pop(session_id, None)
        _LAST_PRUNE_AT = now


def clear_trackers(session_id: str) -> None:
    """Remove a session's unified state on teardown."""
    with _STATUSLINE_ACTIVITY_LOCK:
        _STATUSLINE_ACTIVITY_STATES.pop(session_id, None)


def reset_for_tests() -> None:
    """Clear all state. Intended for test fixtures."""
    global _LAST_PRUNE_AT
    with _STATUSLINE_ACTIVITY_LOCK:
        _STATUSLINE_ACTIVITY_STATES.clear()
        _LAST_PRUNE_AT = None

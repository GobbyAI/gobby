"""Shared helpers for event handler tests."""

from __future__ import annotations

from datetime import UTC, datetime

from gobby.hooks.events import HookEvent, HookEventType, SessionSource


def make_event(
    event_type: HookEventType,
    session_id: str = "test-session",
    source: str = "claude",
    data: dict | None = None,
    metadata: dict | None = None,
) -> HookEvent:
    """Create a HookEvent with default test fields."""
    return HookEvent(
        event_type=event_type,
        session_id=session_id,
        source=SessionSource(source),
        timestamp=datetime.now(UTC),
        data=data or {},
        metadata=metadata or {},
    )

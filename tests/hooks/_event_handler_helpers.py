"""Shared helpers for event handler tests."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from gobby.hooks.events import HookEvent, HookEventType, SessionSource


def empty_database_mock() -> MagicMock:
    """Mock hub database whose reads behave like an empty database.

    Hook paths construct real storage managers (LocalMachineManager,
    LocalAgentRunManager, SessionVariableManager, config store) around the
    mocked database; a bare MagicMock row leaks into typed row parsing where
    parse_stored_datetime/json.loads raise TypeError on MagicMock values.
    Empty-read defaults keep those managers on their row-is-None branches.
    Tests that need rows override the return values with real dicts.
    """
    db = MagicMock()
    db.fetchone.return_value = None
    db.fetchall.return_value = []
    return db


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

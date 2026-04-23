"""Focused constructor coverage for EventHandlers session-manager aliases."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.hooks.event_handlers import EventHandlers

pytestmark = pytest.mark.unit


def test_session_storage_alias_is_used_when_session_manager_missing() -> None:
    session_storage = MagicMock()

    handlers = EventHandlers(session_storage=session_storage)

    assert handlers._session_manager is session_storage


def test_conflicting_session_manager_aliases_raise_value_error() -> None:
    with pytest.raises(ValueError, match="must reference the same object"):
        EventHandlers(
            session_manager=MagicMock(),
            session_storage=MagicMock(),
        )

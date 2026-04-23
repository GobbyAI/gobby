
"""Shared fixtures for event handler tests."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookResponse


@pytest.fixture
def mock_dependencies() -> dict[str, Any]:
    """Create mock dependencies for EventHandlers."""
    workflow_handler = MagicMock()
    workflow_handler.evaluate.return_value = HookResponse(decision="allow", context="")
    session_storage = MagicMock()
    session_manager = session_storage
    session_storage.find_parent.return_value = None
    session_storage.update.return_value = None
    return {
        "session_manager": session_manager,
        "workflow_handler": workflow_handler,
        "session_storage": session_storage,
        "message_processor": MagicMock(),
        "task_manager": MagicMock(),
        "worktree_manager": MagicMock(),
        "session_coordinator": MagicMock(),
        "logger": logging.getLogger("test"),
    }


@pytest.fixture
def event_handlers(mock_dependencies: dict[str, Any]) -> EventHandlers:
    """Create EventHandlers instance with mocks."""
    return EventHandlers(**mock_dependencies)

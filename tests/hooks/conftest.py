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
    session_storage.backfill_terminal_context.return_value = (None, False)
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
def mock_empty_session_variable_manager(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch session variables for session-start routing tests that do not persist them."""
    manager = MagicMock()
    manager.get_variables.return_value = {}
    manager.merge_variables.return_value = True
    manager.claim_startup_context.return_value = "full"

    manager_cls = MagicMock(return_value=manager)
    monkeypatch.setattr("gobby.workflows.state_manager.SessionVariableManager", manager_cls)
    return manager


@pytest.fixture
def event_handlers(mock_dependencies: dict[str, Any]) -> EventHandlers:
    """Create EventHandlers instance with mocks."""
    return EventHandlers(**mock_dependencies)

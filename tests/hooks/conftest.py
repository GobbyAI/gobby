"""Shared fixtures for event handler tests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookResponse
from gobby.hooks.hook_manager import HookManager
from tests.conftest import _ensure_isolated_bootstrap

from ._event_handler_helpers import empty_database_mock


@pytest.fixture(autouse=True)
def _hook_files_home() -> None:
    """Hook modules always have a files_home even when GOBBY_HOME is isolated."""
    _ensure_isolated_bootstrap()


@pytest.fixture
def mock_dependencies() -> dict[str, Any]:
    """Create mock dependencies for EventHandlers."""
    workflow_handler = MagicMock()
    workflow_handler.evaluate.return_value = HookResponse(decision="allow", context="")
    session_storage = MagicMock()
    session_manager = session_storage
    session_storage.find_parent.return_value = None
    session_storage.find_by_external_id.return_value = None
    session_storage.find_by_external_id_any_project.return_value = None
    session_storage.update.return_value = None
    session_storage.backfill_terminal_context.return_value = (None, False)
    message_processor = MagicMock()

    def resolve_message_processor() -> MagicMock:
        return message_processor

    return {
        "session_manager": session_manager,
        "workflow_handler": workflow_handler,
        "session_storage": session_storage,
        "message_processor_resolver": resolve_message_processor,
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


@pytest.fixture
def mock_components() -> MagicMock:
    """Create a mock HookManagerFactory components object."""
    components = MagicMock()
    components.config = MagicMock()
    components.database = empty_database_mock()
    components.daemon_client = MagicMock()
    components.transcript_processor = MagicMock()
    components.session_task_manager = MagicMock()
    components.memory_storage = MagicMock()
    components.message_manager = MagicMock()
    components.task_manager = MagicMock()
    components.agent_run_manager = MagicMock()
    components.worktree_manager = MagicMock()
    components.stop_registry = MagicMock()
    components.progress_tracker = MagicMock()
    components.stuck_detector = MagicMock()
    components.memory_manager = MagicMock()
    components.workflow_loader = MagicMock()
    components.skill_manager = MagicMock()
    components.pipeline_executor = MagicMock()
    components.workflow_handler = MagicMock()
    components.webhook_dispatcher = MagicMock()
    components.webhook_dispatcher.config = MagicMock()
    components.webhook_dispatcher.config.enabled = False
    components.session_manager = MagicMock()
    components.session_coordinator = MagicMock()
    components.health_monitor = MagicMock()
    components.event_handlers = MagicMock()
    return components


@pytest.fixture
def manager_with_mocks(mock_components: MagicMock) -> Iterator[HookManager]:
    """Create a HookManager with all subsystems mocked."""
    hook_asyncio = MagicMock(wraps=asyncio)
    hook_asyncio.get_running_loop.side_effect = RuntimeError
    with (
        patch("gobby.hooks.hook_manager.HookManagerFactory") as mock_factory,
        patch("gobby.hooks.hook_manager.asyncio", hook_asyncio),
        patch("gobby.hooks.event_enrichment.EventEnricher"),
        patch("gobby.hooks.session_lookup.SessionLookupService"),
        patch("gobby.storage.inter_session_messages.InterSessionMessageManager"),
    ):
        mock_factory.create.return_value = mock_components
        manager = HookManager(
            daemon_host="localhost",
            daemon_port=60887,
        )
        # Pre-warm health monitor cache
        manager._health_monitor.get_cached_status.return_value = (True, "ready", "ready", None)
        manager._health_monitor.check_now.return_value = True
        yield manager

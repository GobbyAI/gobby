"""Shared fixtures for adapter tests."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from gobby.adapters.gemini import GeminiAdapter
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager

pytestmark = pytest.mark.unit


@pytest.fixture
def adapter() -> GeminiAdapter:
    """Create a GeminiAdapter instance."""
    return GeminiAdapter()


@pytest.fixture
def mock_hook_manager() -> MagicMock:
    """Create a mock HookManager with an allow response."""
    manager = MagicMock()
    manager.handle.return_value = HookResponse(decision="allow")
    return manager


@pytest.fixture
def mock_components() -> MagicMock:
    components = MagicMock()
    components.config = MagicMock()
    components.database = MagicMock()
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
    components.hook_assembler = MagicMock()
    components.event_handlers = MagicMock()
    return components


@pytest.fixture
def manager_with_mocks(mock_components: MagicMock) -> Iterator[HookManager]:
    with (
        patch("gobby.hooks.hook_manager.HookManagerFactory") as mock_factory,
        patch("gobby.hooks.hook_manager.asyncio.get_running_loop", side_effect=RuntimeError),
        patch("gobby.hooks.event_enrichment.EventEnricher"),
        patch("gobby.hooks.session_lookup.SessionLookupService"),
        patch("gobby.storage.inter_session_messages.InterSessionMessageManager"),
    ):
        mock_factory.create.return_value = mock_components
        manager = HookManager(
            daemon_host="localhost",
            daemon_port=60887,
            log_file="/tmp/test-adapters-hook-manager.log",
        )
        manager._health_monitor.get_cached_status.return_value = (True, "ready", "ready", None)
        manager._health_monitor.check_now.return_value = True
        yield manager


@pytest.fixture
def make_before_tool_event() -> Callable[[dict], HookEvent]:
    def _make(tool_input: dict) -> HookEvent:
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-external-id",
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={"tool_name": "mcp__gobby__call_tool", "tool_input": tool_input},
            machine_id="test-machine",
        )
        # HookEvent does not accept project_id in __init__; set it as an attribute.
        event.project_id = "proj-1"
        return event

    return _make

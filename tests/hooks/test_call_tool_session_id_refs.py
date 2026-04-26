"""Regression tests for call_tool session_id reference handling."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager

pytestmark = pytest.mark.unit


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
            log_file="/tmp/test-call-tool-session-id-refs.log",
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
        event.project_id = "proj-1"
        return event

    return _make


def _prepare_manager_for_before_tool(manager: HookManager) -> None:
    manager._event_handlers.get_handler.return_value = MagicMock(
        return_value=HookResponse(decision="allow")
    )
    manager._session_lookup.resolve.return_value = None
    manager._workflow_handler.handle.return_value = HookResponse(decision="allow")
    manager._enricher.enrich = MagicMock()


def test_top_level_call_tool_session_ref_does_not_create_modified_input(
    manager_with_mocks: HookManager,
    make_before_tool_event: Callable[[dict], HookEvent],
) -> None:
    manager = manager_with_mocks
    _prepare_manager_for_before_tool(manager)
    manager._session_manager.resolve_session_reference.return_value = "wrapper-session-uuid"
    event = make_before_tool_event(
        {
            "server_name": "gobby-sessions",
            "tool_name": "get_session",
            "arguments": {},
            "session_id": "#3",
        }
    )

    response = manager._handle_internal(event)

    assert event.data["tool_input"]["session_id"] == "wrapper-session-uuid"
    assert response.modified_input is None
    assert response.auto_approve is False


def test_nested_call_tool_session_ref_creates_modified_input(
    manager_with_mocks: HookManager,
    make_before_tool_event: Callable[[dict], HookEvent],
) -> None:
    manager = manager_with_mocks
    _prepare_manager_for_before_tool(manager)
    manager._session_manager.resolve_session_reference.return_value = "target-session-uuid"
    event = make_before_tool_event(
        {
            "server_name": "gobby-sessions",
            "tool_name": "get_session",
            "arguments": {"session_id": "#3"},
        }
    )

    response = manager._handle_internal(event)

    assert response.modified_input == {
        "server_name": "gobby-sessions",
        "tool_name": "get_session",
        "arguments": {"session_id": "target-session-uuid"},
    }
    assert response.auto_approve is True


def test_top_level_set_variable_preserves_session_ref(
    manager_with_mocks: HookManager,
) -> None:
    manager = manager_with_mocks
    _prepare_manager_for_before_tool(manager)
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="test-external-id",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__set_variable",
            "tool_input": {
                "name": "loaded_skills",
                "value": "brevity",
                "session_id": "#3",
            },
        },
        machine_id="test-machine",
    )
    event.project_id = "proj-1"

    response = manager._handle_internal(event)

    assert event.data["tool_input"]["session_id"] == "#3"
    assert response.modified_input is None
    manager._session_manager.resolve_session_reference.assert_not_called()

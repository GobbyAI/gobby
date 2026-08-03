"""Regression tests for call_tool session_id reference handling."""

from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager

pytestmark = pytest.mark.unit


# mock_components and manager_with_mocks come from tests/hooks/conftest.py.


@pytest.fixture
def make_before_tool_event() -> Callable[[dict], HookEvent]:
    def _make(tool_input: dict) -> HookEvent:
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-external-id",
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={"tool_name": "mcp__gobby__call_tool", "tool_input": tool_input},
            machine_id="21000000-0000-4000-8000-000000000002",
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


def test_nested_call_tool_session_ref_does_not_create_modified_input(
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

    assert event.data["tool_input"] == {
        "server_name": "gobby-sessions",
        "tool_name": "get_session",
        "arguments": {"session_id": "target-session-uuid"},
    }
    assert response.modified_input is None
    assert response.auto_approve is False
    assert "_session_refs_resolved" not in event.metadata


def test_top_level_list_tools_session_ref_does_not_create_modified_input(
    manager_with_mocks: HookManager,
) -> None:
    manager = manager_with_mocks
    _prepare_manager_for_before_tool(manager)
    manager._session_manager.resolve_session_reference.return_value = "wrapper-session-uuid"
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="test-external-id",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__list_tools",
            "tool_input": {
                "server_name": "gobby-tasks",
                "session_id": "#3",
            },
        },
        machine_id="21000000-0000-4000-8000-000000000002",
    )
    event.project_id = "proj-1"

    response = manager._handle_internal(event)

    assert event.data["tool_input"]["session_id"] == "wrapper-session-uuid"
    assert response.modified_input is None
    assert response.auto_approve is False
    assert "_session_refs_resolved" not in event.metadata


def test_no_session_ref_leaves_response_unmodified(
    manager_with_mocks: HookManager,
    make_before_tool_event: Callable[[dict], HookEvent],
) -> None:
    manager = manager_with_mocks
    _prepare_manager_for_before_tool(manager)
    event = make_before_tool_event(
        {
            "server_name": "gobby-sessions",
            "tool_name": "get_session",
            "arguments": {"session_id": "target-session-uuid"},
        }
    )

    response = manager._handle_internal(event)

    assert event.data["tool_input"]["arguments"]["session_id"] == "target-session-uuid"
    assert response.modified_input is None
    assert response.auto_approve is False
    assert "_session_refs_resolved" not in event.metadata


def test_session_ref_resolution_does_not_set_metadata_flag(
    manager_with_mocks: HookManager,
    make_before_tool_event: Callable[[dict], HookEvent],
) -> None:
    manager = manager_with_mocks
    manager._session_manager.resolve_session_reference.return_value = "target-session-uuid"
    event = make_before_tool_event(
        {
            "server_name": "gobby-sessions",
            "tool_name": "get_session",
            "arguments": {"session_id": "#3"},
        }
    )

    manager._resolve_session_refs_in_tool_input(event)

    assert event.data["tool_input"]["arguments"]["session_id"] == "target-session-uuid"
    assert "_session_refs_resolved" not in event.metadata


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
        machine_id="21000000-0000-4000-8000-000000000002",
    )
    event.project_id = "proj-1"

    response = manager._handle_internal(event)

    assert event.data["tool_input"]["session_id"] == "#3"
    assert response.modified_input is None
    manager._session_manager.resolve_session_reference.assert_not_called()

"""
Tests for EventHandlers module (TDD green phase).

Test categories:
1. Handler registration and lookup
2. Individual event type handlers (15 total)
3. Handler execution order
4. Error isolation
5. Context passing
6. Return value handling
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource

pytestmark = pytest.mark.unit

if TYPE_CHECKING:
    pass


def make_event(
    event_type: HookEventType,
    session_id: str = "test-session",
    source: str = "claude",
    data: dict | None = None,
    metadata: dict | None = None,
) -> HookEvent:
    """Helper to create HookEvent with required fields."""
    return HookEvent(
        event_type=event_type,
        session_id=session_id,
        source=SessionSource(source),
        timestamp=datetime.now(UTC),
        data=data or {},
        metadata=metadata or {},
    )


@pytest.fixture
def mock_dependencies() -> dict[str, Any]:
    """Create mock dependencies for EventHandlers."""
    # Configure workflow_handler to return a proper HookResponse
    workflow_handler = MagicMock()
    workflow_handler.evaluate.return_value = HookResponse(decision="allow", context="")
    session_storage = MagicMock()
    # Default: no handoff parent found (tests that need one override this)
    session_storage.find_parent.return_value = None
    return {
        "session_manager": MagicMock(),
        "workflow_handler": workflow_handler,
        "session_storage": session_storage,
        "message_processor": MagicMock(),
        "task_manager": MagicMock(),
        "session_coordinator": MagicMock(),
        "logger": logging.getLogger("test"),
    }


@pytest.fixture
def event_handlers(mock_dependencies: dict[str, Any]) -> EventHandlers:
    """Create EventHandlers instance with mocks."""
    return EventHandlers(**mock_dependencies)


class TestHandlerRegistration:
    """Test handler registration and lookup."""

    def test_all_event_types_have_handlers(self, event_handlers: EventHandlers) -> None:
        """Test that all HookEventType values have registered handlers."""
        for event_type in HookEventType:
            handler = event_handlers.get_handler(event_type)
            assert handler is not None, f"No handler for {event_type}"
            assert callable(handler)

    def test_get_handler_returns_callable(self, event_handlers: EventHandlers) -> None:
        """Test get_handler returns a callable."""
        handler = event_handlers.get_handler(HookEventType.SESSION_START)
        assert callable(handler)

    def test_get_handler_for_unknown_returns_none(self, event_handlers: EventHandlers) -> None:
        """Test get_handler returns None for unknown event type."""
        result = event_handlers.get_handler("invalid_event")  # type: ignore
        assert result is None

    def test_handler_map_is_immutable(self, event_handlers: EventHandlers) -> None:
        """Test handler map cannot be modified externally."""
        handler_map = event_handlers.get_handler_map()
        original_count = len(handler_map)
        handler_map["fake"] = lambda x: x
        assert len(event_handlers.get_handler_map()) == original_count


class TestSessionHandlers:
    """Test SESSION_START and SESSION_END handlers."""

    def test_session_start_allows(
        self, event_handlers: EventHandlers, mock_dependencies: dict
    ) -> None:
        """Test SESSION_START handler allows by default."""
        event = make_event(HookEventType.SESSION_START, session_id="ext-123")
        response = event_handlers.handle_session_start(event)
        assert response.decision == "allow"

    def test_session_end_allows(self, event_handlers: EventHandlers) -> None:
        """Test SESSION_END handler allows by default."""
        event = make_event(
            HookEventType.SESSION_END,
            metadata={"_platform_session_id": "plat-123"},
        )
        response = event_handlers.handle_session_end(event)
        assert response.decision == "allow"


class TestAgentHandlers:
    """Test BEFORE_AGENT and AFTER_AGENT handlers."""

    def test_before_agent_allows(self, event_handlers: EventHandlers) -> None:
        """Test BEFORE_AGENT allows by default."""
        event = make_event(
            HookEventType.BEFORE_AGENT,
            data={"prompt": "Hello"},
            metadata={"_platform_session_id": "plat-123"},
        )
        response = event_handlers.handle_before_agent(event)
        assert response.decision == "allow"

    def test_after_agent_allows(self, event_handlers: EventHandlers) -> None:
        """Test AFTER_AGENT allows by default."""
        event = make_event(
            HookEventType.AFTER_AGENT,
            metadata={"_platform_session_id": "plat-123"},
        )
        response = event_handlers.handle_after_agent(event)
        assert response.decision == "allow"


class TestToolHandlers:
    """Test BEFORE_TOOL and AFTER_TOOL handlers."""

    def test_before_tool_allows(self, event_handlers: EventHandlers) -> None:
        """Test BEFORE_TOOL allows by default."""
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Read"},
            metadata={"_platform_session_id": "plat-123"},
        )
        response = event_handlers.handle_before_tool(event)
        assert response.decision == "allow"

    def test_after_tool_allows(self, event_handlers: EventHandlers) -> None:
        """Test AFTER_TOOL allows by default."""
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Read"},
            metadata={"_platform_session_id": "plat-123"},
        )
        response = event_handlers.handle_after_tool(event)
        assert response.decision == "allow"


class TestOtherHandlers:
    """Test remaining event handlers."""

    def test_stop_allows(self, event_handlers: EventHandlers) -> None:
        """Test STOP allows by default."""
        event = make_event(HookEventType.STOP)
        response = event_handlers.handle_stop(event)
        assert response.decision == "allow"

    def test_pre_compact_allows(self, event_handlers: EventHandlers) -> None:
        """Test PRE_COMPACT allows by default."""
        event = make_event(HookEventType.PRE_COMPACT)
        response = event_handlers.handle_pre_compact(event)
        assert response.decision == "allow"

    def test_subagent_start_allows(self, event_handlers: EventHandlers) -> None:
        """Test SUBAGENT_START allows by default."""
        event = make_event(HookEventType.SUBAGENT_START, data={"subagent_id": "sub-1"})
        response = event_handlers.handle_subagent_start(event)
        assert response.decision == "allow"

    def test_subagent_stop_allows(self, event_handlers: EventHandlers) -> None:
        """Test SUBAGENT_STOP allows by default."""
        event = make_event(HookEventType.SUBAGENT_STOP, data={"subagent_id": "sub-1"})
        response = event_handlers.handle_subagent_stop(event)
        assert response.decision == "allow"

    def test_notification_allows(self, event_handlers: EventHandlers) -> None:
        """Test NOTIFICATION allows by default."""
        event = make_event(HookEventType.NOTIFICATION, data={"message": "test"})
        response = event_handlers.handle_notification(event)
        assert response.decision == "allow"

    def test_permission_request_allows(self, event_handlers: EventHandlers) -> None:
        """Test PERMISSION_REQUEST allows by default."""
        event = make_event(HookEventType.PERMISSION_REQUEST, data={"permission": "write"})
        response = event_handlers.handle_permission_request(event)
        assert response.decision == "allow"


class TestGeminiOnlyHandlers:
    """Test Gemini-only event handlers."""

    def test_before_tool_selection_allows(self, event_handlers: EventHandlers) -> None:
        """Test BEFORE_TOOL_SELECTION allows (Gemini only)."""
        event = make_event(HookEventType.BEFORE_TOOL_SELECTION, source="gemini")
        response = event_handlers.handle_before_tool_selection(event)
        assert response.decision == "allow"

    def test_before_model_allows(self, event_handlers: EventHandlers) -> None:
        """Test BEFORE_MODEL allows (Gemini only)."""
        event = make_event(HookEventType.BEFORE_MODEL, source="gemini")
        response = event_handlers.handle_before_model(event)
        assert response.decision == "allow"

    def test_after_model_allows(self, event_handlers: EventHandlers) -> None:
        """Test AFTER_MODEL allows (Gemini only)."""
        event = make_event(HookEventType.AFTER_MODEL, source="gemini")
        response = event_handlers.handle_after_model(event)
        assert response.decision == "allow"


class TestErrorIsolation:
    """Test handler error isolation."""

    def test_workflow_error_handled(
        self, event_handlers: EventHandlers, mock_dependencies: dict
    ) -> None:
        """Test workflow errors are handled gracefully."""
        mock_dependencies["workflow_handler"].evaluate.side_effect = Exception("Workflow error")
        event = make_event(HookEventType.BEFORE_AGENT, data={"prompt": "Hello"})
        response = event_handlers.handle_before_agent(event)
        assert response.decision in ("allow", "block")

    def test_missing_metadata_handled(self, event_handlers: EventHandlers) -> None:
        """Test missing metadata is handled gracefully."""
        event = make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Read"})
        response = event_handlers.handle_before_tool(event)
        assert response.decision in ("allow", "block")


class TestReturnValues:
    """Test handler return values."""

    def test_returns_hook_response(self, event_handlers: EventHandlers) -> None:
        """Test handlers return HookResponse."""
        event = make_event(HookEventType.BEFORE_AGENT, data={"prompt": "Hello"})
        response = event_handlers.handle_before_agent(event)
        assert isinstance(response, HookResponse)
        assert hasattr(response, "decision")
        assert hasattr(response, "context")

    def test_context_is_string(self, event_handlers: EventHandlers) -> None:
        """Test context is always a string."""
        event = make_event(HookEventType.SESSION_START)
        response = event_handlers.handle_session_start(event)
        assert isinstance(response.context, str)


class TestEventHandlersInit:
    """Test EventHandlers initialization."""

    def test_init_creates_logger(self) -> None:
        """Test init creates logger if not provided."""
        handlers = EventHandlers()
        assert handlers.logger is not None

    def test_init_with_dependencies(self, mock_dependencies: dict) -> None:
        """Test init with dependencies."""
        handlers = EventHandlers(**mock_dependencies)
        assert handlers._session_manager is mock_dependencies["session_manager"]

    def test_init_default_get_machine_id(self) -> None:
        """Test default get_machine_id function returns unknown-machine."""
        handlers = EventHandlers()
        assert handlers._get_machine_id() == "unknown-machine"

    def test_init_default_resolve_project_id(self) -> None:
        """Test default resolve_project_id function returns project_id or empty string."""
        handlers = EventHandlers()
        assert handlers._resolve_project_id("proj-123", None) == "proj-123"
        assert handlers._resolve_project_id(None, "/some/path") == ""

    def test_init_custom_get_machine_id(self) -> None:
        """Test custom get_machine_id function is used."""
        handlers = EventHandlers(get_machine_id=lambda: "custom-machine")
        assert handlers._get_machine_id() == "custom-machine"

    def test_init_custom_resolve_project_id(self) -> None:
        """Test custom resolve_project_id function is used."""
        handlers = EventHandlers(resolve_project_id=lambda p, c: f"resolved-{p or 'none'}")
        assert handlers._resolve_project_id("proj-1", None) == "resolved-proj-1"


class TestSessionStartPreCreatedSession:
    """Test SESSION_START handling for pre-created sessions (terminal mode agents)."""

    def test_pre_created_session_found_and_updated(self, mock_dependencies: dict) -> None:
        """Test pre-created session is found and updated."""
        # Create a mock session object
        mock_session = MagicMock()
        mock_session.id = "sess-pre-123"
        mock_session.project_id = "proj-123"
        mock_session.parent_session_id = None
        mock_session.agent_depth = 0
        mock_session.agent_run_id = None

        # Configure session_storage.get to return the session
        mock_dependencies["session_storage"].get.return_value = mock_session

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="sess-pre-123",
            data={"transcript_path": "/path/to/transcript.jsonl", "cwd": "/some/dir"},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        assert response.metadata.get("is_pre_created") is True
        assert response.metadata.get("session_id") == "sess-pre-123"
        mock_dependencies["session_storage"].update.assert_called_once()

    def test_pre_created_session_with_parent(self, mock_dependencies: dict) -> None:
        """Test pre-created session with parent session ID includes parent context."""
        mock_session = MagicMock()
        mock_session.id = "sess-child-123"
        mock_session.project_id = "proj-123"
        mock_session.parent_session_id = "sess-parent-456"
        mock_session.agent_depth = 1
        mock_session.agent_run_id = None

        mock_dependencies["session_storage"].get.return_value = mock_session

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="sess-child-123",
            data={"transcript_path": "/path/to/transcript.jsonl"},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        # Parent session info in context and metadata
        assert "Parent session: sess-parent-456" in response.context
        assert response.metadata["parent_session_id"] == "sess-parent-456"
        assert response.metadata.get("is_pre_created") is True

    def test_pre_created_session_with_agent_run_id(self, mock_dependencies: dict) -> None:
        """Test pre-created session with agent_run_id starts the agent run."""
        mock_session = MagicMock()
        mock_session.id = "sess-agent-123"
        mock_session.project_id = "proj-123"
        mock_session.parent_session_id = None
        mock_session.agent_depth = 0
        mock_session.agent_run_id = "run-456"

        mock_dependencies["session_storage"].get.return_value = mock_session

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="sess-agent-123",
            data={"transcript_path": "/path/to/transcript.jsonl"},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_dependencies["session_coordinator"].start_agent_run.assert_called_once_with("run-456")

    def test_pre_created_session_agent_run_start_error(self, mock_dependencies: dict) -> None:
        """Test error starting agent run is handled gracefully."""
        mock_session = MagicMock()
        mock_session.id = "sess-agent-123"
        mock_session.project_id = "proj-123"
        mock_session.parent_session_id = None
        mock_session.agent_depth = 0
        mock_session.agent_run_id = "run-456"

        mock_dependencies["session_storage"].get.return_value = mock_session
        mock_dependencies["session_coordinator"].start_agent_run.side_effect = Exception(
            "Failed to start"
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="sess-agent-123",
            data={"transcript_path": "/path/to/transcript.jsonl"},
        )

        response = handlers.handle_session_start(event)

        # Should still allow despite error
        assert response.decision == "allow"

    def test_pre_created_session_registers_with_message_processor(
        self, mock_dependencies: dict
    ) -> None:
        """Test pre-created session registers with message processor."""
        mock_session = MagicMock()
        mock_session.id = "sess-123"
        mock_session.project_id = "proj-123"
        mock_session.parent_session_id = None
        mock_session.agent_depth = 0
        mock_session.agent_run_id = None

        mock_dependencies["session_storage"].get.return_value = mock_session

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="sess-123",
            data={"transcript_path": "/path/to/transcript.jsonl"},
        )

        handlers.handle_session_start(event)

        mock_dependencies["message_processor"].register_session.assert_called_once_with(
            "sess-123", "/path/to/transcript.jsonl", source="claude"
        )

    def test_pre_created_session_message_processor_error(self, mock_dependencies: dict) -> None:
        """Test error registering with message processor is handled gracefully."""
        mock_session = MagicMock()
        mock_session.id = "sess-123"
        mock_session.project_id = "proj-123"
        mock_session.parent_session_id = None
        mock_session.agent_depth = 0
        mock_session.agent_run_id = None

        mock_dependencies["session_storage"].get.return_value = mock_session
        mock_dependencies["message_processor"].register_session.side_effect = Exception(
            "Registration failed"
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="sess-123",
            data={"transcript_path": "/path/to/transcript.jsonl"},
        )

        response = handlers.handle_session_start(event)

        # Should still allow despite error
        assert response.decision == "allow"

    def test_pre_created_session_coordinator_error(self, mock_dependencies: dict) -> None:
        """Test error registering session with coordinator is handled."""
        mock_session = MagicMock()
        mock_session.id = "sess-123"
        mock_session.project_id = "proj-123"
        mock_session.parent_session_id = None
        mock_session.agent_depth = 0
        mock_session.agent_run_id = None

        mock_dependencies["session_storage"].get.return_value = mock_session
        mock_dependencies["session_coordinator"].register_session.side_effect = Exception(
            "Coordinator error"
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="sess-123",
            data={"transcript_path": "/path/to/transcript.jsonl"},
        )

        response = handlers.handle_session_start(event)

        # Should still allow despite error
        assert response.decision == "allow"


class TestSessionStartNewSession:
    """Test SESSION_START handling for new sessions."""

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_new_session_with_parent_on_handoff(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict
    ) -> None:
        """Test new session finds parent when source is 'clear'."""
        mock_sv_mgr_cls.return_value = MagicMock(get_variables=MagicMock(return_value={}))

        mock_parent = MagicMock()
        mock_parent.id = "parent-sess-123"

        # No pre-created session found
        mock_dependencies["session_storage"].get.return_value = None
        mock_dependencies["session_storage"].find_parent.return_value = mock_parent
        mock_dependencies["session_manager"].register_session.return_value = "new-sess-456"

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={"source": "clear", "cwd": "/some/dir"},
            metadata={},
        )
        event.machine_id = "machine-123"

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        assert "Parent session: parent-sess-123" in response.context
        mock_dependencies["session_storage"].find_parent.assert_called_once()
        mock_dependencies["session_manager"].mark_session_expired.assert_called_once_with(
            "parent-sess-123"
        )

    def test_startup_session_does_not_adopt_stale_parent(self, mock_dependencies: dict) -> None:
        """Test that fresh startup sessions never search for handoff parents."""
        mock_parent = MagicMock()
        mock_parent.id = "stale-parent-123"

        mock_dependencies["session_storage"].get.return_value = None
        mock_dependencies["session_storage"].find_parent.return_value = mock_parent
        mock_dependencies["session_manager"].register_session.return_value = "new-sess-789"

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-456",
            data={"source": "startup", "cwd": "/some/dir"},
            metadata={},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        # find_parent should NOT be called for startup sessions
        mock_dependencies["session_storage"].find_parent.assert_not_called()
        # Parent should not be linked
        mock_dependencies["session_manager"].register_session.assert_called_once()
        call_kwargs = mock_dependencies["session_manager"].register_session.call_args
        assert call_kwargs.kwargs.get("parent_session_id") is None or (
            call_kwargs[1].get("parent_session_id") is None
            if call_kwargs[1]
            else True
        )

    def test_new_session_parent_lookup_error(self, mock_dependencies: dict) -> None:
        """Test error looking up parent session is handled gracefully."""
        mock_dependencies["session_storage"].get.return_value = None
        mock_dependencies["session_storage"].find_parent.side_effect = Exception("Lookup error")
        mock_dependencies["session_manager"].register_session.return_value = "new-sess-456"

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={"source": "clear", "cwd": "/some/dir"},
        )

        response = handlers.handle_session_start(event)

        # Should still allow despite error
        assert response.decision == "allow"

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_new_session_mark_parent_expired_error(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict
    ) -> None:
        """Test error marking parent as expired is handled gracefully."""
        mock_sv_mgr_cls.return_value = MagicMock(get_variables=MagicMock(return_value={}))

        mock_parent = MagicMock()
        mock_parent.id = "parent-sess-123"

        mock_dependencies["session_storage"].get.return_value = None
        mock_dependencies["session_storage"].find_parent.return_value = mock_parent
        mock_dependencies["session_manager"].register_session.return_value = "new-sess-456"
        mock_dependencies["session_manager"].mark_session_expired.side_effect = Exception(
            "Failed to expire"
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={"source": "clear"},
        )

        response = handlers.handle_session_start(event)

        # Should still allow despite error
        assert response.decision == "allow"

    def test_new_session_coordinator_registration_error(self, mock_dependencies: dict) -> None:
        """Test error registering session with coordinator is handled."""
        mock_dependencies["session_storage"].get.return_value = None
        mock_dependencies["session_manager"].register_session.return_value = "new-sess-456"
        mock_dependencies["session_coordinator"].register_session.side_effect = Exception(
            "Coordinator error"
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={"transcript_path": "/path/to/transcript.jsonl"},
        )

        response = handlers.handle_session_start(event)

        # Should still allow despite error
        assert response.decision == "allow"

    def test_new_session_message_processor_registration(self, mock_dependencies: dict) -> None:
        """Test new session registers with message processor."""
        mock_dependencies["session_storage"].get.return_value = None
        mock_dependencies["session_manager"].register_session.return_value = "new-sess-456"

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={"transcript_path": "/path/to/transcript.jsonl"},
        )

        handlers.handle_session_start(event)

        mock_dependencies["message_processor"].register_session.assert_called_once_with(
            "new-sess-456", "/path/to/transcript.jsonl", source="claude"
        )

    def test_new_session_message_processor_error(self, mock_dependencies: dict) -> None:
        """Test error registering with message processor is handled."""
        mock_dependencies["session_storage"].get.return_value = None
        mock_dependencies["session_manager"].register_session.return_value = "new-sess-456"
        mock_dependencies["message_processor"].register_session.side_effect = Exception(
            "Registration failed"
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={"transcript_path": "/path/to/transcript.jsonl"},
        )

        response = handlers.handle_session_start(event)

        # Should still allow despite error
        assert response.decision == "allow"

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_new_session_with_task_id_context(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict
    ) -> None:
        """Test new session includes task context when task_id present."""
        mock_sv_mgr = MagicMock()
        mock_sv_mgr.get_variables.return_value = {}
        mock_sv_mgr_cls.return_value = mock_sv_mgr

        mock_dependencies["session_storage"].get.return_value = None
        mock_dependencies["session_manager"].register_session.return_value = "new-sess-456"

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={},
        )
        event.task_id = "task-789"
        event.metadata["_task_title"] = "Implement feature X"

        response = handlers.handle_session_start(event)

        assert "Active Task Context" in response.context
        assert "task-789" in response.context
        assert "Implement feature X" in response.context


class TestSessionEndHandling:
    """Test SESSION_END handler edge cases and error paths."""

    def test_session_end_lookup_from_database(self, mock_dependencies: dict) -> None:
        """Test session_id lookup from database when not in metadata."""
        mock_dependencies["session_manager"].lookup_session_id.return_value = "found-sess-123"

        # Mock session for auto-link
        mock_session = MagicMock()
        mock_session.created_at = "2024-01-01T00:00:00Z"
        mock_session.agent_run_id = None
        mock_dependencies["session_storage"].get.return_value = mock_session

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_END,
            session_id="ext-123",
            metadata={},  # No _platform_session_id
        )
        event.machine_id = "machine-123"

        response = handlers.handle_session_end(event)

        assert response.decision == "allow"
        mock_dependencies["session_manager"].lookup_session_id.assert_called_once()

    def test_session_end_workflow_error(self, mock_dependencies: dict) -> None:
        """Test workflow error during session end is handled."""
        mock_dependencies["workflow_handler"].evaluate.side_effect = Exception("Workflow error")

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_END,
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_session_end(event)

        # Should still allow despite error
        assert response.decision == "allow"

    def test_session_end_auto_link_commits(self, mock_dependencies: dict) -> None:
        """Test auto-linking commits on session end."""
        from unittest.mock import patch

        mock_session = MagicMock()
        mock_session.created_at = "2024-01-01T00:00:00Z"
        mock_session.agent_run_id = None
        mock_dependencies["session_storage"].get.return_value = mock_session

        mock_link_result = MagicMock()
        mock_link_result.total_linked = 2
        mock_link_result.linked_tasks = {"task-1": ["abc123"], "task-2": ["def456"]}

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_END,
            metadata={"_platform_session_id": "sess-123"},
            data={"cwd": "/some/dir"},
        )

        with patch("gobby.tasks.commits.auto_link_commits", return_value=mock_link_result):
            response = handlers.handle_session_end(event)

        assert response.decision == "allow"

    def test_session_end_auto_link_error(self, mock_dependencies: dict) -> None:
        """Test error auto-linking commits is handled gracefully."""
        from unittest.mock import patch

        mock_session = MagicMock()
        mock_session.created_at = "2024-01-01T00:00:00Z"
        mock_session.agent_run_id = None
        mock_dependencies["session_storage"].get.return_value = mock_session

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_END,
            metadata={"_platform_session_id": "sess-123"},
            data={"cwd": "/some/dir"},
        )

        with patch(
            "gobby.tasks.commits.auto_link_commits",
            side_effect=Exception("Link error"),
        ):
            response = handlers.handle_session_end(event)

        # Should still allow despite error
        assert response.decision == "allow"

    def test_session_end_complete_agent_run(self, mock_dependencies: dict) -> None:
        """Test completing agent run on session end."""
        mock_session = MagicMock()
        mock_session.created_at = "2024-01-01T00:00:00Z"
        mock_session.agent_run_id = "run-456"
        mock_dependencies["session_storage"].get.return_value = mock_session

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_END,
            metadata={"_platform_session_id": "sess-123"},
        )

        handlers.handle_session_end(event)

        mock_dependencies["session_coordinator"].complete_agent_run.assert_called_once()

    def test_session_end_complete_agent_run_error(self, mock_dependencies: dict) -> None:
        """Test error completing agent run is handled gracefully."""
        mock_session = MagicMock()
        mock_session.created_at = "2024-01-01T00:00:00Z"
        mock_session.agent_run_id = "run-456"
        mock_dependencies["session_storage"].get.return_value = mock_session
        mock_dependencies["session_coordinator"].complete_agent_run.side_effect = Exception(
            "Completion error"
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_END,
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_session_end(event)

        # Should still allow despite error
        assert response.decision == "allow"

    def test_session_end_unregister_message_processor(self, mock_dependencies: dict) -> None:
        """Test unregistering from message processor on session end."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_END,
            session_id="ext-123",
            metadata={"_platform_session_id": "sess-123"},
        )

        handlers.handle_session_end(event)

        mock_dependencies["message_processor"].unregister_session.assert_called_once_with(
            "sess-123"
        )

    def test_session_end_unregister_uses_external_id_as_fallback(
        self, mock_dependencies: dict
    ) -> None:
        """Test unregister uses external_id when session_id lookup returns None."""
        # Make lookup return None so external_id is used as fallback
        mock_dependencies["session_manager"].lookup_session_id.return_value = None

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_END,
            session_id="ext-123",
            metadata={},  # No _platform_session_id
        )

        handlers.handle_session_end(event)

        # When session_id is None, external_id is used as fallback for unregister
        mock_dependencies["message_processor"].unregister_session.assert_called_once_with("ext-123")

    def test_session_end_unregister_error(self, mock_dependencies: dict) -> None:
        """Test error unregistering from message processor is handled."""
        mock_dependencies["message_processor"].unregister_session.side_effect = Exception(
            "Unregister error"
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_END,
            session_id="ext-123",
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_session_end(event)

        # Should still allow despite error
        assert response.decision == "allow"

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_session_end_marks_expired_without_handoff(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict
    ) -> None:
        """Test SESSION_END marks session as expired when no handoff_source."""
        mock_sv_mgr_cls.return_value.get_variables.return_value = {}
        mock_session = MagicMock()
        mock_session.created_at = "2024-01-01T00:00:00Z"
        mock_session.agent_run_id = None
        mock_dependencies["session_storage"].get.return_value = mock_session

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_END,
            session_id="ext-123",
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_session_end(event)

        assert response.decision == "allow"
        mock_dependencies["session_storage"].update_status.assert_called_once_with(
            "sess-123", "expired"
        )

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_session_end_marks_handoff_ready_with_clear_reason(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict
    ) -> None:
        """Test SESSION_END marks handoff_ready when event reason is 'clear'."""
        mock_session = MagicMock()
        mock_session.created_at = "2024-01-01T00:00:00Z"
        mock_session.agent_run_id = None
        mock_session.status = "active"
        mock_dependencies["session_storage"].get.return_value = mock_session

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_END,
            session_id="ext-123",
            data={"reason": "clear"},
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_session_end(event)

        assert response.decision == "allow"
        mock_dependencies["session_storage"].update_status.assert_called_once_with(
            "sess-123", "handoff_ready"
        )

    def test_session_end_preserves_handoff_ready_from_compact(
        self, mock_dependencies: dict
    ) -> None:
        """Test SESSION_END doesn't downgrade handoff_ready set by PRE_COMPACT."""
        mock_session = MagicMock()
        mock_session.created_at = "2024-01-01T00:00:00Z"
        mock_session.agent_run_id = None
        mock_session.status = "handoff_ready"
        mock_dependencies["session_storage"].get.return_value = mock_session

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_END,
            session_id="ext-123",
            data={"reason": "other"},
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_session_end(event)

        assert response.decision == "allow"
        mock_dependencies["session_storage"].update_status.assert_called_once_with(
            "sess-123", "handoff_ready"
        )

    def test_session_end_handoff_ready_error_handled(self, mock_dependencies: dict) -> None:
        """Test error marking handoff_ready doesn't block response."""
        mock_session = MagicMock()
        mock_session.created_at = "2024-01-01T00:00:00Z"
        mock_session.agent_run_id = None
        mock_dependencies["session_storage"].get.return_value = mock_session
        mock_dependencies["session_storage"].update_status.side_effect = Exception("DB write error")

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_END,
            session_id="ext-123",
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_session_end(event)

        # Should still allow despite error
        assert response.decision == "allow"


class TestSessionStartHandoff:
    """Test session handoff context injection on /clear and /compact."""

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_session_start_compact_finds_parent(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict
    ) -> None:
        """Test parent lookup works for source='compact'."""
        mock_sv_mgr_cls.return_value = MagicMock(get_variables=MagicMock(return_value={}))

        mock_parent = MagicMock()
        mock_parent.id = "parent-sess-123"

        mock_dependencies["session_storage"].get.return_value = None
        mock_dependencies["session_storage"].find_parent.return_value = mock_parent
        mock_dependencies["session_manager"].register_session.return_value = "new-sess-456"

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={"source": "compact", "cwd": "/some/dir"},
            metadata={},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        assert "Parent session: parent-sess-123" in response.context
        mock_dependencies["session_storage"].find_parent.assert_called_once()

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_session_start_clear_sets_full_session_summary_variable(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict
    ) -> None:
        """Test summary_markdown set as full_session_summary session variable for source='clear'."""
        mock_sv_mgr = MagicMock()
        mock_sv_mgr.get_variables.return_value = {"auto_inject_handoff": True}
        mock_sv_mgr_cls.return_value = mock_sv_mgr

        mock_parent_for_find = MagicMock()
        mock_parent_for_find.id = "parent-sess-123"

        mock_parent_obj = MagicMock()
        mock_parent_obj.id = "parent-sess-123"
        mock_parent_obj.seq_num = 42
        mock_parent_obj.summary_markdown = "# Summary\nWorked on feature X"
        mock_parent_obj.compact_markdown = None

        # get() called: pre-created check (None), handoff var population (parent),
        # seq_num fetch (new session)
        mock_new_session = MagicMock()
        mock_new_session.seq_num = 43

        mock_dependencies["session_storage"].get.side_effect = [
            None,  # pre-created session check
            mock_parent_obj,  # handoff variable population
            mock_new_session,  # fetch session for seq_num
        ]
        mock_dependencies["session_storage"].find_parent.return_value = mock_parent_for_find
        mock_dependencies["session_manager"].register_session.return_value = "new-sess-456"

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={"source": "clear", "cwd": "/some/dir"},
            metadata={},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_sv_mgr.merge_variables.assert_any_call(
            "new-sess-456",
            {"full_session_summary": "# Summary\nWorked on feature X"},
        )

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_session_start_compact_sets_compact_session_summary_variable(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict
    ) -> None:
        """Test compact_markdown set as compact_session_summary session variable for source='compact'."""
        mock_sv_mgr = MagicMock()
        mock_sv_mgr.get_variables.return_value = {"auto_inject_handoff": True}
        mock_sv_mgr_cls.return_value = mock_sv_mgr

        mock_parent_for_find = MagicMock()
        mock_parent_for_find.id = "parent-sess-123"

        mock_parent_obj = MagicMock()
        mock_parent_obj.id = "parent-sess-123"
        mock_parent_obj.seq_num = 42
        mock_parent_obj.summary_markdown = None
        mock_parent_obj.compact_markdown = "# Compact\nContinuation of task Y"

        mock_new_session = MagicMock()
        mock_new_session.seq_num = 43

        mock_dependencies["session_storage"].get.side_effect = [
            None,  # pre-created session check
            mock_parent_obj,  # handoff variable population
            mock_new_session,  # fetch session for seq_num
        ]
        mock_dependencies["session_storage"].find_parent.return_value = mock_parent_for_find
        mock_dependencies["session_manager"].register_session.return_value = "new-sess-456"

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={"source": "compact", "cwd": "/some/dir"},
            metadata={},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_sv_mgr.merge_variables.assert_any_call(
            "new-sess-456",
            {"compact_session_summary": "# Compact\nContinuation of task Y"},
        )

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_task_claim_vars_carried_over_on_compact(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict
    ) -> None:
        """Test task claim variables are copied from parent to child on compact."""
        parent_vars = {
            "task_claimed": True,
            "claimed_tasks": {"uuid-123": "#42"},
            "session_had_task": True,
        }
        mock_sv_mgr = MagicMock()
        mock_sv_mgr.get_variables.side_effect = lambda sid: (
            parent_vars if sid == "parent-sess-123" else {"auto_inject_handoff": True}
        )
        mock_sv_mgr_cls.return_value = mock_sv_mgr

        mock_parent_for_find = MagicMock()
        mock_parent_for_find.id = "parent-sess-123"

        mock_parent_obj = MagicMock()
        mock_parent_obj.id = "parent-sess-123"
        mock_parent_obj.seq_num = 42
        mock_parent_obj.summary_markdown = None
        mock_parent_obj.compact_markdown = "# Compact\nContinuation"

        mock_new_session = MagicMock()
        mock_new_session.seq_num = 43

        mock_dependencies["session_storage"].get.side_effect = [
            None,  # pre-created session check
            mock_parent_obj,  # handoff variable population
            mock_new_session,  # fetch session for seq_num
        ]
        mock_dependencies["session_storage"].find_parent.return_value = mock_parent_for_find
        mock_dependencies["session_manager"].register_session.return_value = "new-sess-456"
        mock_dependencies["session_task_manager"] = MagicMock()

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={"source": "compact", "cwd": "/some/dir"},
            metadata={},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_sv_mgr.merge_variables.assert_any_call(
            "new-sess-456",
            {
                "task_claimed": True,
                "claimed_tasks": {"uuid-123": "#42"},
                "session_had_task": True,
            },
        )
        mock_dependencies["task_manager"].update_task.assert_called_once_with(
            "uuid-123", assignee="new-sess-456"
        )
        mock_dependencies["session_task_manager"].link_task.assert_called_once_with(
            "new-sess-456", "uuid-123", "claimed"
        )

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_closed_task_not_carried_over(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict
    ) -> None:
        """Test that closed task (task_claimed=False) is not carried over on compact."""
        mock_sv_mgr = MagicMock()
        mock_sv_mgr.get_variables.side_effect = lambda sid: (
            {"task_claimed": False, "claimed_tasks": {}}
            if sid == "parent-sess-123"
            else {"auto_inject_handoff": True}
        )
        mock_sv_mgr_cls.return_value = mock_sv_mgr

        mock_parent_for_find = MagicMock()
        mock_parent_for_find.id = "parent-sess-123"

        mock_parent_obj = MagicMock()
        mock_parent_obj.id = "parent-sess-123"
        mock_parent_obj.seq_num = 42
        mock_parent_obj.summary_markdown = None
        mock_parent_obj.compact_markdown = "# Compact\nDone"

        mock_new_session = MagicMock()
        mock_new_session.seq_num = 43

        mock_dependencies["session_storage"].get.side_effect = [
            None,
            mock_parent_obj,
            mock_new_session,
        ]
        mock_dependencies["session_storage"].find_parent.return_value = mock_parent_for_find
        mock_dependencies["session_manager"].register_session.return_value = "new-sess-456"
        mock_dependencies["session_task_manager"] = MagicMock()

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={"source": "compact", "cwd": "/some/dir"},
            metadata={},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        # Only the compact summary merge should have happened, not task claim vars
        for call in mock_sv_mgr.merge_variables.call_args_list:
            args = call[0]
            if len(args) >= 2:
                merged_dict = args[1]
                assert "task_claimed" not in merged_dict
        mock_dependencies["task_manager"].update_task.assert_not_called()
        mock_dependencies["session_task_manager"].link_task.assert_not_called()

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_task_claim_vars_carried_over_on_clear(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict
    ) -> None:
        """Test task claim variables are copied from parent to child on /clear."""
        parent_vars = {
            "task_claimed": True,
            "claimed_tasks": {"uuid-789": "#99"},
        }
        mock_sv_mgr = MagicMock()
        mock_sv_mgr.get_variables.side_effect = lambda sid: (
            parent_vars if sid == "parent-sess-500" else {"auto_inject_handoff": True}
        )
        mock_sv_mgr_cls.return_value = mock_sv_mgr

        mock_parent_for_find = MagicMock()
        mock_parent_for_find.id = "parent-sess-500"

        mock_parent_obj = MagicMock()
        mock_parent_obj.id = "parent-sess-500"
        mock_parent_obj.seq_num = 50
        mock_parent_obj.summary_markdown = "# Summary\nCleared session"
        mock_parent_obj.compact_markdown = None

        mock_new_session = MagicMock()
        mock_new_session.seq_num = 51

        mock_dependencies["session_storage"].get.side_effect = [
            None,  # pre-created session check
            mock_parent_obj,  # handoff variable population
            mock_new_session,  # fetch session for seq_num
        ]
        mock_dependencies["session_storage"].find_parent.return_value = mock_parent_for_find
        mock_dependencies["session_manager"].register_session.return_value = "new-sess-600"
        mock_dependencies["session_task_manager"] = MagicMock()

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-456",
            data={"source": "clear", "cwd": "/some/dir"},
            metadata={},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_sv_mgr.merge_variables.assert_any_call(
            "new-sess-600",
            {
                "task_claimed": True,
                "claimed_tasks": {"uuid-789": "#99"},
            },
        )
        mock_dependencies["task_manager"].update_task.assert_called_once_with(
            "uuid-789", assignee="new-sess-600"
        )
        mock_dependencies["session_task_manager"].link_task.assert_called_once_with(
            "new-sess-600", "uuid-789", "claimed"
        )

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_session_start_task_context_variable(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict
    ) -> None:
        """Test task_context session variable set when task_id is present."""
        mock_sv_mgr = MagicMock()
        mock_sv_mgr.get_variables.return_value = {}
        mock_sv_mgr_cls.return_value = mock_sv_mgr

        mock_dependencies["session_storage"].get.side_effect = [
            None,  # pre-created session check
            MagicMock(seq_num=10),  # fetch session for seq_num
        ]
        mock_dependencies["session_manager"].register_session.return_value = "new-sess-789"

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-456",
            data={"source": "startup", "cwd": "/some/dir"},
            metadata={"_task_title": "Fix login bug"},
        )
        event.task_id = "task-abc"

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_sv_mgr.merge_variables.assert_any_call(
            "new-sess-789",
            {"task_context": "You are working on task: Fix login bug (task-abc)"},
        )


class TestBeforeAgentHandling:
    """Test BEFORE_AGENT handler edge cases."""

    def test_before_agent_updates_session_status(self, mock_dependencies: dict) -> None:
        """Test BEFORE_AGENT updates session status to active."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_AGENT,
            data={"prompt": "Hello world"},
            metadata={"_platform_session_id": "sess-123"},
        )

        handlers.handle_before_agent(event)

        mock_dependencies["session_manager"].update_session_status.assert_called_once_with(
            "sess-123", "active"
        )

    def test_before_agent_skips_status_update_for_clear(self, mock_dependencies: dict) -> None:
        """Test BEFORE_AGENT skips status update for /clear command."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_AGENT,
            data={"prompt": "/clear"},
            metadata={"_platform_session_id": "sess-123"},
        )

        handlers.handle_before_agent(event)

        mock_dependencies["session_manager"].update_session_status.assert_not_called()

    def test_before_agent_skips_status_update_for_exit(self, mock_dependencies: dict) -> None:
        """Test BEFORE_AGENT skips status update for /exit command."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_AGENT,
            data={"prompt": "/exit"},
            metadata={"_platform_session_id": "sess-123"},
        )

        handlers.handle_before_agent(event)

        mock_dependencies["session_manager"].update_session_status.assert_not_called()

    def test_before_agent_resets_transcript_processed(self, mock_dependencies: dict) -> None:
        """Test BEFORE_AGENT resets transcript processed flag."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_AGENT,
            data={"prompt": "Hello"},
            metadata={"_platform_session_id": "sess-123"},
        )

        handlers.handle_before_agent(event)

        mock_dependencies["session_storage"].reset_transcript_processed.assert_called_once_with(
            "sess-123"
        )

    def test_before_agent_status_update_error(self, mock_dependencies: dict) -> None:
        """Test error updating session status is handled."""
        mock_dependencies["session_manager"].update_session_status.side_effect = Exception(
            "Update error"
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_AGENT,
            data={"prompt": "Hello"},
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_before_agent(event)

        # Should still allow despite error
        assert response.decision == "allow"

    def test_before_agent_handles_clear_with_transcript(self, mock_dependencies: dict) -> None:
        """Test BEFORE_AGENT handles /clear with transcript path."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_AGENT,
            data={"prompt": "/clear", "transcript_path": "/path/to/transcript.jsonl"},
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_before_agent(event)

        assert response.decision == "allow"


class TestAfterAgentHandling:
    """Test AFTER_AGENT handler edge cases."""

    def test_after_agent_updates_session_status(self, mock_dependencies: dict) -> None:
        """Test AFTER_AGENT updates session status to paused."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_AGENT,
            metadata={"_platform_session_id": "sess-123"},
        )

        handlers.handle_after_agent(event)

        mock_dependencies["session_manager"].update_session_status.assert_called_once_with(
            "sess-123", "paused"
        )

    def test_after_agent_status_update_error(self, mock_dependencies: dict) -> None:
        """Test error updating session status is handled."""
        mock_dependencies["session_manager"].update_session_status.side_effect = Exception(
            "Update error"
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_AGENT,
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_after_agent(event)

        # Should still allow despite error
        assert response.decision == "allow"

    def test_after_agent_no_session_id(self, mock_dependencies: dict) -> None:
        """Test AFTER_AGENT handles missing session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_AGENT,
            metadata={},  # No _platform_session_id
        )

        response = handlers.handle_after_agent(event)

        assert response.decision == "allow"
        mock_dependencies["session_manager"].update_session_status.assert_not_called()


class TestToolHandlerEdgeCases:
    """Test BEFORE_TOOL and AFTER_TOOL edge cases."""

    def test_before_tool_no_session_id(self, mock_dependencies: dict) -> None:
        """Test BEFORE_TOOL handles missing session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Read"},
            metadata={},
        )

        response = handlers.handle_before_tool(event)

        assert response.decision == "allow"

    def test_after_tool_failure_status(self, mock_dependencies: dict) -> None:
        """Test AFTER_TOOL handles is_failure metadata."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Write"},
            metadata={"_platform_session_id": "sess-123", "is_failure": True},
        )

        response = handlers.handle_after_tool(event)

        assert response.decision == "allow"

    def test_after_tool_no_session_id(self, mock_dependencies: dict) -> None:
        """Test AFTER_TOOL handles missing session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Read"},
            metadata={},
        )

        response = handlers.handle_after_tool(event)

        assert response.decision == "allow"

    def test_after_tool_edit_marks_had_edits(self, mock_dependencies: dict) -> None:
        """Test AFTER_TOOL marks had_edits for edit tools on regular files."""
        mock_dependencies["task_manager"].list_tasks.return_value = [
            MagicMock()
        ]  # Has claimed task
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Write",
                "tool_input": {"file_path": "/path/to/regular/file.py"},
            },
            metadata={"_platform_session_id": "sess-123"},
        )

        handlers.handle_after_tool(event)

        mock_dependencies["session_storage"].mark_had_edits.assert_called_once_with("sess-123")

    def test_after_tool_edit_skips_gobby_internal_files(self, mock_dependencies: dict) -> None:
        """Test AFTER_TOOL does NOT mark had_edits for .gobby/ internal files."""
        mock_dependencies["task_manager"].list_tasks.return_value = [
            MagicMock()
        ]  # Has claimed task
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Write",
                "tool_input": {"file_path": "/path/to/project/.gobby/tasks.jsonl"},
            },
            metadata={"_platform_session_id": "sess-123"},
        )

        handlers.handle_after_tool(event)

        mock_dependencies["session_storage"].mark_had_edits.assert_not_called()

    def test_after_tool_edit_skips_relative_gobby_path(self, mock_dependencies: dict) -> None:
        """Test AFTER_TOOL does NOT mark had_edits for relative .gobby/ paths."""
        mock_dependencies["task_manager"].list_tasks.return_value = [
            MagicMock()
        ]  # Has claimed task
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={
                "tool_name": "Edit",
                "tool_input": {"file_path": ".gobby/memories.jsonl"},
            },
            metadata={"_platform_session_id": "sess-123"},
        )

        handlers.handle_after_tool(event)

        mock_dependencies["session_storage"].mark_had_edits.assert_not_called()


class TestPreCompactHandlerEdgeCases:
    """Test PRE_COMPACT handler edge cases."""

    def test_pre_compact_updates_session_status(self, mock_dependencies: dict) -> None:
        """Test PRE_COMPACT updates session status to handoff_ready."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.PRE_COMPACT,
            data={"trigger": "user"},
            metadata={"_platform_session_id": "sess-123"},
        )

        handlers.handle_pre_compact(event)

        mock_dependencies["session_manager"].update_session_status.assert_called_once_with(
            "sess-123", "handoff_ready"
        )

    def test_pre_compact_no_session_id(self, mock_dependencies: dict) -> None:
        """Test PRE_COMPACT handles missing session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.PRE_COMPACT,
            data={"trigger": "auto"},
            metadata={},
        )

        response = handlers.handle_pre_compact(event)

        assert response.decision == "allow"
        mock_dependencies["session_manager"].update_session_status.assert_not_called()

    def test_pre_compact_gemini_skips_handoff(self, mock_dependencies: dict) -> None:
        """Test PRE_COMPACT skips handoff logic for Gemini source.

        Gemini fires PreCompress constantly during normal operation,
        unlike Claude which fires it only when approaching context limits.
        """
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.PRE_COMPACT,
            source="gemini",
            data={"trigger": "auto"},
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_pre_compact(event)

        assert response.decision == "allow"
        # Should NOT update session status for Gemini
        mock_dependencies["session_manager"].update_session_status.assert_not_called()
        # Should NOT execute workflow handler for Gemini
        mock_dependencies["workflow_handler"].evaluate.assert_not_called()


class TestSubagentHandlerEdgeCases:
    """Test SUBAGENT_START and SUBAGENT_STOP edge cases."""

    def test_subagent_start_with_agent_id(self, mock_dependencies: dict) -> None:
        """Test SUBAGENT_START logs agent_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SUBAGENT_START,
            data={"agent_id": "agent-123", "subagent_id": "subagent-456"},
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_subagent_start(event)

        assert response.decision == "allow"

    def test_subagent_start_no_session_id(self, mock_dependencies: dict) -> None:
        """Test SUBAGENT_START handles missing session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SUBAGENT_START,
            data={"subagent_id": "sub-1"},
            metadata={},
        )

        response = handlers.handle_subagent_start(event)

        assert response.decision == "allow"

    def test_subagent_stop_no_session_id(self, mock_dependencies: dict) -> None:
        """Test SUBAGENT_STOP handles missing session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SUBAGENT_STOP,
            metadata={},
        )

        response = handlers.handle_subagent_stop(event)

        assert response.decision == "allow"


class TestNotificationHandlerEdgeCases:
    """Test NOTIFICATION handler edge cases."""

    def test_notification_updates_session_status(self, mock_dependencies: dict) -> None:
        """Test NOTIFICATION updates session status to paused."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.NOTIFICATION,
            data={"notification_type": "info"},
            metadata={"_platform_session_id": "sess-123"},
        )

        handlers.handle_notification(event)

        mock_dependencies["session_manager"].update_session_status.assert_called_once_with(
            "sess-123", "paused"
        )

    def test_notification_status_update_error(self, mock_dependencies: dict) -> None:
        """Test error updating session status is handled."""
        mock_dependencies["session_manager"].update_session_status.side_effect = Exception(
            "Update error"
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.NOTIFICATION,
            data={"notification_type": "info"},
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_notification(event)

        # Should still allow despite error
        assert response.decision == "allow"

    def test_notification_type_variants(self, mock_dependencies: dict) -> None:
        """Test NOTIFICATION handles different type field names."""
        handlers = EventHandlers(**mock_dependencies)

        # Test notificationType field
        event1 = make_event(
            HookEventType.NOTIFICATION,
            data={"notificationType": "warning"},
        )
        response1 = handlers.handle_notification(event1)
        assert response1.decision == "allow"

        # Test type field
        event2 = make_event(
            HookEventType.NOTIFICATION,
            data={"type": "error"},
        )
        response2 = handlers.handle_notification(event2)
        assert response2.decision == "allow"

        # Test no type field (defaults to general)
        event3 = make_event(
            HookEventType.NOTIFICATION,
            data={},
        )
        response3 = handlers.handle_notification(event3)
        assert response3.decision == "allow"

    def test_notification_no_session_id(self, mock_dependencies: dict) -> None:
        """Test NOTIFICATION handles missing session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.NOTIFICATION,
            data={"message": "test"},
            metadata={},
        )

        response = handlers.handle_notification(event)

        assert response.decision == "allow"
        mock_dependencies["session_manager"].update_session_status.assert_not_called()


class TestPermissionRequestEdgeCases:
    """Test PERMISSION_REQUEST handler edge cases."""

    def test_permission_request_with_session_id(self, mock_dependencies: dict) -> None:
        """Test PERMISSION_REQUEST with session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.PERMISSION_REQUEST,
            data={"permission_type": "write"},
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_permission_request(event)

        assert response.decision == "allow"

    def test_permission_request_no_session_id(self, mock_dependencies: dict) -> None:
        """Test PERMISSION_REQUEST handles missing session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.PERMISSION_REQUEST,
            data={"permission_type": "execute"},
            metadata={},
        )

        response = handlers.handle_permission_request(event)

        assert response.decision == "allow"


class TestGeminiHandlerEdgeCases:
    """Test Gemini-only handler edge cases."""

    def test_before_tool_selection_with_session_id(self, mock_dependencies: dict) -> None:
        """Test BEFORE_TOOL_SELECTION with session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_TOOL_SELECTION,
            source="gemini",
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_before_tool_selection(event)

        assert response.decision == "allow"

    def test_before_tool_selection_no_session_id(self, mock_dependencies: dict) -> None:
        """Test BEFORE_TOOL_SELECTION handles missing session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_TOOL_SELECTION,
            source="gemini",
            metadata={},
        )

        response = handlers.handle_before_tool_selection(event)

        assert response.decision == "allow"

    def test_before_model_with_session_id(self, mock_dependencies: dict) -> None:
        """Test BEFORE_MODEL with session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_MODEL,
            source="gemini",
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_before_model(event)

        assert response.decision == "allow"

    def test_before_model_no_session_id(self, mock_dependencies: dict) -> None:
        """Test BEFORE_MODEL handles missing session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_MODEL,
            source="gemini",
            metadata={},
        )

        response = handlers.handle_before_model(event)

        assert response.decision == "allow"

    def test_after_model_with_session_id(self, mock_dependencies: dict) -> None:
        """Test AFTER_MODEL with session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_MODEL,
            source="gemini",
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_after_model(event)

        assert response.decision == "allow"

    def test_after_model_no_session_id(self, mock_dependencies: dict) -> None:
        """Test AFTER_MODEL handles missing session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_MODEL,
            source="gemini",
            metadata={},
        )

        response = handlers.handle_after_model(event)

        assert response.decision == "allow"


class TestSubagentHandlerWithSessionId:
    """Test SUBAGENT handlers with session_id for log coverage."""

    def test_subagent_stop_with_session_id(self, mock_dependencies: dict) -> None:
        """Test SUBAGENT_STOP with session_id present."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SUBAGENT_STOP,
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_subagent_stop(event)

        assert response.decision == "allow"

    def test_subagent_start_without_subagent_id(self, mock_dependencies: dict) -> None:
        """Test SUBAGENT_START without subagent_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SUBAGENT_START,
            data={"agent_id": "agent-123"},  # No subagent_id
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_subagent_start(event)

        assert response.decision == "allow"

    def test_subagent_start_without_agent_id(self, mock_dependencies: dict) -> None:
        """Test SUBAGENT_START without agent_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SUBAGENT_START,
            data={},  # No agent_id or subagent_id
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_subagent_start(event)

        assert response.decision == "allow"


class TestNoManagerDependencies:
    """Test handlers when dependencies are None."""

    def test_session_start_no_dependencies(self) -> None:
        """Test SESSION_START works without dependencies."""
        handlers = EventHandlers()
        event = make_event(HookEventType.SESSION_START)

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"

    def test_session_end_no_dependencies(self) -> None:
        """Test SESSION_END works without dependencies."""
        handlers = EventHandlers()
        event = make_event(HookEventType.SESSION_END)

        response = handlers.handle_session_end(event)

        assert response.decision == "allow"

    def test_before_agent_no_dependencies(self) -> None:
        """Test BEFORE_AGENT works without dependencies."""
        handlers = EventHandlers()
        event = make_event(
            HookEventType.BEFORE_AGENT,
            data={"prompt": "Hello"},
        )

        response = handlers.handle_before_agent(event)

        assert response.decision == "allow"

    def test_after_agent_no_dependencies(self) -> None:
        """Test AFTER_AGENT works without dependencies."""
        handlers = EventHandlers()
        event = make_event(HookEventType.AFTER_AGENT)

        response = handlers.handle_after_agent(event)

        assert response.decision == "allow"

    def test_before_tool_no_dependencies(self) -> None:
        """Test BEFORE_TOOL works without dependencies."""
        handlers = EventHandlers()
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Read"},
        )

        response = handlers.handle_before_tool(event)

        assert response.decision == "allow"

    def test_after_tool_no_dependencies(self) -> None:
        """Test AFTER_TOOL works without dependencies."""
        handlers = EventHandlers()
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Read"},
        )

        response = handlers.handle_after_tool(event)

        assert response.decision == "allow"

    def test_pre_compact_no_dependencies(self) -> None:
        """Test PRE_COMPACT works without dependencies."""
        handlers = EventHandlers()
        event = make_event(HookEventType.PRE_COMPACT)

        response = handlers.handle_pre_compact(event)

        assert response.decision == "allow"

    def test_stop_no_dependencies(self) -> None:
        """Test STOP works without dependencies."""
        handlers = EventHandlers()
        event = make_event(HookEventType.STOP)

        response = handlers.handle_stop(event)

        assert response.decision == "allow"

    def test_notification_no_dependencies(self) -> None:
        """Test NOTIFICATION works without dependencies."""
        handlers = EventHandlers()
        event = make_event(HookEventType.NOTIFICATION)

        response = handlers.handle_notification(event)

        assert response.decision == "allow"


class TestTranscriptPathDerivation:
    """Test _derive_transcript_path and helpers for non-Claude CLIs."""

    def test_derive_transcript_path_unknown_cli(self, event_handlers: EventHandlers) -> None:
        """Unknown CLI should return None."""
        result = event_handlers._derive_transcript_path("unknown-cli", {}, "ext-123")
        assert result is None

    def test_derive_transcript_path_claude_returns_none(
        self, event_handlers: EventHandlers
    ) -> None:
        """Claude provides transcript_path natively, so derivation returns None."""
        result = event_handlers._derive_transcript_path("claude", {}, "ext-123")
        assert result is None

    def test_find_gemini_transcript_no_cwd(self, event_handlers: EventHandlers) -> None:
        """Should return None when cwd is not provided."""
        result = event_handlers._find_gemini_transcript({}, "ext-123")
        assert result is None

    def test_find_gemini_transcript_nonexistent_dir(self, event_handlers: EventHandlers) -> None:
        """Should return None when chats dir doesn't exist."""
        result = event_handlers._find_gemini_transcript(
            {"cwd": "/nonexistent/path/that/does/not/exist"}, "ext-123"
        )
        assert result is None

    def test_find_gemini_transcript_by_prefix(
        self, event_handlers: EventHandlers, tmp_path, monkeypatch
    ) -> None:
        """Should find Gemini session file by session_id prefix."""
        import hashlib

        cwd = str(tmp_path / "myproject")
        project_hash = hashlib.sha256(cwd.encode()).hexdigest()
        chats_dir = tmp_path / ".gemini" / "tmp" / project_hash / "chats"
        chats_dir.mkdir(parents=True)

        # Create a session file matching the prefix
        session_file = chats_dir / "session-2024-01-01T10-00-abcd1234.json"
        session_file.touch()

        import gobby.hooks.event_handlers._session as session_mod

        monkeypatch.setattr(session_mod.Path, "home", staticmethod(lambda: tmp_path))

        result = event_handlers._find_gemini_transcript(
            {"cwd": cwd, "session_id": "abcd1234-full-uuid"}, "ext-123"
        )
        assert result is not None
        assert "abcd1234" in result

    def test_find_gemini_transcript_fallback_most_recent(
        self, event_handlers: EventHandlers, tmp_path, monkeypatch
    ) -> None:
        """Should fall back to most recent session file when prefix doesn't match."""
        import hashlib

        cwd = str(tmp_path / "myproject")
        project_hash = hashlib.sha256(cwd.encode()).hexdigest()
        chats_dir = tmp_path / ".gemini" / "tmp" / project_hash / "chats"
        chats_dir.mkdir(parents=True)

        # Create session files that won't match the prefix
        session_file = chats_dir / "session-2024-01-01T10-00-xxxxxxxx.json"
        session_file.touch()

        import gobby.hooks.event_handlers._session as session_mod

        monkeypatch.setattr(session_mod.Path, "home", staticmethod(lambda: tmp_path))

        result = event_handlers._find_gemini_transcript(
            {"cwd": cwd, "session_id": "nomatch-uuid"}, "ext-123"
        )
        assert result is not None
        assert "xxxxxxxx" in result

    def test_find_cursor_transcript_from_terminal_context(
        self, event_handlers: EventHandlers
    ) -> None:
        """Should find Cursor capture path from terminal_context."""
        result = event_handlers._find_cursor_transcript(
            {"terminal_context": {"cursor_capture_path": "/tmp/gobby-cursor-abc.ndjson"}},
            "ext-123",
        )
        assert result == "/tmp/gobby-cursor-abc.ndjson"

    def test_find_cursor_transcript_from_standard_location(
        self, event_handlers: EventHandlers, tmp_path
    ) -> None:
        """Should find Cursor capture file in temp dir by session_id."""
        import tempfile
        from unittest.mock import patch

        capture_file = tmp_path / "gobby-cursor-test-session-id-xyz.ndjson"
        capture_file.write_text("")

        with patch.object(tempfile, "gettempdir", return_value=str(tmp_path)):
            result = event_handlers._find_cursor_transcript(
                {"session_id": "test-session-id-xyz"}, "ext-123"
            )
            assert result == str(capture_file)

    def test_find_cursor_transcript_not_found(self, event_handlers: EventHandlers) -> None:
        """Should return None when no capture file exists."""
        result = event_handlers._find_cursor_transcript({}, "nonexistent-session-id-xyz")
        assert result is None

    def test_derive_gemini_dispatches(self, event_handlers: EventHandlers) -> None:
        """_derive_transcript_path should dispatch to _find_gemini_transcript for gemini."""
        # Without cwd, gemini derivation returns None
        result = event_handlers._derive_transcript_path("gemini", {}, "ext-123")
        assert result is None

    def test_derive_antigravity_dispatches(self, event_handlers: EventHandlers) -> None:
        """_derive_transcript_path should dispatch to _find_gemini_transcript for antigravity."""
        result = event_handlers._derive_transcript_path("antigravity", {}, "ext-123")
        assert result is None

    def test_derive_cursor_dispatches(self, event_handlers: EventHandlers) -> None:
        """_derive_transcript_path should dispatch to _find_cursor_transcript for cursor."""
        result = event_handlers._derive_transcript_path("cursor", {}, "ext-123")
        assert result is None

    def test_session_start_derives_gemini_transcript(self, mock_dependencies: dict) -> None:
        """SESSION_START should derive transcript_path for Gemini when not provided natively."""
        handlers = EventHandlers(**mock_dependencies)
        mock_dependencies["session_storage"].get.return_value = None

        event = make_event(
            HookEventType.SESSION_START,
            source="gemini",
            data={"cwd": "/some/project", "source": "startup"},
        )

        response = handlers.handle_session_start(event)
        assert response.decision == "allow"


class TestApplyDebugEcho:
    """Tests for _apply_debug_echo helper on EventHandlersBase."""

    def test_debug_echo_from_workflow_config(self) -> None:
        """Test debug echo enabled via WorkflowConfig.debug_echo_context."""
        mock_config = MagicMock()
        mock_config.debug_echo_context = True

        handlers = EventHandlers(workflow_config=mock_config)
        response = HookResponse(decision="allow", context="some context")

        handlers._apply_debug_echo(response)

        assert response.system_message is not None
        assert "[DEBUG additionalContext]" in response.system_message
        assert "some context" in response.system_message

    def test_debug_echo_disabled(self) -> None:
        """Test no echo when debug_echo_context is False."""
        mock_config = MagicMock()
        mock_config.debug_echo_context = False

        handlers = EventHandlers(workflow_config=mock_config)
        response = HookResponse(decision="allow", context="some context")

        handlers._apply_debug_echo(response)

        assert response.system_message is None

    def test_debug_echo_empty_context(self) -> None:
        """Test no echo when context is empty."""
        mock_config = MagicMock()
        mock_config.debug_echo_context = True

        handlers = EventHandlers(workflow_config=mock_config)
        response = HookResponse(decision="allow", context=None)

        handlers._apply_debug_echo(response)

        assert response.system_message is None

    def test_debug_echo_appends_to_existing_system_message(self) -> None:
        """Test echo appends to existing system_message rather than replacing."""
        mock_config = MagicMock()
        mock_config.debug_echo_context = True

        handlers = EventHandlers(workflow_config=mock_config)
        response = HookResponse(
            decision="allow",
            context="new context",
            system_message="Existing message",
        )

        handlers._apply_debug_echo(response)

        assert response.system_message.startswith("Existing message")
        assert "[DEBUG additionalContext]" in response.system_message
        assert "new context" in response.system_message

    def test_debug_echo_exists_on_base(self) -> None:
        """_apply_debug_echo is defined on EventHandlersBase."""
        from gobby.hooks.event_handlers._base import EventHandlersBase

        assert hasattr(EventHandlersBase, "_apply_debug_echo")
        assert callable(EventHandlersBase._apply_debug_echo)

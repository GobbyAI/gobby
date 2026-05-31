"""Handler execution, return value, and dependency isolation tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEventType, HookResponse

from ._event_handler_helpers import make_event

pytestmark = pytest.mark.unit


class TestErrorIsolation:
    """Test handler error isolation."""

    def test_workflow_error_handled(
        self, event_handlers: EventHandlers, mock_dependencies: dict[str, Any]
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

    def test_context_is_string(
        self,
        event_handlers: EventHandlers,
        mock_empty_session_variable_manager: MagicMock,
    ) -> None:
        """Test context is always a string."""
        event = make_event(HookEventType.SESSION_START)
        response = event_handlers.handle_session_start(event)
        assert isinstance(response.context, str)


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

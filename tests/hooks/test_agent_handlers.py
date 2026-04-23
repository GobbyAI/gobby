
"""Agent handler tests."""

from __future__ import annotations

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEventType

from ._event_handler_helpers import make_event

pytestmark = pytest.mark.unit


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

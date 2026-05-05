"""Session end handler tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEventType

from ._event_handler_helpers import make_event

pytestmark = pytest.mark.unit


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
        assert mock_dependencies["session_coordinator"].complete_agent_run.call_count == 1
        assert mock_dependencies["session_coordinator"].complete_agent_run.call_args is not None

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
        assert mock_dependencies["message_processor"].unregister_session.call_count == 1
        assert mock_dependencies["message_processor"].unregister_session.call_args is not None

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
        assert mock_dependencies["message_processor"].unregister_session.call_count == 1
        assert mock_dependencies["message_processor"].unregister_session.call_args is not None

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

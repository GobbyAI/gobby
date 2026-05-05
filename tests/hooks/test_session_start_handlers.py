"""Session start handler tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEventType

from ._event_handler_helpers import make_event

pytestmark = pytest.mark.unit


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

    def test_pre_created_session_backfills_terminal_context(self, mock_dependencies: dict) -> None:
        """Pre-created sessions should persist terminal metadata from runtime hooks."""
        mock_session = MagicMock()
        mock_session.id = "sess-pre-123"
        mock_session.project_id = "proj-123"
        mock_session.parent_session_id = None
        mock_session.agent_depth = 0
        mock_session.agent_run_id = None
        mock_session.title = "Useful synthesized title"
        mock_session.digest_markdown = None
        mock_session.terminal_context = None

        updated_session = MagicMock()
        updated_session.id = "sess-pre-123"
        updated_session.project_id = "proj-123"
        updated_session.parent_session_id = None
        updated_session.agent_depth = 0
        updated_session.agent_run_id = None
        updated_session.title = "Useful synthesized title"
        updated_session.digest_markdown = None
        updated_session.terminal_context = {"tmux_pane": "%77", "parent_pid": 123}

        mock_dependencies["session_storage"].get.return_value = mock_session
        mock_dependencies["session_storage"].update.return_value = mock_session
        mock_dependencies["session_manager"].backfill_terminal_context.return_value = (
            updated_session,
            True,
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="sess-pre-123",
            data={
                "transcript_path": "/path/to/transcript.jsonl",
                "terminal_context": {"tmux_pane": "%77", "parent_pid": 123},
            },
        )

        with patch(
            "gobby.hooks.event_handlers._session_start.schedule_tmux_window_rename"
        ) as mock_schedule:
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_dependencies["session_manager"].backfill_terminal_context.assert_called_once_with(
            "sess-pre-123",
            {"tmux_pane": "%77", "parent_pid": 123},
        )
        mock_schedule.assert_called_once()
        assert response.metadata.get("terminal_tmux_pane") == "%77"

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
        assert mock_dependencies["message_processor"].register_session.call_count == 1
        assert mock_dependencies["message_processor"].register_session.call_args is not None

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
            call_kwargs[1].get("parent_session_id") is None if call_kwargs[1] else True
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
        assert mock_dependencies["message_processor"].register_session.call_count == 1
        assert mock_dependencies["message_processor"].register_session.call_args is not None

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

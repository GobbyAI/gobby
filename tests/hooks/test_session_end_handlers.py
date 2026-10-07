"""Session end handler tests."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, call, patch

import psutil
import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEventType
from gobby.hooks.hook_types import SessionEndReason

from ._event_handler_helpers import make_event

pytestmark = pytest.mark.unit


class TestSessionEndHandling:
    """Test SESSION_END handler edge cases and error paths."""

    @pytest.mark.parametrize("replayed", [False, True], ids=["live", "restart-replay"])
    def test_session_end_preserves_live_recorded_cli(
        self, mock_dependencies: dict[str, Any], replayed: bool
    ) -> None:
        """A SessionEnd cannot retire a seat while its recorded CLI still runs."""
        session = MagicMock()
        session.id = "sess-123"
        session.status = "active"
        session.session_type = "terminal"
        session.agent_run_id = "run-123"
        session.machine_id = "21000000-0000-4000-8000-000000000008"
        session.terminal_context = {
            "parent_pid": os.getpid(),
            "parent_create_time": psutil.Process(os.getpid()).create_time(),
        }
        storage = mock_dependencies["session_storage"]
        storage.get.return_value = session
        worker = MagicMock()
        terminal_manager = MagicMock()
        handlers = EventHandlers(
            **mock_dependencies,
            session_end_auto_link_worker=worker,
            terminal_manager=terminal_manager,
            get_machine_id=lambda: session.machine_id,
        )
        processor = MagicMock()
        handlers._session_message_processors[session.id] = processor
        metadata: dict[str, Any] = {"_platform_session_id": session.id}
        if replayed:
            metadata["_enqueued_at"] = "2026-10-07T17:25:45Z"
        event = make_event(
            HookEventType.SESSION_END,
            session_id="codex-thread-123",
            source="codex",
            data={"reason": "exit"},
            metadata=metadata,
        )

        response = handlers.handle_session_end(event)

        assert response.decision == "allow"
        storage.update_status_if_non_terminal.assert_not_called()
        mock_dependencies["session_coordinator"].complete_agent_run.assert_not_called()
        worker.submit.assert_not_called()
        processor.unregister_session.assert_not_called()
        assert handlers._session_message_processors[session.id] is processor
        mock_dependencies["task_manager"].remove_label.assert_not_called()
        terminal_manager.release_session.assert_not_called()
        terminal_manager.mark_exited.assert_not_called()

    @pytest.mark.parametrize(
        ("identity", "reason", "expected_status"),
        [
            ("reused-pid", "exit", "expired"),
            ("missing", "exit", "expired"),
            ("foreign", "exit", "expired"),
            ("live", "compact", "awaiting_handoff"),
        ],
    )
    def test_session_end_preserves_real_exit_and_compact_semantics(
        self,
        mock_dependencies: dict[str, Any],
        identity: str,
        reason: str,
        expected_status: str,
    ) -> None:
        """Local PID reuse and foreign PIDs cannot impersonate a living seat."""
        session = MagicMock()
        session.id = "sess-123"
        session.status = "active"
        session.session_type = "terminal"
        session.agent_run_id = None
        session.machine_id = "foreign-machine" if identity == "foreign" else "local-machine"
        session.terminal_context = (
            {}
            if identity == "missing"
            else {
                "parent_pid": os.getpid(),
                "parent_create_time": psutil.Process(os.getpid()).create_time()
                - (3600 if identity == "reused-pid" else 0),
            }
        )
        storage = mock_dependencies["session_storage"]
        storage.get.return_value = session
        handlers = EventHandlers(**mock_dependencies, get_machine_id=lambda: "local-machine")
        event = make_event(
            HookEventType.SESSION_END,
            source="codex",
            data={"reason": reason},
            metadata={"_platform_session_id": session.id},
        )

        response = handlers.handle_session_end(event)

        assert response.decision == "allow"
        storage.update_status_if_non_terminal.assert_called_once_with(session.id, expected_status)

    def test_session_end_keeps_a_seat_when_process_inspection_is_denied(
        self, mock_dependencies: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An inaccessible recorded process is not proof of CLI exit."""
        session = MagicMock()
        session.id = "sess-123"
        session.session_type = "terminal"
        session.machine_id = "local-machine"
        session.terminal_context = {"parent_pid": 12345, "parent_create_time": 100.0}
        storage = mock_dependencies["session_storage"]
        storage.get.return_value = session
        process = MagicMock(side_effect=psutil.AccessDenied(12345))
        monkeypatch.setattr("gobby.terminal_ownership.psutil.Process", process)
        handlers = EventHandlers(**mock_dependencies, get_machine_id=lambda: "local-machine")
        event = make_event(HookEventType.SESSION_END, metadata={"_platform_session_id": session.id})

        assert handlers.handle_session_end(event).decision == "allow"
        storage.update_status_if_non_terminal.assert_not_called()

    def test_session_end_lookup_from_database(self, mock_dependencies: dict[str, Any]) -> None:
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
        event.machine_id = "21000000-0000-4000-8000-000000000008"

        response = handlers.handle_session_end(event)

        assert response.decision == "allow"
        mock_dependencies["session_manager"].lookup_session_id.assert_called_once()

    def test_session_end_workflow_error(self, mock_dependencies: dict[str, Any]) -> None:
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

    def test_session_end_auto_link_commits(self, mock_dependencies: dict[str, Any]) -> None:
        """Session end hands the commit auto-link to the managed worker."""
        from gobby.hooks.session_end_auto_link import SessionEndAutoLinkJob

        created_at = datetime(2024, 1, 1, tzinfo=UTC)
        mock_session = MagicMock()
        mock_session.id = "sess-uuid-1"
        mock_session.created_at = created_at
        mock_session.agent_run_id = None
        mock_session.project_id = "d45545c5-ded5-4335-b115-0245752edacf"
        mock_dependencies["session_storage"].get.return_value = mock_session

        worker = MagicMock()
        handlers = EventHandlers(**mock_dependencies, session_end_auto_link_worker=worker)
        event = make_event(
            HookEventType.SESSION_END,
            metadata={"_platform_session_id": "sess-123"},
            data={"cwd": "/some/dir"},
        )

        response = handlers.handle_session_end(event)

        assert response.decision == "allow"
        worker.submit.assert_called_once_with(
            SessionEndAutoLinkJob(
                session_id="sess-uuid-1",
                project_id=mock_session.project_id,
                created_at=created_at,
                cwd="/some/dir",
            )
        )

    def test_session_end_auto_link_error(self, mock_dependencies: dict[str, Any]) -> None:
        """A worker that refuses the job must not fail session end."""
        mock_session = MagicMock()
        mock_session.id = "sess-uuid-1"
        mock_session.created_at = datetime(2024, 1, 1, tzinfo=UTC)
        mock_session.agent_run_id = None
        mock_session.project_id = "d45545c5-ded5-4335-b115-0245752edacf"
        mock_dependencies["session_storage"].get.return_value = mock_session

        # The real failure mode: shutdown closed the worker between the hook
        # firing and the job being accepted.
        worker = MagicMock()
        worker.submit.side_effect = RuntimeError("session-end auto-link worker is closed")
        handlers = EventHandlers(**mock_dependencies, session_end_auto_link_worker=worker)
        event = make_event(
            HookEventType.SESSION_END,
            metadata={"_platform_session_id": "sess-123"},
            data={"cwd": "/some/dir"},
        )

        response = handlers.handle_session_end(event)

        # Should still allow despite the enqueue failure
        assert response.decision == "allow"
        worker.submit.assert_called_once()

    def test_session_end_complete_agent_run(self, mock_dependencies: dict[str, Any]) -> None:
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

        with patch("gobby.workflows.step_instances.AgentStepInstanceManager") as manager_cls:
            handlers.handle_session_end(event)

        mock_dependencies["session_coordinator"].complete_agent_run.assert_called_once()
        manager_cls.return_value.delete_for_session.assert_called_once_with("sess-123")
        assert mock_dependencies["session_coordinator"].complete_agent_run.call_count == 1
        assert mock_dependencies["session_coordinator"].complete_agent_run.call_args is not None

    def test_session_end_complete_agent_run_error(self, mock_dependencies: dict[str, Any]) -> None:
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

    def test_session_end_unregister_message_processor(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        """Test unregistering from message processor on session end."""
        handlers = EventHandlers(**mock_dependencies)
        registered_processor = mock_dependencies["message_processor_resolver"]()
        replacement_processor = MagicMock()
        handlers._session_message_processors["sess-123"] = registered_processor
        handlers._message_processor_resolver = lambda: replacement_processor
        event = make_event(
            HookEventType.SESSION_END,
            session_id="ext-123",
            metadata={"_platform_session_id": "sess-123"},
        )

        handlers.handle_session_end(event)

        registered_processor.unregister_session.assert_called_once_with("sess-123")
        replacement_processor.unregister_session.assert_not_called()
        assert "sess-123" not in handlers._session_message_processors

    def test_session_end_unregister_maps_external_id_to_platform_id(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        """An event without metadata unregisters the mapped platform session ID."""
        mock_dependencies["session_manager"].lookup_session_id.return_value = "mapped-sess-123"

        handlers = EventHandlers(**mock_dependencies)
        processor = mock_dependencies["message_processor_resolver"]()
        handlers._session_message_processors["mapped-sess-123"] = processor
        event = make_event(
            HookEventType.SESSION_END,
            session_id="ext-123",
            metadata={},  # No _platform_session_id
        )

        handlers.handle_session_end(event)

        assert processor.unregister_session.call_count == 1
        assert processor.unregister_session.call_args == call("mapped-sess-123")
        assert "mapped-sess-123" not in handlers._session_message_processors

    def test_session_end_lookup_miss_does_not_unregister_external_id(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        """An unknown external ID is never used as a processor registration key."""
        mock_dependencies["session_manager"].lookup_session_id.return_value = None
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_END,
            session_id="unknown-ext-123",
            metadata={},
        )

        response = handlers.handle_session_end(event)

        assert response.decision == "allow"
        mock_dependencies["message_processor_resolver"]().unregister_session.assert_not_called()

    def test_session_end_unregister_error(self, mock_dependencies: dict[str, Any]) -> None:
        """Test error unregistering from message processor is handled."""
        mock_dependencies[
            "message_processor_resolver"
        ]().unregister_session.side_effect = Exception("Unregister error")

        handlers = EventHandlers(**mock_dependencies)
        handlers._session_message_processors["sess-123"] = mock_dependencies[
            "message_processor_resolver"
        ]()
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
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict[str, Any]
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
        mock_dependencies["session_storage"].update_status_if_non_terminal.assert_called_once_with(
            "sess-123", "expired"
        )

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_session_end_marks_expired_with_clear_reason(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict[str, Any]
    ) -> None:
        """Test SESSION_END marks expired when event reason is 'clear'."""
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
        mock_dependencies["session_storage"].update_status_if_non_terminal.assert_called_once_with(
            "sess-123", "expired"
        )

    def test_session_end_marks_awaiting_handoff_with_compact_reason(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        """Test SESSION_END marks awaiting_handoff when event reason is 'compact'."""
        mock_session = MagicMock()
        mock_session.created_at = "2024-01-01T00:00:00Z"
        mock_session.agent_run_id = "run-456"
        mock_session.status = "active"
        mock_dependencies["session_storage"].get.return_value = mock_session

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_END,
            session_id="ext-123",
            data={"reason": "compact"},
            metadata={"_platform_session_id": "sess-123"},
        )

        with patch("gobby.workflows.step_instances.AgentStepInstanceManager") as manager_cls:
            response = handlers.handle_session_end(event)

        assert response.decision == "allow"
        mock_dependencies["session_storage"].update_status_if_non_terminal.assert_called_once_with(
            "sess-123", "awaiting_handoff"
        )
        mock_dependencies["session_coordinator"].complete_agent_run.assert_not_called()
        manager_cls.return_value.delete_for_session.assert_not_called()

    def test_session_end_leaves_awaiting_handoff_for_the_successor(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        """An ordinary SESSION_END never expires a clear predecessor; its successor's bind does."""
        mock_session = MagicMock()
        mock_session.created_at = "2024-01-01T00:00:00Z"
        mock_session.agent_run_id = None
        mock_session.status = "awaiting_handoff"
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
        mock_dependencies["session_storage"].update_status_if_non_terminal.assert_not_called()

    def test_session_end_resume_reason_expires_session(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        """Runtime resume on a live row is an ordinary exit."""
        mock_session = MagicMock()
        mock_session.created_at = "2024-01-01T00:00:00Z"
        mock_session.agent_run_id = None
        mock_session.status = "active"
        mock_dependencies["session_storage"].get.return_value = mock_session

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_END,
            session_id="ext-123",
            data={"reason": SessionEndReason.RESUME},
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_session_end(event)

        assert response.decision == "allow"
        mock_dependencies["session_storage"].update_status_if_non_terminal.assert_called_once_with(
            "sess-123", "expired"
        )

    def test_session_end_idle_reason_pauses_session(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        """Idle eviction keeps a durable web-chat row resumable."""
        mock_session = MagicMock()
        mock_session.created_at = "2024-01-01T00:00:00Z"
        mock_session.agent_run_id = "run-456"
        mock_session.session_type = "web_chat"
        mock_dependencies["session_storage"].get.return_value = mock_session

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_END,
            session_id="ext-123",
            data={"reason": "idle"},
            metadata={"_platform_session_id": "sess-123"},
        )

        with patch("gobby.workflows.step_instances.AgentStepInstanceManager") as manager_cls:
            response = handlers.handle_session_end(event)

        assert response.decision == "allow"
        mock_dependencies["session_storage"].update_status_if_non_terminal.assert_called_once_with(
            "sess-123", "paused"
        )
        mock_dependencies["session_storage"].update_status.assert_not_called()
        mock_dependencies["session_coordinator"].complete_agent_run.assert_not_called()
        manager_cls.return_value.delete_for_session.assert_not_called()

    def test_session_end_idle_reason_expires_terminal_session(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        mock_session = MagicMock()
        mock_session.created_at = "2024-01-01T00:00:00Z"
        mock_session.agent_run_id = None
        mock_session.session_type = "terminal"
        mock_session.terminal_context = {"parent_pid": 4242}
        mock_dependencies["session_storage"].get.return_value = mock_session

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_END,
            session_id="ext-123",
            data={"reason": "idle"},
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_session_end(event)

        assert response.decision == "allow"
        mock_dependencies["session_storage"].update_status_if_non_terminal.assert_called_once_with(
            "sess-123", "expired"
        )

    def test_session_end_pauses_interactive_tmux_session(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        mock_session = MagicMock()
        mock_session.created_at = "2024-01-01T00:00:00Z"
        mock_session.agent_run_id = "run-456"
        mock_session.session_type = "terminal"
        mock_session.terminal_context = {
            "tmux_socket_path": "/tmp/tmux-501/default",
            "tmux_window_id": "@7",
            "tmux_pane": "%6",
        }
        mock_dependencies["session_storage"].get.return_value = mock_session

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_END,
            session_id="ext-123",
            metadata={"_platform_session_id": "sess-123"},
        )

        with patch("gobby.workflows.step_instances.AgentStepInstanceManager") as manager_cls:
            response = handlers.handle_session_end(event)

        assert response.decision == "allow"
        mock_dependencies["session_storage"].update_status_if_non_terminal.assert_called_once_with(
            "sess-123", "paused"
        )
        mock_dependencies["session_coordinator"].complete_agent_run.assert_not_called()
        manager_cls.return_value.delete_for_session.assert_not_called()

    def test_session_end_awaiting_handoff_error_handled(
        self,
        mock_dependencies: dict[str, Any],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test error marking awaiting_handoff doesn't block response."""
        mock_session = MagicMock()
        mock_session.created_at = "2024-01-01T00:00:00Z"
        mock_session.agent_run_id = None
        mock_dependencies["session_storage"].get.return_value = mock_session
        mock_dependencies["session_storage"].update_status_if_non_terminal.side_effect = Exception(
            "DB write error"
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
        assert "sess-123" in caplog.text

    def test_session_end_marks_liveness_monitor_recently_handled(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        monitor = MagicMock()
        handlers = EventHandlers(**mock_dependencies)
        handlers.set_liveness_monitor(monitor)
        event = make_event(
            HookEventType.SESSION_END,
            session_id="ext-123",
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_session_end(event)

        assert response.decision == "allow"
        monitor.mark_recently_handled.assert_called_once_with("sess-123")

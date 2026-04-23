"""Miscellaneous event handler tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEventType

from ._event_handler_helpers import make_event

pytestmark = pytest.mark.unit


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


class TestWorktreeHandlers:
    """Test WORKTREE_CREATE and WORKTREE_REMOVE default behavior."""

    def test_worktree_create_returns_created_path(self, mock_dependencies: dict) -> None:
        mock_dependencies["worktree_manager"].get_by_branch.return_value = None

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.WORKTREE_CREATE,
            data={"name": "feature-auth"},
            source="claude",
        )

        git_manager = MagicMock()
        git_manager.repo_path = "/repo"
        git_manager.get_current_branch.return_value = "main"
        git_manager.has_unpushed_commits.return_value = (False, 0)
        git_manager.create_worktree.return_value = MagicMock(success=True, message="ok")

        with (
            patch(
                "gobby.hooks.event_handlers._misc.resolve_project_context",
                return_value=(git_manager, "proj-123", None),
            ),
            patch(
                "gobby.hooks.event_handlers._misc.generate_worktree_path",
                return_value="/tmp/worktrees/feature-auth",
            ),
            patch("gobby.hooks.event_handlers._misc.copy_project_json_to_worktree"),
            patch("gobby.hooks.event_handlers._misc.install_provider_hooks"),
        ):
            response = handlers.handle_worktree_create(event)

        assert response.worktree_path == "/tmp/worktrees/feature-auth"
        git_manager.create_worktree.assert_called_once_with(
            worktree_path="/tmp/worktrees/feature-auth",
            branch_name="feature-auth",
            base_branch="main",
            create_branch=True,
            use_local=False,
        )
        mock_dependencies["worktree_manager"].create.assert_called_once_with(
            project_id="proj-123",
            branch_name="feature-auth",
            worktree_path="/tmp/worktrees/feature-auth",
            base_branch="main",
        )

    def test_worktree_remove_deletes_git_worktree_and_record(self, mock_dependencies: dict) -> None:
        mock_dependencies["worktree_manager"].get_by_path.return_value = MagicMock(id="wt-123")

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.WORKTREE_REMOVE,
            data={"worktree_path": "/tmp/worktrees/feature-auth"},
            source="claude",
        )

        with (
            patch(
                "gobby.hooks.event_handlers._misc.get_workflow_project_path",
                return_value=Path("/repo"),
            ),
            patch("gobby.hooks.event_handlers._misc.WorktreeGitManager") as mock_git_cls,
        ):
            mock_git_manager = mock_git_cls.return_value
            mock_git_manager.delete_worktree.return_value = MagicMock(success=True, message="ok")
            response = handlers.handle_worktree_remove(event)

        assert response.decision == "allow"
        mock_git_manager.delete_worktree.assert_called_once_with(
            worktree_path="/tmp/worktrees/feature-auth",
            force=True,
        )
        mock_dependencies["worktree_manager"].delete.assert_called_once_with("wt-123")


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

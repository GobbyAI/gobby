"""Miscellaneous event handler tests."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEventType
from gobby.sessions.compact_continuation import COMPACT_HANDOFF_MARKER_VARIABLE
from gobby.sessions.compact_markers import COMPACT_HANDOFF_INJECT_PENDING_VARIABLE
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.observer_context_usage import detect_context_compact_guidance
from gobby.workflows.state_manager import SessionVariableManager

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


class TestPostCompactHandler:
    @pytest.mark.parametrize("source", ["claude", "qwen", "codex", "droid", "grok", "agy"])
    def test_resets_context_pressure_for_compacting_cli(
        self,
        mock_dependencies: dict,
        source: str,
    ) -> None:
        session_manager = mock_dependencies["session_manager"]
        session = SimpleNamespace(
            source=source,
            model="provider-model",
            context_window=258_400,
            context_used_tokens=222_353,
            context_usage_ratio=222_353 / 258_400,
        )
        session_manager.get.return_value = session
        session_manager.update_context_usage.return_value = True
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.POST_COMPACT,
            source=source,
            metadata={"_platform_session_id": "session-1"},
        )

        response = handlers.handle_post_compact(event)

        assert response.decision == "allow"
        session_manager.update_context_usage.assert_called_once()
        session_id, snapshot = session_manager.update_context_usage.call_args.args
        assert session_id == "session-1"
        assert snapshot.source == source
        assert snapshot.model == "provider-model"
        assert snapshot.context_window == 258_400
        assert snapshot.context_used_tokens is None
        assert snapshot.context_usage_ratio is None
        assert snapshot.confidence == "unknown"

    def test_reset_pressure_is_absent_from_next_turn_guidance(
        self,
        mock_dependencies: dict,
    ) -> None:
        session_manager = mock_dependencies["session_manager"]
        session = SimpleNamespace(
            source="codex",
            model="gpt-5.6-sol",
            context_window=258_400,
            context_used_tokens=222_353,
            context_usage_ratio=222_353 / 258_400,
            context_compact_soft_ratio=None,
            context_compact_strong_ratio=None,
        )
        session_manager.get.return_value = session

        def persist_context(_session_id: str, snapshot: Any) -> bool:
            session.context_used_tokens = snapshot.context_used_tokens
            session.context_usage_ratio = snapshot.context_usage_ratio
            return True

        session_manager.update_context_usage.side_effect = persist_context
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.POST_COMPACT,
            source="codex",
            metadata={"_platform_session_id": "session-1"},
        )
        handlers.handle_post_compact(event)
        variables: dict[str, Any] = {"parent_turn_seq": 1, "turns_since_compact": 0}

        detect_context_compact_guidance(variables, "session-1", session_manager)

        assert variables["context_compact_guidance_kind"] == ""
        assert variables["context_compact_guidance_message"] == ""

    def test_missing_session_is_logged_and_allowed(
        self,
        mock_dependencies: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.WARNING, logger="test")
        mock_dependencies["session_manager"].get.return_value = None
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.POST_COMPACT,
            metadata={"_platform_session_id": "missing-session"},
        )

        response = handlers.handle_post_compact(event)

        assert response.decision == "allow"
        assert "session missing-session was not found" in caplog.text

    def test_missing_platform_session_id_is_logged_and_allowed(
        self,
        mock_dependencies: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.WARNING, logger="test")
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(HookEventType.POST_COMPACT)

        response = handlers.handle_post_compact(event)

        assert response.decision == "allow"
        assert "missing platform session id" in caplog.text

    def test_persistence_failure_is_logged_and_allowed(
        self,
        mock_dependencies: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.WARNING, logger="test")
        session_manager = mock_dependencies["session_manager"]
        session_manager.get.return_value = SimpleNamespace(
            source="codex",
            model="gpt-5.6-sol",
            context_window=258_400,
        )
        session_manager.update_context_usage.return_value = False
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.POST_COMPACT,
            source="codex",
            metadata={"_platform_session_id": "session-1"},
        )

        response = handlers.handle_post_compact(event)

        assert response.decision == "allow"
        assert "failed to reset context usage for session session-1" in caplog.text

    def test_non_grok_post_compact_does_not_apply_in_place_closeout(
        self,
        hub_db: HubDatabase,
        mock_dependencies: dict[str, Any],
    ) -> None:
        project = LocalProjectManager(hub_db).create(
            name="post-compact-claude",
            repo_path="/some/dir",
        )
        with patch(
            "gobby.utils.machine_id._cached_machine_id", "21000000-0000-4000-8000-000000000001"
        ):
            session = SessionManager(hub_db).register(
                external_id="cccccccc-0000-4000-8000-000000000001",
                machine_id="21000000-0000-4000-8000-000000000001",
                source="claude",
                project_id=project.id,
                terminal_context={"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"},
            )
        sv_mgr = SessionVariableManager(hub_db)
        sv_mgr.merge_variables(
            session.id,
            {
                COMPACT_HANDOFF_MARKER_VARIABLE: "compact",
                "unlocked_tools": ["call_tool"],
                "plan_mode": True,
            },
        )
        mock_dependencies["session_manager"].db = hub_db
        mock_dependencies["session_manager"].get.return_value = session
        mock_dependencies["session_manager"].update_context_usage.return_value = True
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.POST_COMPACT,
            source="claude",
            metadata={"_platform_session_id": session.id},
        )

        response = handlers.handle_post_compact(event)

        assert response.decision == "allow"
        variables = sv_mgr.get_variables(session.id)
        assert variables[COMPACT_HANDOFF_MARKER_VARIABLE] == "compact"
        assert COMPACT_HANDOFF_INJECT_PENDING_VARIABLE not in variables
        assert variables["unlocked_tools"] == ["call_tool"]
        assert variables["plan_mode"] is True


class TestAcpOnlyHandlers:
    """Test ACP-style event handlers."""

    def test_before_tool_selection_allows(self, event_handlers: EventHandlers) -> None:
        """Test BEFORE_TOOL_SELECTION allows for ACP providers."""
        event = make_event(HookEventType.BEFORE_TOOL_SELECTION, source="qwen")
        response = event_handlers.handle_before_tool_selection(event)
        assert response.decision == "allow"

    def test_before_model_allows(self, event_handlers: EventHandlers) -> None:
        """Test BEFORE_MODEL allows for ACP providers."""
        event = make_event(HookEventType.BEFORE_MODEL, source="qwen")
        response = event_handlers.handle_before_model(event)
        assert response.decision == "allow"

    def test_after_model_allows(self, event_handlers: EventHandlers) -> None:
        """Test AFTER_MODEL allows for ACP providers."""
        event = make_event(HookEventType.AFTER_MODEL, source="qwen")
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
        assert mock_dependencies["session_manager"].update_session_status.call_count == 1
        assert mock_dependencies["session_manager"].update_session_status.call_args is not None

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

    def test_pre_compact_qwen_skips_handoff(self, mock_dependencies: dict) -> None:
        """Test PRE_COMPACT skips handoff logic for Qwen source.

        Qwen fires PreCompress constantly during normal operation,
        unlike Claude which fires it only when approaching context limits.
        """
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.PRE_COMPACT,
            source="qwen",
            data={"trigger": "auto"},
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_pre_compact(event)

        assert response.decision == "allow"
        # Should NOT update session status for Qwen
        mock_dependencies["session_manager"].update_session_status.assert_not_called()
        # Should NOT execute workflow handler for Qwen
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
            "sess-123",
            "paused",
            activity_confirmed=True,
        )
        assert mock_dependencies["session_manager"].update_session_status.call_count == 1
        assert mock_dependencies["session_manager"].update_session_status.call_args is not None

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

    @pytest.mark.parametrize(
        ("data", "expected_type"),
        [
            ({"notificationType": "warning"}, "warning"),
            ({"type": "error"}, "error"),
            ({"level": "warning"}, "warning"),
            ({"severity": "error"}, "error"),
            ({}, "general"),
        ],
    )
    def test_notification_type_variants(
        self,
        mock_dependencies: dict,
        caplog: pytest.LogCaptureFixture,
        data: dict,
        expected_type: str,
    ) -> None:
        """Test NOTIFICATION handles different type field names."""
        handlers = EventHandlers(**mock_dependencies)

        event = make_event(
            HookEventType.NOTIFICATION,
            data=data,
        )

        with caplog.at_level("DEBUG"):
            response = handlers.handle_notification(event)

        assert response.decision == "allow"
        assert f"NOTIFICATION ({expected_type})" in caplog.text

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
        assert git_manager.create_worktree.call_count == 1
        assert git_manager.create_worktree.call_args is not None
        mock_dependencies["worktree_manager"].create.assert_called_once_with(
            project_id="proj-123",
            branch_name="feature-auth",
            worktree_path="/tmp/worktrees/feature-auth",
            base_branch="main",
        )
        assert mock_dependencies["worktree_manager"].create.call_count == 1
        assert mock_dependencies["worktree_manager"].create.call_args is not None

    def test_worktree_remove_deletes_git_worktree_and_record(self, mock_dependencies: dict) -> None:
        mock_dependencies["worktree_manager"].has_path_on_other_machine.return_value = False
        mock_dependencies["worktree_manager"].get_by_path.return_value = MagicMock(
            id="wt-123",
            branch_name="feature-auth",
            base_branch="main",
        )

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
            delete_branch=True,
            branch_name="feature-auth",
            base_branch="main",
        )
        assert mock_git_manager.delete_worktree.call_count == 1
        assert mock_git_manager.delete_worktree.call_args is not None
        mock_dependencies["worktree_manager"].delete.assert_called_once_with("wt-123")
        assert mock_dependencies["worktree_manager"].delete.call_count == 1
        assert mock_dependencies["worktree_manager"].delete.call_args is not None

    def test_worktree_remove_ignores_remote_record(self, mock_dependencies: dict) -> None:
        worktree_manager = mock_dependencies["worktree_manager"]
        worktree_manager.has_path_on_other_machine.return_value = True
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.WORKTREE_REMOVE,
            data={"worktree_path": "/shared/worktrees/remote"},
            source="claude",
        )

        with patch("gobby.hooks.event_handlers._misc.WorktreeGitManager") as mock_git_cls:
            response = handlers.handle_worktree_remove(event)

        assert response.decision == "allow"
        worktree_manager.has_path_on_other_machine.assert_called_once_with(
            "/shared/worktrees/remote"
        )
        worktree_manager.get_by_path.assert_not_called()
        worktree_manager.delete.assert_not_called()
        mock_git_cls.assert_not_called()


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


class TestAcpHandlerEdgeCases:
    """Test ACP-style handler edge cases."""

    def test_before_tool_selection_with_session_id(self, mock_dependencies: dict) -> None:
        """Test BEFORE_TOOL_SELECTION with session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_TOOL_SELECTION,
            source="qwen",
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_before_tool_selection(event)

        assert response.decision == "allow"

    def test_before_tool_selection_no_session_id(self, mock_dependencies: dict) -> None:
        """Test BEFORE_TOOL_SELECTION handles missing session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_TOOL_SELECTION,
            source="qwen",
            metadata={},
        )

        response = handlers.handle_before_tool_selection(event)

        assert response.decision == "allow"

    def test_before_model_with_session_id(self, mock_dependencies: dict) -> None:
        """Test BEFORE_MODEL with session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_MODEL,
            source="qwen",
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_before_model(event)

        assert response.decision == "allow"

    def test_before_model_no_session_id(self, mock_dependencies: dict) -> None:
        """Test BEFORE_MODEL handles missing session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_MODEL,
            source="qwen",
            metadata={},
        )

        response = handlers.handle_before_model(event)

        assert response.decision == "allow"

    def test_after_model_with_session_id(self, mock_dependencies: dict) -> None:
        """Test AFTER_MODEL with session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_MODEL,
            source="qwen",
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_after_model(event)

        assert response.decision == "allow"

    def test_after_model_no_session_id(self, mock_dependencies: dict) -> None:
        """Test AFTER_MODEL handles missing session_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_MODEL,
            source="qwen",
            metadata={},
        )

        response = handlers.handle_after_model(event)

        assert response.decision == "allow"

    def test_after_model_de_overlaps_cached_and_thinking_tokens(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        """Typed-JSON usage must de-overlap cached input and fold thinking into output."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.AFTER_MODEL,
            source="qwen",
            data={
                "response": {
                    "usageMetadata": {
                        "promptTokenCount": 1_000,
                        "cachedContentTokenCount": 750,
                        "candidatesTokenCount": 80,
                        "thoughtsTokenCount": 20,
                    }
                },
                "model_name": "qwen3-coder",
            },
            metadata={"_platform_session_id": "sess-123"},
        )

        with patch("gobby.hooks.event_handlers._misc.get_app_context", return_value=None):
            response = handlers.handle_after_model(event)

        assert response.decision == "allow"
        mock_dependencies["session_manager"].update_usage.assert_called_once_with(
            session_id="sess-123",
            input_tokens=250,
            output_tokens=100,
            cache_creation_tokens=0,
            cache_read_tokens=750,
            model="qwen3-coder",
        )

    @pytest.mark.asyncio
    async def test_after_model_worker_thread_broadcasts_usage_on_daemon_loop(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        loop = asyncio.get_running_loop()
        handlers = EventHandlers(event_loop=loop, **mock_dependencies)
        event = make_event(
            HookEventType.AFTER_MODEL,
            source="qwen",
            data={
                "response": {
                    "usageMetadata": {
                        "promptTokenCount": 120,
                        "candidatesTokenCount": 30,
                    }
                },
                "model_name": "qwen3-coder",
            },
            metadata={"_platform_session_id": "sess-123"},
        )
        refreshed = MagicMock(
            project_id="project-1",
            model="qwen3-coder",
            context_window=262_144,
            usage_input_tokens=120,
            usage_output_tokens=30,
            usage_cache_creation_tokens=0,
            usage_cache_read_tokens=0,
        )
        mock_dependencies["session_manager"].get.return_value = refreshed
        broadcasts = []
        broadcasted = asyncio.Event()

        async def broadcast_usage(payload: dict[str, Any]) -> None:
            broadcasts.append(payload)
            broadcasted.set()

        websocket_server = MagicMock()
        websocket_server.broadcast_session_usage_updated = broadcast_usage
        app_context = MagicMock(websocket_server=websocket_server)

        with patch("gobby.hooks.event_handlers._misc.get_app_context", return_value=app_context):
            response = await asyncio.to_thread(handlers.handle_after_model, event)
            await asyncio.wait_for(broadcasted.wait(), timeout=1)

        assert response.decision == "allow"
        assert len(broadcasts) == 1
        assert broadcasts[0]["session_id"] == "sess-123"
        assert broadcasts[0]["usage_input_tokens"] == 120
        assert broadcasts[0]["usage_output_tokens"] == 30


class TestSubagentHandlerWithSessionId:
    """Test SUBAGENT handlers with session_id for log coverage."""

    def test_subagent_stop_with_session_id(self, mock_dependencies: dict[str, Any]) -> None:
        """Test SUBAGENT_STOP with session_id present."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SUBAGENT_STOP,
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_subagent_stop(event)

        assert response.decision == "allow"

    def test_subagent_start_without_subagent_id(self, mock_dependencies: dict[str, Any]) -> None:
        """Test SUBAGENT_START without subagent_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SUBAGENT_START,
            data={"agent_id": "agent-123"},  # No subagent_id
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_subagent_start(event)

        assert response.decision == "allow"

    def test_subagent_start_without_agent_id(self, mock_dependencies: dict[str, Any]) -> None:
        """Test SUBAGENT_START without agent_id."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SUBAGENT_START,
            data={},  # No agent_id or subagent_id
            metadata={"_platform_session_id": "sess-123"},
        )

        response = handlers.handle_subagent_start(event)

        assert response.decision == "allow"

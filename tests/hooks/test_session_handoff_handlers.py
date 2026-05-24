"""Session handoff handler tests."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.event_handlers._session_start.handoff import find_parent_session
from gobby.hooks.events import HookEventType
from gobby.sessions.compact_continuation import (
    COMPACT_SELF_CONTINUE_VARIABLE,
    consume_compact_self_continuation_pending,
    mark_compact_self_continuation_pending,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.state_manager import SessionVariableManager

from ._event_handler_helpers import make_event

pytestmark = pytest.mark.unit


class TestSessionStartHandoff:
    """Test session handoff context injection on /clear and /compact."""

    def _make_db(self, hub_db: HubDatabase) -> HubDatabase:
        return hub_db

    def _make_precreated_session(self, session_id: str = "sess-compact") -> MagicMock:
        session = MagicMock()
        session.id = session_id
        session.seq_num = 45
        session.project_id = "proj-1"
        session.parent_session_id = None
        session.terminal_context = {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"}
        session.agent_run_id = None
        session.workflow_name = None
        return session

    def _fake_compact_self_consumer(self, scheduled: list[tuple[object, str]]):
        def _consume(
            db: HubDatabase,
            *,
            pending_session_id: str | None,
            target_session: object,
            fallback_pending_session_id: str | None = None,
            loop: object | None = None,
        ) -> bool:
            _ = loop
            prompt = None
            if pending_session_id:
                prompt = consume_compact_self_continuation_pending(db, pending_session_id)
            if prompt is None and fallback_pending_session_id != pending_session_id:
                if fallback_pending_session_id:
                    prompt = consume_compact_self_continuation_pending(
                        db,
                        fallback_pending_session_id,
                    )
            if prompt is None:
                return False
            scheduled.append((target_session, prompt))
            return True

        return _consume

    def test_compact_parent_miss_logs_below_warning(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A no-parent /compact restart is the safe no-handoff path."""
        handler = MagicMock()
        handler._session_manager.find_parent.return_value = None
        handler.logger = logging.getLogger("test.compact_parent_miss")
        input_data = {"source": "compact"}
        caplog.set_level(logging.INFO, logger=handler.logger.name)

        with patch(
            "gobby.hooks.event_handlers._session_start.handoff.time.monotonic",
            side_effect=[10.0, 16.0],
        ):
            parent_session_id, session_source = find_parent_session(
                handler,
                input_data,
                "compact",
                "machine-1",
                "project-1",
                "codex",
            )

        assert parent_session_id is None
        assert session_source == "startup"
        assert input_data["source"] == "startup"
        assert "No handoff_ready parent found for /compact" in caplog.text
        assert all(record.levelno < logging.WARNING for record in caplog.records)

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_session_start_compact_finds_parent(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict
    ) -> None:
        """Test parent lookup works for source='compact'."""
        mock_sv_mgr_cls.return_value = MagicMock(get_variables=MagicMock(return_value={}))

        mock_parent = MagicMock()
        mock_parent.id = "parent-sess-123"
        mock_parent.terminal_context = {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"}

        mock_dependencies["session_storage"].get.return_value = None
        mock_dependencies["session_storage"].find_parent.return_value = mock_parent
        mock_dependencies["session_manager"].register_session.return_value = "new-sess-456"

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={
                "source": "compact",
                "cwd": "/some/dir",
                "terminal_context": {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"},
            },
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
        mock_parent_for_find.terminal_context = {
            "tmux_pane": "%12",
            "tmux_socket_path": "/tmp/tmux",
        }

        mock_parent_obj = MagicMock()
        mock_parent_obj.id = "parent-sess-123"
        mock_parent_obj.seq_num = 42
        mock_parent_obj.summary_markdown = "# Summary\nWorked on feature X"
        mock_parent_obj.terminal_context = mock_parent_for_find.terminal_context

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
            data={
                "source": "clear",
                "cwd": "/some/dir",
                "terminal_context": {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"},
            },
            metadata={},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_sv_mgr.merge_variables.assert_any_call(
            "new-sess-456",
            {
                "session_summary": "# Summary\nWorked on feature X",
                "full_session_summary": "# Summary\nWorked on feature X",
            },
        )
        assert mock_sv_mgr.merge_variables.call_count >= 1
        assert mock_sv_mgr.merge_variables.call_args is not None

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_session_start_compact_sets_compact_session_summary_variable(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict
    ) -> None:
        """Test summary_markdown set as all session summary variables for source='compact'."""
        mock_sv_mgr = MagicMock()
        mock_sv_mgr.get_variables.return_value = {"auto_inject_handoff": True}
        mock_sv_mgr_cls.return_value = mock_sv_mgr

        mock_parent_for_find = MagicMock()
        mock_parent_for_find.id = "parent-sess-123"
        mock_parent_for_find.terminal_context = {
            "tmux_pane": "%12",
            "tmux_socket_path": "/tmp/tmux",
        }

        mock_parent_obj = MagicMock()
        mock_parent_obj.id = "parent-sess-123"
        mock_parent_obj.seq_num = 42
        mock_parent_obj.summary_markdown = "# Compact\nContinuation of task Y"
        mock_parent_obj.terminal_context = mock_parent_for_find.terminal_context

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
            data={
                "source": "compact",
                "cwd": "/some/dir",
                "terminal_context": {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"},
            },
            metadata={},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_sv_mgr.merge_variables.assert_any_call(
            "new-sess-456",
            {
                "session_summary": "# Compact\nContinuation of task Y",
                "full_session_summary": "# Compact\nContinuation of task Y",
            },
        )
        assert mock_sv_mgr.merge_variables.call_count >= 1
        assert mock_sv_mgr.merge_variables.call_args is not None

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_compact_handoff_refreshes_existing_parent_summary_before_injecting(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict
    ) -> None:
        """Existing parent summaries must be refreshed before /compact handoff injection."""
        mock_sv_mgr = MagicMock()
        mock_sv_mgr.get_variables.return_value = {"auto_inject_handoff": True}
        mock_sv_mgr_cls.return_value = mock_sv_mgr

        mock_parent_for_find = MagicMock()
        mock_parent_for_find.id = "parent-sess-123"
        mock_parent_for_find.terminal_context = {
            "tmux_pane": "%12",
            "tmux_socket_path": "/tmp/tmux",
        }

        stale_parent = MagicMock()
        stale_parent.id = "parent-sess-123"
        stale_parent.seq_num = 42
        stale_parent.summary_markdown = "# Old\nStale coordinator handoff"
        stale_parent.terminal_context = mock_parent_for_find.terminal_context

        refreshed_parent = MagicMock()
        refreshed_parent.id = "parent-sess-123"
        refreshed_parent.seq_num = 42
        refreshed_parent.summary_markdown = "# Fresh\nCurrent compact handoff"
        refreshed_parent.terminal_context = mock_parent_for_find.terminal_context

        mock_new_session = MagicMock()
        mock_new_session.seq_num = 43

        mock_dependencies["session_storage"].get.side_effect = [
            None,  # pre-created session check
            stale_parent,  # initial parent fetch before summary refresh
            refreshed_parent,  # parent refetch after summary generation
            mock_new_session,  # fetch session for seq_num
        ]
        mock_dependencies["session_storage"].find_parent.return_value = mock_parent_for_find
        mock_dependencies["session_manager"].register_session.return_value = "new-sess-456"

        dispatch_calls: list[tuple[str, bool, Any, bool]] = []

        def dispatch_summary(
            session_id: str,
            background: bool,
            done_event: Any,
            set_handoff_ready: bool,
        ) -> None:
            dispatch_calls.append((session_id, background, done_event, set_handoff_ready))
            done_event.set()

        handlers = EventHandlers(**mock_dependencies)
        handlers._dispatch_session_summaries_fn = dispatch_summary
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={
                "source": "compact",
                "cwd": "/some/dir",
                "terminal_context": {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"},
            },
            metadata={},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        assert len(dispatch_calls) == 1
        assert dispatch_calls[0][0] == "parent-sess-123"
        assert dispatch_calls[0][1] is True
        assert dispatch_calls[0][3] is False
        mock_sv_mgr.merge_variables.assert_any_call(
            "new-sess-456",
            {
                "session_summary": "# Fresh\nCurrent compact handoff",
                "full_session_summary": "# Fresh\nCurrent compact handoff",
            },
        )
        merged_payloads = [args[1] for args, _kwargs in mock_sv_mgr.merge_variables.call_args_list]
        assert all(
            "# Old\nStale coordinator handoff" not in payload.values()
            for payload in merged_payloads
        )

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_compact_handoff_ignores_stale_parent_from_different_terminal(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict
    ) -> None:
        """A native-helper session must not inherit stale coordinator compact context."""
        parent_vars = {
            "handoff_source": "compact",
            "task_claimed": True,
            "claimed_tasks": {"coordination-task-uuid": "#14997"},
        }
        mock_sv_mgr = MagicMock()
        mock_sv_mgr.get_variables.side_effect = lambda sid: (
            parent_vars if sid == "session-5815" else {"auto_inject_handoff": True}
        )
        mock_sv_mgr_cls.return_value = mock_sv_mgr

        stale_parent = MagicMock()
        stale_parent.id = "session-5815"
        stale_parent.terminal_context = {
            "tmux_pane": "%5815",
            "tmux_socket_path": "/tmp/gobby-tmux",
        }
        stale_parent.summary_markdown = (
            "Build coordinator handoff for #12746 / coordination task #14997."
        )

        new_session = MagicMock()
        new_session.seq_num = 5867

        mock_dependencies["session_storage"].get.side_effect = [
            None,
            new_session,
        ]
        mock_dependencies["session_storage"].find_parent.return_value = stale_parent
        mock_dependencies["session_manager"].register_session.return_value = "session-5867"
        mock_dependencies["session_task_manager"] = MagicMock()

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="019e5137-53a9-7a20-be96-cdcb5f168f62",
            data={
                "source": "compact",
                "cwd": "/some/dir",
                "terminal_context": {
                    "tmux_pane": "%5867",
                    "tmux_socket_path": "/tmp/gobby-tmux",
                },
            },
            metadata={"first_user_prompt": "Fix Native Helper Precedence And Version Floors"},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        assert "Parent session: session-5815" not in (response.context or "")
        assert "Build coordinator handoff" not in (response.context or "")
        mock_sv_mgr.merge_variables.assert_not_called()
        mock_dependencies["task_manager"].claim_task.assert_not_called()

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
        mock_parent_for_find.terminal_context = {
            "tmux_pane": "%12",
            "tmux_socket_path": "/tmp/tmux",
        }

        mock_parent_obj = MagicMock()
        mock_parent_obj.id = "parent-sess-123"
        mock_parent_obj.seq_num = 42
        mock_parent_obj.summary_markdown = "# Compact\nContinuation"
        mock_parent_obj.terminal_context = mock_parent_for_find.terminal_context

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
        claimed_task = MagicMock(
            status="in_progress",
            assignee="parent-sess-123",
            current_stage={"state": "in_progress"},
        )
        mock_dependencies["task_manager"].get_task.return_value = claimed_task

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={
                "source": "compact",
                "cwd": "/some/dir",
                "terminal_context": {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"},
            },
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
        assert mock_sv_mgr.merge_variables.call_count >= 1
        assert mock_sv_mgr.merge_variables.call_args is not None
        mock_dependencies["task_manager"].claim_task.assert_called_once_with(
            "uuid-123",
            session_id="new-sess-456",
            force=True,
        )
        assert mock_dependencies["task_manager"].claim_task.call_count == 1
        assert mock_dependencies["task_manager"].claim_task.call_args is not None
        mock_dependencies["session_task_manager"].link_task.assert_called_once_with(
            "new-sess-456", "uuid-123", "claimed"
        )
        assert mock_dependencies["session_task_manager"].link_task.call_count == 1
        assert mock_dependencies["session_task_manager"].link_task.call_args is not None

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
        mock_parent_for_find.terminal_context = {
            "tmux_pane": "%12",
            "tmux_socket_path": "/tmp/tmux",
        }

        mock_parent_obj = MagicMock()
        mock_parent_obj.id = "parent-sess-123"
        mock_parent_obj.seq_num = 42
        mock_parent_obj.summary_markdown = "# Compact\nDone"
        mock_parent_obj.terminal_context = mock_parent_for_find.terminal_context

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
            data={
                "source": "compact",
                "cwd": "/some/dir",
                "terminal_context": {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"},
            },
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
        mock_dependencies["task_manager"].claim_task.assert_not_called()
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
        mock_parent_for_find.terminal_context = {
            "tmux_pane": "%12",
            "tmux_socket_path": "/tmp/tmux",
        }

        mock_parent_obj = MagicMock()
        mock_parent_obj.id = "parent-sess-500"
        mock_parent_obj.seq_num = 50
        mock_parent_obj.summary_markdown = "# Summary\nCleared session"
        mock_parent_obj.terminal_context = mock_parent_for_find.terminal_context

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
        claimed_task = MagicMock(
            status="needs_review",
            assignee="parent-sess-500",
            current_stage={"state": "needs_review"},
        )
        mock_dependencies["task_manager"].get_task.return_value = claimed_task

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-456",
            data={
                "source": "clear",
                "cwd": "/some/dir",
                "terminal_context": {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"},
            },
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
        assert mock_sv_mgr.merge_variables.call_count >= 1
        assert mock_sv_mgr.merge_variables.call_args is not None
        mock_dependencies["task_manager"].claim_task.assert_called_once_with(
            "uuid-789",
            session_id="new-sess-600",
            force=True,
        )
        assert mock_dependencies["task_manager"].claim_task.call_count == 1
        assert mock_dependencies["task_manager"].claim_task.call_args is not None
        mock_dependencies["session_task_manager"].link_task.assert_called_once_with(
            "new-sess-600", "uuid-789", "claimed"
        )
        assert mock_dependencies["session_task_manager"].link_task.call_count == 1
        assert mock_dependencies["session_task_manager"].link_task.call_args is not None

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_task_claim_handoff_skips_reassignment_when_owned_elsewhere(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict
    ) -> None:
        """Compact handoff should not steal a task already assigned elsewhere."""
        parent_vars = {
            "task_claimed": True,
            "claimed_tasks": {"uuid-321": "#321"},
        }
        mock_sv_mgr = MagicMock()
        mock_sv_mgr.get_variables.side_effect = lambda sid: (
            parent_vars if sid == "parent-sess-123" else {"auto_inject_handoff": True}
        )
        mock_sv_mgr_cls.return_value = mock_sv_mgr

        mock_parent_for_find = MagicMock()
        mock_parent_for_find.id = "parent-sess-123"
        mock_parent_for_find.terminal_context = {
            "tmux_pane": "%12",
            "tmux_socket_path": "/tmp/tmux",
        }

        mock_parent_obj = MagicMock()
        mock_parent_obj.id = "parent-sess-123"
        mock_parent_obj.seq_num = 42
        mock_parent_obj.summary_markdown = "# Compact\nContinuation"
        mock_parent_obj.terminal_context = mock_parent_for_find.terminal_context

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
        mock_dependencies["task_manager"].get_task.return_value = MagicMock(
            status="needs_review",
            assignee="other-session",
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={
                "source": "compact",
                "cwd": "/some/dir",
                "terminal_context": {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"},
            },
            metadata={},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_dependencies["task_manager"].claim_task.assert_not_called()
        assert mock_dependencies["task_manager"].claim_task.call_count == 0
        assert not mock_dependencies["task_manager"].claim_task.called
        mock_dependencies["session_task_manager"].link_task.assert_not_called()
        assert mock_dependencies["session_task_manager"].link_task.call_count == 0
        assert not mock_dependencies["session_task_manager"].link_task.called

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

    def test_compact_start_with_pending_flag_clears_and_schedules_continuation(
        self, hub_db: HubDatabase, mock_dependencies: dict
    ) -> None:
        """A self-initiated compact schedules one continuation when the pending flag is fresh."""
        db = self._make_db(hub_db)
        session = self._make_precreated_session()
        mark_compact_self_continuation_pending(db, session.id)
        mock_dependencies["session_storage"].db = db
        mock_dependencies["session_storage"].get.return_value = session
        mock_dependencies["task_manager"].list_tasks.return_value = []

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id=session.id,
            data={"source": "compact", "cwd": "/some/dir"},
            metadata={},
        )

        scheduled: list[tuple[object, str]] = []
        with (
            patch.object(handlers, "_activate_default_agent", return_value=None),
            patch(
                "gobby.hooks.event_handlers._session_start.consume_and_schedule_compact_self_continuation",
                side_effect=self._fake_compact_self_consumer(scheduled),
            ) as mock_schedule,
        ):
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        variables = SessionVariableManager(db).get_variables(session.id)
        assert COMPACT_SELF_CONTINUE_VARIABLE not in variables
        mock_schedule.assert_called_once()
        assert scheduled == [(session, "Continue where you last left off.")]

    @pytest.mark.parametrize("cli_source", ["codex", "gemini", "qwen", "droid"])
    def test_pending_flag_schedules_continuation_without_compact_source(
        self, hub_db: HubDatabase, mock_dependencies: dict, cli_source: str
    ) -> None:
        """Providers that omit source='compact' still resume after compact_self."""
        db = self._make_db(hub_db)
        session = self._make_precreated_session(session_id=f"{cli_source}-sess")
        mark_compact_self_continuation_pending(db, session.id)
        mock_dependencies["session_storage"].db = db
        mock_dependencies["session_storage"].get.return_value = session
        mock_dependencies["task_manager"].list_tasks.return_value = []

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id=session.id,
            source=cli_source,
            data={"cwd": "/some/dir"},
            metadata={},
        )

        scheduled: list[tuple[object, str]] = []
        with (
            patch.object(handlers, "_activate_default_agent", return_value=None),
            patch(
                "gobby.hooks.event_handlers._session_start.consume_and_schedule_compact_self_continuation",
                side_effect=self._fake_compact_self_consumer(scheduled),
            ) as mock_schedule,
        ):
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        variables = SessionVariableManager(db).get_variables(session.id)
        assert COMPACT_SELF_CONTINUE_VARIABLE not in variables
        mock_schedule.assert_called_once()
        assert scheduled == [(session, "Continue where you last left off.")]

    def test_manual_compact_without_pending_flag_does_not_schedule_continuation(
        self, hub_db: HubDatabase, mock_dependencies: dict
    ) -> None:
        """A manual compact without the pending flag does not schedule continuation."""
        db = self._make_db(hub_db)
        session = self._make_precreated_session()
        mock_dependencies["session_storage"].db = db
        mock_dependencies["session_storage"].get.return_value = session
        mock_dependencies["task_manager"].list_tasks.return_value = []

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id=session.id,
            data={"source": "compact", "cwd": "/some/dir"},
            metadata={},
        )

        scheduled: list[tuple[object, str]] = []
        with (
            patch.object(handlers, "_activate_default_agent", return_value=None),
            patch(
                "gobby.hooks.event_handlers._session_start.consume_and_schedule_compact_self_continuation",
                side_effect=self._fake_compact_self_consumer(scheduled),
            ) as mock_schedule,
        ):
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_schedule.assert_called_once()
        assert scheduled == []

    def test_stale_compact_pending_flag_clears_without_scheduling_continuation(
        self, hub_db: HubDatabase, mock_dependencies: dict
    ) -> None:
        """A stale self-compact flag is cleared without scheduling a continuation."""
        db = self._make_db(hub_db)
        session = self._make_precreated_session()
        stale_time = datetime.now(UTC) - timedelta(seconds=601)
        mark_compact_self_continuation_pending(db, session.id, now=stale_time)
        mock_dependencies["session_storage"].db = db
        mock_dependencies["session_storage"].get.return_value = session
        mock_dependencies["task_manager"].list_tasks.return_value = []

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id=session.id,
            data={"source": "compact", "cwd": "/some/dir"},
            metadata={},
        )

        scheduled: list[tuple[object, str]] = []
        with (
            patch.object(handlers, "_activate_default_agent", return_value=None),
            patch(
                "gobby.hooks.event_handlers._session_start.consume_and_schedule_compact_self_continuation",
                side_effect=self._fake_compact_self_consumer(scheduled),
            ) as mock_schedule,
        ):
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        variables = SessionVariableManager(db).get_variables(session.id)
        assert COMPACT_SELF_CONTINUE_VARIABLE not in variables
        mock_schedule.assert_called_once()
        assert scheduled == []

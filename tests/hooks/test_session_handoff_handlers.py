"""Session handoff handler tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEventType

from ._event_handler_helpers import make_event

pytestmark = pytest.mark.unit


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
            {
                "session_summary": "# Summary\nWorked on feature X",
                "full_session_summary": "# Summary\nWorked on feature X",
            },
        )

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

        mock_parent_obj = MagicMock()
        mock_parent_obj.id = "parent-sess-123"
        mock_parent_obj.seq_num = 42
        mock_parent_obj.summary_markdown = "# Compact\nContinuation of task Y"

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
            {
                "session_summary": "# Compact\nContinuation of task Y",
                "full_session_summary": "# Compact\nContinuation of task Y",
            },
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
        mock_parent_obj.summary_markdown = "# Compact\nContinuation"

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
        claimed_task = MagicMock(status="in_progress", assignee="parent-sess-123")
        mock_dependencies["task_manager"].get_task.return_value = claimed_task

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
        mock_dependencies["task_manager"].claim_task.assert_called_once_with(
            "uuid-123",
            session_id="new-sess-456",
            force=True,
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
        mock_parent_obj.summary_markdown = "# Compact\nDone"

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

        mock_parent_obj = MagicMock()
        mock_parent_obj.id = "parent-sess-500"
        mock_parent_obj.seq_num = 50
        mock_parent_obj.summary_markdown = "# Summary\nCleared session"

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
        claimed_task = MagicMock(status="needs_review", assignee="parent-sess-500")
        mock_dependencies["task_manager"].get_task.return_value = claimed_task

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
        mock_dependencies["task_manager"].claim_task.assert_called_once_with(
            "uuid-789",
            session_id="new-sess-600",
            force=True,
        )
        mock_dependencies["session_task_manager"].link_task.assert_called_once_with(
            "new-sess-600", "uuid-789", "claimed"
        )

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

        mock_parent_obj = MagicMock()
        mock_parent_obj.id = "parent-sess-123"
        mock_parent_obj.seq_num = 42
        mock_parent_obj.summary_markdown = "# Compact\nContinuation"

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
            data={"source": "compact", "cwd": "/some/dir"},
            metadata={},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_dependencies["task_manager"].claim_task.assert_not_called()
        mock_dependencies["session_task_manager"].link_task.assert_not_called()

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

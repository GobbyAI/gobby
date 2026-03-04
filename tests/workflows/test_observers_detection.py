"""Tests for detection functions in observers module."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.workflows.observers import (
    detect_mcp_call,
    detect_plan_mode_from_context,
    detect_task_claim,
)

pytestmark = pytest.mark.unit

SESSION_ID = "test-session"


@pytest.fixture
def variables():
    """Create empty variables dict."""
    return {}


@pytest.fixture
def mock_task_manager():
    """Mock LocalTaskManager."""
    mock = MagicMock()
    mock_task = MagicMock()
    mock_task.id = "task-uuid-123"
    mock.get_task.return_value = mock_task
    return mock


@pytest.fixture
def make_after_tool_event():
    """Factory for creating AFTER_TOOL events with normalized adapter fields."""

    def _make(tool_name: str, tool_input: dict | None = None, tool_output: dict | None = None):
        data = {
            "tool_name": tool_name,
            "tool_input": tool_input or {},
            "tool_output": tool_output or {},
        }

        # Simulate adapter normalization for MCP calls
        if tool_name in ("call_tool", "mcp__gobby__call_tool") and tool_input:
            data["mcp_server"] = tool_input.get("server_name")
            data["mcp_tool"] = tool_input.get("tool_name")

        return HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            source=SessionSource.CLAUDE,
            session_id="test-session-ext",
            timestamp=datetime.now(UTC),
            data=data,
            metadata={"_platform_session_id": SESSION_ID},
        )

    return _make


# =============================================================================
# Tests for detect_plan_mode_from_context
# =============================================================================


class TestDetectPlanModeFromContext:
    def test_detects_plan_mode_active_indicator(self, variables) -> None:
        prompt = "User prompt here\n<system-reminder>Plan mode is active</system-reminder>"
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("mode_level") == 0

    def test_detects_plan_mode_still_active(self, variables) -> None:
        prompt = "<system-reminder>Plan mode still active</system-reminder>\nWhat should I do?"
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("mode_level") == 0

    def test_detects_you_are_in_plan_mode(self, variables) -> None:
        prompt = "<system-reminder>You are in plan mode</system-reminder>. Please continue."
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("mode_level") == 0

    def test_detects_exited_plan_mode(self, variables) -> None:
        variables["mode_level"] = 0
        prompt = "<system-reminder>Exited Plan Mode</system-reminder>. Now implement."
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("mode_level") != 0

    def test_does_not_change_when_already_in_plan_mode(self, variables) -> None:
        variables["mode_level"] = 0
        prompt = "Plan mode is active"
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("mode_level") == 0

    def test_ignores_prompt_without_indicators(self, variables) -> None:
        prompt = "Please fix the bug in the code."
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert "mode_level" not in variables

    def test_handles_empty_prompt(self, variables) -> None:
        detect_plan_mode_from_context("", variables, SESSION_ID)
        assert "mode_level" not in variables

    def test_handles_none_prompt(self, variables) -> None:
        detect_plan_mode_from_context(None, variables, SESSION_ID)  # type: ignore[arg-type]
        assert "mode_level" not in variables

    def test_ignores_plan_mode_inside_conversation_history(self, variables) -> None:
        prompt = (
            "<system-reminder>\n"
            "<conversation-history>\n"
            "The following is prior conversation history.\n\n"
            "**Assistant:** Let me enter plan mode.\n\n"
            "<system-reminder>\n"
            '<plan-mode status="active">\n'
            "You are in PLAN MODE. Your role is to research and design, not execute.\n"
            "</plan-mode>\n"
            "</system-reminder>\n"
            "</conversation-history>\n"
            "</system-reminder>\n"
            "How about now?"
        )
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert "mode_level" not in variables

    def test_detects_plan_mode_outside_conversation_history(self, variables) -> None:
        prompt = (
            "<system-reminder>\n"
            "<conversation-history>\n"
            "Some old context here.\n"
            "</conversation-history>\n"
            "</system-reminder>\n"
            "<system-reminder>Plan mode is active</system-reminder>\n"
            "Please plan the changes."
        )
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("mode_level") == 0


# =============================================================================
# Tests for detect_task_claim - close_task behavior
# =============================================================================


class TestDetectTaskClaimCloseTaskBehavior:
    def test_successful_close_task_removes_from_claimed_tasks(
        self, variables, make_after_tool_event, mock_task_manager
    ) -> None:
        mock_task = MagicMock()
        mock_task.id = "task-uuid-123"
        mock_task_manager.get_task.return_value = mock_task

        variables["task_claimed"] = True
        variables["claimed_tasks"] = {"task-uuid-123": "#1", "task-uuid-456": "#2"}

        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": {"task_id": "task-123"},
            },
            tool_output={"success": True, "result": {"id": "task-123", "status": "done"}},
        )

        detect_task_claim(event, variables, SESSION_ID, task_manager=mock_task_manager)

        assert variables.get("task_claimed") is True  # Still has task-uuid-456
        assert variables.get("claimed_tasks") == {"task-uuid-456": "#2"}

    def test_successful_close_last_task_clears_task_claimed(
        self, variables, make_after_tool_event, mock_task_manager
    ) -> None:
        mock_task = MagicMock()
        mock_task.id = "task-uuid-123"
        mock_task_manager.get_task.return_value = mock_task

        variables["task_claimed"] = True
        variables["claimed_tasks"] = {"task-uuid-123": "#1"}

        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": {"task_id": "task-123"},
            },
            tool_output={"success": True, "result": {"id": "task-123", "status": "done"}},
        )

        detect_task_claim(event, variables, SESSION_ID, task_manager=mock_task_manager)

        assert variables.get("task_claimed") is False
        assert variables.get("claimed_tasks") == {}

    def test_failed_close_task_with_error(self, variables, make_after_tool_event) -> None:
        variables["task_claimed"] = True
        variables["claimed_tasks"] = {"task-123": "#1"}

        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": {"task_id": "task-123"},
            },
            tool_output={
                "success": True,
                "result": {
                    "error": "uncommitted_changes",
                    "message": "Task has uncommitted changes",
                },
            },
        )

        detect_task_claim(event, variables, SESSION_ID)

        assert variables.get("task_claimed") is True
        assert variables.get("claimed_tasks") == {"task-123": "#1"}

    def test_close_task_with_empty_output(self, variables, make_after_tool_event) -> None:
        variables["task_claimed"] = True
        variables["claimed_tasks"] = {"task-123": "#1"}

        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": {"task_id": "task-123"},
            },
            tool_output={},
        )

        detect_task_claim(event, variables, SESSION_ID)

        assert variables.get("task_claimed") is True

    def test_close_task_with_top_level_error(self, variables, make_after_tool_event) -> None:
        variables["task_claimed"] = True
        variables["claimed_tasks"] = {"task-123": "#1"}

        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": {"task_id": "task-123"},
            },
            tool_output={"status": "error", "error": "Something went wrong"},
        )

        detect_task_claim(event, variables, SESSION_ID)

        assert variables.get("task_claimed") is True


# =============================================================================
# Tests for detect_task_claim - claim operations
# =============================================================================


class TestDetectTaskClaimClaimOperations:
    def test_sets_task_claimed_on_claim_task(
        self, variables, make_after_tool_event, mock_task_manager
    ) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "claim_task",
                "arguments": {"task_id": "task-123"},
            },
            tool_output={"success": True, "result": {"id": "task-123", "status": "in_progress"}},
        )

        detect_task_claim(event, variables, SESSION_ID, task_manager=mock_task_manager)

        assert variables.get("task_claimed") is True
        assert "task-uuid-123" in variables.get("claimed_tasks", {})

    def test_sets_task_claimed_on_create_task_with_claim(
        self, variables, make_after_tool_event, mock_task_manager
    ) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
                "arguments": {"title": "New task", "claim": True},
            },
            tool_output={"success": True, "result": {"id": "new-task-uuid", "status": "open"}},
        )

        detect_task_claim(event, variables, SESSION_ID, task_manager=mock_task_manager)

        assert variables.get("task_claimed") is True
        assert "new-task-uuid" in variables.get("claimed_tasks", {})

    def test_create_task_without_claim_does_not_set_task_claimed(
        self, variables, make_after_tool_event, mock_task_manager
    ) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
                "arguments": {"title": "New task"},
            },
            tool_output={"success": True, "result": {"id": "new-task-uuid", "status": "open"}},
        )

        detect_task_claim(event, variables, SESSION_ID, task_manager=mock_task_manager)

        assert "task_claimed" not in variables

    def test_create_task_with_explicit_claim_false_does_not_set_task_claimed(
        self, variables, make_after_tool_event, mock_task_manager
    ) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
                "arguments": {"title": "New task", "claim": False},
            },
            tool_output={"success": True, "result": {"id": "new-task-uuid", "status": "open"}},
        )

        detect_task_claim(event, variables, SESSION_ID, task_manager=mock_task_manager)

        assert "task_claimed" not in variables

    def test_create_task_handles_missing_id(
        self, variables, make_after_tool_event, mock_task_manager
    ) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
                "arguments": {"title": "New task"},
            },
            tool_output={"success": True, "result": {"status": "error"}},
        )

        detect_task_claim(event, variables, SESSION_ID, task_manager=mock_task_manager)

        assert "task_claimed" not in variables

    def test_sets_task_claimed_on_update_to_in_progress(
        self, variables, make_after_tool_event, mock_task_manager
    ) -> None:
        mock_task = mock_task_manager.get_task.return_value
        mock_task.id = "task-uuid-456"
        mock_task.seq_num = 456

        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "update_task",
                "arguments": {"task_id": "task-123", "status": "in_progress"},
            },
            tool_output={"success": True, "result": {"id": "task-123", "status": "in_progress"}},
        )

        detect_task_claim(event, variables, SESSION_ID, task_manager=mock_task_manager)

        assert variables.get("task_claimed") is True
        assert "task-uuid-456" in variables.get("claimed_tasks", {})

    def test_ignores_update_to_other_status(self, variables, make_after_tool_event) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "update_task",
                "arguments": {"task_id": "task-123", "status": "blocked"},
            },
            tool_output={"success": True, "result": {"id": "task-123", "status": "blocked"}},
        )

        detect_task_claim(event, variables, SESSION_ID)

        assert "task_claimed" not in variables

    def test_does_not_set_task_claimed_on_claim_error(
        self, variables, make_after_tool_event
    ) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "claim_task",
                "arguments": {"task_id": "task-123"},
            },
            tool_output={
                "success": True,
                "result": {"error": "already_claimed", "message": "Task is already claimed"},
            },
        )

        detect_task_claim(event, variables, SESSION_ID)

        assert "task_claimed" not in variables

    def test_ignores_non_gobby_tasks_server(self, variables, make_after_tool_event) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "other-server",
                "tool_name": "claim_task",
                "arguments": {"task_id": "task-123"},
            },
            tool_output={"success": True, "result": {}},
        )

        detect_task_claim(event, variables, SESSION_ID)

        assert "task_claimed" not in variables

    def test_ignores_non_mcp_tools(self, variables, make_after_tool_event) -> None:
        event = make_after_tool_event(
            "Read",
            tool_input={"file_path": "/some/file.py"},
            tool_output={"content": "file content"},
        )

        detect_task_claim(event, variables, SESSION_ID)

        assert "task_claimed" not in variables

    def test_task_resolution_without_manager_warns(self, variables, make_after_tool_event) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "claim_task",
                "arguments": {"task_id": "task-123"},
            },
            tool_output={"success": True, "result": {"id": "task-123", "status": "in_progress"}},
        )

        detect_task_claim(event, variables, SESSION_ID, task_manager=None)

        assert "task_claimed" not in variables
        assert "claimed_tasks" not in variables

    def test_task_resolution_failure_is_handled(
        self, variables, make_after_tool_event, mock_task_manager
    ) -> None:
        mock_task_manager.get_task.side_effect = Exception("DB Error")

        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "claim_task",
                "arguments": {"task_id": "task-123"},
            },
            tool_output={"success": True, "result": {"id": "task-123", "status": "in_progress"}},
        )

        detect_task_claim(event, variables, SESSION_ID, task_manager=mock_task_manager)

        assert "task_claimed" not in variables

    def test_task_not_found(self, variables, make_after_tool_event, mock_task_manager) -> None:
        mock_task_manager.get_task.return_value = None

        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "claim_task",
                "arguments": {"task_id": "task-123"},
            },
            tool_output={"success": True, "result": {"id": "task-123", "status": "in_progress"}},
        )

        detect_task_claim(event, variables, SESSION_ID, task_manager=mock_task_manager)

        assert "task_claimed" not in variables

    def test_auto_link_failure_handled(
        self, variables, make_after_tool_event, mock_task_manager
    ) -> None:
        mock_session_manager = MagicMock()
        mock_session_manager.link_task.side_effect = Exception("Link error")

        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "claim_task",
                "arguments": {"task_id": "task-123"},
            },
            tool_output={"success": True, "result": {"id": "task-123", "status": "in_progress"}},
        )

        detect_task_claim(
            event,
            variables,
            SESSION_ID,
            task_manager=mock_task_manager,
            session_task_manager=mock_session_manager,
        )

        assert variables.get("task_claimed") is True


# =============================================================================
# Tests for detect_mcp_call
# =============================================================================


class TestDetectMcpCall:
    def test_tracks_successful_mcp_call(self, variables, make_after_tool_event) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={"server_name": "demo-server", "tool_name": "demo-tool"},
            tool_output={"result": "success"},
        )

        detect_mcp_call(event, variables, SESSION_ID)

        mcp_calls = variables.get("mcp_calls", {})
        assert "demo-server" in mcp_calls
        assert "demo-tool" in mcp_calls["demo-server"]

        mcp_results = variables.get("mcp_results", {})
        assert "demo-server" in mcp_results
        assert mcp_results["demo-server"]["demo-tool"] == "success"

    def test_tracks_multiple_tools(self, variables, make_after_tool_event) -> None:
        event1 = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={"server_name": "demo-server", "tool_name": "tool-1"},
            tool_output={"result": "1"},
        )
        event2 = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={"server_name": "demo-server", "tool_name": "tool-2"},
            tool_output={"result": "2"},
        )

        detect_mcp_call(event1, variables, SESSION_ID)
        detect_mcp_call(event2, variables, SESSION_ID)

        calls = variables["mcp_calls"]["demo-server"]
        assert "tool-1" in calls
        assert "tool-2" in calls

    def test_ignores_error_responses(self, variables, make_after_tool_event) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={"server_name": "demo-server", "tool_name": "error-tool"},
            tool_output={"error": "failed"},
        )

        detect_mcp_call(event, variables, SESSION_ID)

        assert "mcp_calls" not in variables

    def test_ignores_nested_error_result(self, variables, make_after_tool_event) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={"server_name": "demo-server", "tool_name": "error-tool"},
            tool_output={"result": {"error": "nested failure"}},
        )

        detect_mcp_call(event, variables, SESSION_ID)

        assert "mcp_calls" not in variables

    def test_ignores_missing_server_or_tool(self, variables, make_after_tool_event) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={"server_name": "", "tool_name": "tool"},
            tool_output={"result": "ok"},
        )

        detect_mcp_call(event, variables, SESSION_ID)
        assert "mcp_calls" not in variables

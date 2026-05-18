"""Tests for detection functions in observers module."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.workflows.observers import (
    _extract_shell_output_text,
    _is_git_commit_command,
    _looks_like_commit_success,
    detect_bash_commit,
    detect_commit_link,
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
        normalize_tool_fields(data)

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
        assert variables.get("plan_mode") is True

    def test_detects_plan_mode_still_active(self, variables) -> None:
        prompt = "<system-reminder>Plan mode still active</system-reminder>\nWhat should I do?"
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("mode_level") == 0
        assert variables.get("plan_mode") is True

    def test_detects_you_are_in_plan_mode(self, variables) -> None:
        prompt = "<system-reminder>You are in plan mode</system-reminder>. Please continue."
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("mode_level") == 0
        assert variables.get("plan_mode") is True

    def test_detects_exited_plan_mode(self, variables) -> None:
        variables["mode_level"] = 0
        variables["plan_mode"] = True
        variables["plan_skill_loaded"] = True
        prompt = "<system-reminder>Exited Plan Mode</system-reminder>. Now implement."
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("mode_level") != 0
        assert variables.get("plan_mode") is False
        assert variables.get("plan_skill_loaded") is False

    def test_detects_claude_yolo_mode_system_reminder(self, variables) -> None:
        variables["mode_level"] = 1
        variables["plan_mode"] = True
        variables["plan_skill_loaded"] = True
        prompt = (
            "<system-reminder>"
            "YOLO mode is active. You may execute without approval prompts."
            "</system-reminder>"
        )
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("chat_mode") == "bypass"
        assert variables.get("mode_level") == 2
        assert variables.get("plan_mode") is False
        assert variables.get("plan_skill_loaded") is False

    def test_detects_legacy_claude_auto_mode_system_reminder(self, variables) -> None:
        variables["mode_level"] = 1
        prompt = "<system-reminder>Auto mode is active.</system-reminder>"
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("chat_mode") == "bypass"
        assert variables.get("mode_level") == 2

    def test_detects_claude_act_mode_system_reminder(self, variables) -> None:
        variables["mode_level"] = 2
        prompt = "<system-reminder>You are in Act mode.</system-reminder>"
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("chat_mode") == "normal"
        assert variables.get("mode_level") == 1

    def test_does_not_change_when_already_in_plan_mode(self, variables) -> None:
        variables["mode_level"] = 0
        variables["plan_mode"] = True
        prompt = "<system-reminder>Plan mode is active</system-reminder>"
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("mode_level") == 0
        assert variables.get("plan_mode") is True

    def test_heals_stale_plan_mode_when_no_markers(self, variables) -> None:
        """After clear/compact, mode_level=0 persists but no CLI injects markers."""
        variables["mode_level"] = 0
        variables["chat_mode"] = "bypass"
        prompt = "Please fix the bug in the code."
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("mode_level") == 2  # reset to YOLO

    def test_no_heal_when_chat_mode_is_plan(self, variables) -> None:
        """Don't reset mode_level if chat_mode is genuinely plan (edge case)."""
        variables["mode_level"] = 0
        variables["chat_mode"] = "plan"
        prompt = "Please fix the bug in the code."
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("mode_level") == 0  # chat_mode=plan → stay at 0

    def test_ignores_prompt_without_indicators(self, variables) -> None:
        prompt = "Please fix the bug in the code."
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert "mode_level" not in variables

    def test_handles_empty_prompt(self, variables) -> None:
        detect_plan_mode_from_context("", variables, SESSION_ID)
        assert "mode_level" not in variables

    def test_handles_none_prompt(self, variables) -> None:
        detect_plan_mode_from_context(None, variables, SESSION_ID)
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

    # --- Gemini CLI detection ---

    def test_detects_gemini_active_approval_mode_plan(self, variables) -> None:
        prompt = "# Active Approval Mode: Plan\nPlease analyze the codebase."
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("mode_level") == 0

    def test_detects_gemini_operating_in_plan_mode(self, variables) -> None:
        prompt = "You are operating in **Plan Mode**. Research only."
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("mode_level") == 0

    def test_detects_gemini_exit_via_execute_mode(self, variables) -> None:
        variables["mode_level"] = 0
        variables["chat_mode"] = "bypass"
        prompt = "# Active Approval Mode: Execute\nNow implement."
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("mode_level") == 2

    def test_gemini_markers_inside_conversation_history_ignored(self, variables) -> None:
        prompt = (
            "<conversation-history>\n"
            "# Active Approval Mode: Plan\n"
            "You are operating in **Plan Mode**.\n"
            "</conversation-history>\n"
            "Now do something else."
        )
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert "mode_level" not in variables

    # --- Gobby <plan-mode> tag detection ---

    def test_detects_plan_mode_active_tag(self, variables) -> None:
        prompt = '<plan-mode status="active">\nYou are in PLAN MODE.\n</plan-mode>'
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("mode_level") == 0

    def test_detects_plan_mode_approved_tag(self, variables) -> None:
        variables["mode_level"] = 0
        variables["chat_mode"] = "bypass"
        prompt = '<plan-mode status="approved">\nPlan approved.\n</plan-mode>'
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("mode_level") == 2

    def test_detects_yolo_chat_mode_tag(self, variables) -> None:
        variables["mode_level"] = 1
        prompt = '<chat-mode status="yolo">\nYou are in YOLO MODE.\n</chat-mode>'
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("chat_mode") == "bypass"
        assert variables.get("mode_level") == 2

    def test_detects_legacy_auto_chat_mode_tag(self, variables) -> None:
        variables["mode_level"] = 1
        prompt = '<chat-mode status="auto">\nYou are in AUTO MODE.\n</chat-mode>'
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("chat_mode") == "bypass"
        assert variables.get("mode_level") == 2

    def test_plan_mode_active_tag_inside_conversation_history_ignored(self, variables) -> None:
        prompt = (
            "<conversation-history>\n"
            '<plan-mode status="active">\nOld plan mode.\n</plan-mode>\n'
            "</conversation-history>\n"
            "Continue working."
        )
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert "mode_level" not in variables


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

    def test_tracks_json_safe_result(self, variables, make_after_tool_event) -> None:
        @dataclass
        class DemoResult:
            id: str
            count: int

        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={"server_name": "demo-server", "tool_name": "demo-tool"},
            tool_output={"result": {"items": [DemoResult(id="a", count=1)]}},
        )

        detect_mcp_call(event, variables, SESSION_ID)

        assert variables["mcp_results"]["demo-server"]["demo-tool"] == {
            "items": [{"id": "a", "count": 1}]
        }
        json.dumps(variables)

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

    def test_heals_null_mcp_calls(self, variables, make_after_tool_event) -> None:
        variables["mcp_calls"] = None
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={"server_name": "demo-server", "tool_name": "demo-tool"},
            tool_output={"result": "success"},
        )

        detect_mcp_call(event, variables, SESSION_ID)

        assert variables["mcp_calls"] == {"demo-server": ["demo-tool"]}

    def test_heals_null_mcp_results(self, variables, make_after_tool_event) -> None:
        variables["mcp_results"] = None
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={"server_name": "demo-server", "tool_name": "demo-tool"},
            tool_output={"result": "success"},
        )

        detect_mcp_call(event, variables, SESSION_ID)

        assert variables["mcp_results"] == {"demo-server": {"demo-tool": "success"}}

    def test_heals_null_mcp_server_buckets(self, variables, make_after_tool_event) -> None:
        variables["mcp_calls"] = {"demo-server": None}
        variables["mcp_results"] = {"demo-server": None}
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={"server_name": "demo-server", "tool_name": "demo-tool"},
            tool_output={"result": "success"},
        )

        detect_mcp_call(event, variables, SESSION_ID)

        assert variables["mcp_calls"] == {"demo-server": ["demo-tool"]}
        assert variables["mcp_results"] == {"demo-server": {"demo-tool": "success"}}

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

    def test_tracks_loaded_skill_from_successful_get_skill(
        self, variables, make_after_tool_event
    ) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={"server_name": "gobby-skills", "tool_name": "get_skill"},
            tool_output={"result": {"success": True, "skill": {"name": "plan"}}},
        )

        detect_mcp_call(event, variables, SESSION_ID)

        assert variables["loaded_skills"] == ["plan"]

    def test_tracks_get_skill_result_and_loaded_skill_together(
        self, variables, make_after_tool_event
    ) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={"server_name": "gobby-skills", "tool_name": "get_skill"},
            tool_output={"result": {"success": True, "skill": {"name": "brevity"}}},
        )

        detect_mcp_call(event, variables, SESSION_ID)

        assert variables["mcp_results"]["gobby-skills"]["get_skill"] == {
            "success": True,
            "skill": {"name": "brevity"},
        }
        assert variables["loaded_skills"] == ["brevity"]

    def test_tracks_loaded_skill_idempotently(self, variables, make_after_tool_event) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={"server_name": "gobby-skills", "tool_name": "get_skill"},
            tool_output={"success": True, "skill": {"name": "plan"}},
        )

        detect_mcp_call(event, variables, SESSION_ID)
        detect_mcp_call(event, variables, SESSION_ID)

        assert variables["loaded_skills"] == ["plan"]

    def test_failed_get_skill_does_not_track_loaded_skill(
        self, variables, make_after_tool_event
    ) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={"server_name": "gobby-skills", "tool_name": "get_skill"},
            tool_output={"result": {"success": False, "error": "missing"}},
        )

        detect_mcp_call(event, variables, SESSION_ID)

        assert "loaded_skills" not in variables

    def test_ignores_missing_server_or_tool(self, variables, make_after_tool_event) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={"server_name": "", "tool_name": "tool"},
            tool_output={"result": "ok"},
        )

        detect_mcp_call(event, variables, SESSION_ID)
        assert "mcp_calls" not in variables


# =============================================================================
# Tests for detect_commit_link
# =============================================================================


class TestDetectCommitLink:
    def test_link_commit_sets_task_has_commits(self, variables, make_after_tool_event) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "link_commit",
                "arguments": {"task_id": "#123", "commit_sha": "abc123"},
            },
            tool_output={"success": True, "result": {}},
        )

        detect_commit_link(event, variables, SESSION_ID)

        assert variables["task_has_commits"] is True

    def test_close_task_with_commit_sha_sets_task_has_commits(
        self, variables, make_after_tool_event
    ) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": {"task_id": "#123", "commit_sha": "abc123"},
            },
            tool_output={"success": True, "result": {}},
        )

        detect_commit_link(event, variables, SESSION_ID)

        assert variables["task_has_commits"] is True

    def test_close_task_without_commit_sha_does_not_set(
        self, variables, make_after_tool_event
    ) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": {"task_id": "#123"},
            },
            tool_output={"success": True, "result": {}},
        )

        detect_commit_link(event, variables, SESSION_ID)

        assert "task_has_commits" not in variables

    def test_auto_link_commits_sets_task_has_commits(
        self, variables, make_after_tool_event
    ) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "auto_link_commits",
                "arguments": {},
            },
            tool_output={"success": True, "result": {"linked": 3}},
        )

        detect_commit_link(event, variables, SESSION_ID)

        assert variables["task_has_commits"] is True

    def test_ignores_non_commit_tools(self, variables, make_after_tool_event) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
                "arguments": {"title": "test"},
            },
            tool_output={"success": True, "result": {"id": "task-123"}},
        )

        detect_commit_link(event, variables, SESSION_ID)

        assert "task_has_commits" not in variables

    def test_ignores_error_response(self, variables, make_after_tool_event) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "link_commit",
                "arguments": {"task_id": "#123", "commit_sha": "abc123"},
            },
            tool_output={"error": "Task not found"},
        )

        detect_commit_link(event, variables, SESSION_ID)

        assert "task_has_commits" not in variables

    def test_skips_when_already_set(self, variables, make_after_tool_event) -> None:
        variables["task_has_commits"] = True

        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "link_commit",
                "arguments": {"task_id": "#456", "commit_sha": "def456"},
            },
            tool_output={"success": True, "result": {}},
        )

        detect_commit_link(event, variables, SESSION_ID)

        assert variables["task_has_commits"] is True


# =============================================================================
# Tests for detect_bash_commit
# =============================================================================


def _make_bash_event(
    tool_output: str,
    *,
    tool_name: str = "Bash",
    command: str = "git commit -m 'msg'",
    is_error: bool = False,
) -> HookEvent:
    """Helper to create a Bash AFTER_TOOL event with string output."""
    data: dict[str, object] = {
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "tool_output": tool_output,
    }
    if is_error:
        data["is_error"] = True
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        source=SessionSource.CLAUDE,
        session_id="test-session-ext",
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"_platform_session_id": SESSION_ID},
    )


class TestDetectBashCommit:
    """Verify detect_bash_commit sets task_has_commits from Bash git output."""

    def test_git_commit_output_sets_task_has_commits(self, variables) -> None:
        event = _make_bash_event("[main abc1234] Fix bug\n 1 file changed, 2 insertions(+)")

        detect_bash_commit(event, variables, SESSION_ID)

        assert variables["task_has_commits"] is True

    def test_git_commit_branch_with_slash(self, variables) -> None:
        event = _make_bash_event("[feat/login 9a3b2c1e] Add auth\n 3 files changed")

        detect_bash_commit(event, variables, SESSION_ID)

        assert variables["task_has_commits"] is True

    def test_skips_when_already_set(self, variables) -> None:
        variables["task_has_commits"] = True
        event = _make_bash_event("[main def5678] Another\n 1 file changed")

        detect_bash_commit(event, variables, SESSION_ID)

        assert variables["task_has_commits"] is True

    def test_skips_on_error(self, variables) -> None:
        event = _make_bash_event(
            "error: pathspec 'foo' did not match\nExit code: 1",
            is_error=True,
        )

        detect_bash_commit(event, variables, SESSION_ID)

        assert "task_has_commits" not in variables

    def test_ignores_non_bash_tools(self, variables) -> None:
        event = _make_bash_event(
            "[main abc1234] looks like commit but isn't",
            tool_name="Read",
            command="",
        )

        detect_bash_commit(event, variables, SESSION_ID)

        assert "task_has_commits" not in variables

    def test_ignores_output_without_commit_pattern(self, variables) -> None:
        event = _make_bash_event(
            "total 42\ndrwxr-xr-x  5 user staff  160 Mar 22 10:00 .",
            command="ls -la",
        )

        detect_bash_commit(event, variables, SESSION_ID)

        assert "task_has_commits" not in variables

    def test_multiline_output_with_commit(self, variables) -> None:
        output = (
            "On branch main\n"
            "Changes to be committed:\n"
            "  modified: foo.py\n"
            "[main 1a2b3c4d] gobby-#42 Fix the thing\n"
            " 1 file changed, 5 insertions(+), 2 deletions(-)\n"
        )
        event = _make_bash_event(output, command="git add . && git commit -m 'Fix'")

        detect_bash_commit(event, variables, SESSION_ID)

        assert variables["task_has_commits"] is True

    def test_branch_with_hash_in_name(self, variables) -> None:
        event = _make_bash_event("[gobby-#42 abc1234def] Fix\n 1 file changed")

        detect_bash_commit(event, variables, SESSION_ID)

        assert variables["task_has_commits"] is True

    # ── Dict output tests (post-normalization JSON parsing) ──────────────

    def test_dict_output_with_output_key(self, variables) -> None:
        """tool_output is a dict after normalization parses JSON string."""
        event = _make_bash_event_dict(
            {"output": "[main abc1234] Fix bug\n 1 file changed", "exitCode": 0}
        )
        detect_bash_commit(event, variables, SESSION_ID)
        assert variables["task_has_commits"] is True

    def test_dict_output_with_stdout_key(self, variables) -> None:
        """Some adapters use 'stdout' key."""
        event = _make_bash_event_dict({"stdout": "[feat/x 1a2b3c4] Add feature\n 2 files changed"})
        detect_bash_commit(event, variables, SESSION_ID)
        assert variables["task_has_commits"] is True

    def test_dict_output_without_commit_pattern(self, variables) -> None:
        """Dict output that doesn't contain a commit pattern."""
        event = _make_bash_event_dict(
            {"output": "total 42\ndrwxr-xr-x  5 user staff  160 Mar 22 10:00 ."},
            command="ls -la",
        )
        detect_bash_commit(event, variables, SESSION_ID)
        assert "task_has_commits" not in variables

    def test_dict_output_with_error(self, variables) -> None:
        """Dict output with is_error set should be skipped."""
        event = _make_bash_event_dict(
            {"output": "error: pathspec 'foo' did not match", "exitCode": 1},
            is_error=True,
        )
        detect_bash_commit(event, variables, SESSION_ID)
        assert "task_has_commits" not in variables

    # ── Integration tests through normalization ──────────────────────────

    def test_full_normalization_flow_json_string(self, variables) -> None:
        """JSON string tool_response goes through normalization then observer."""
        from gobby.hooks.normalization import normalize_tool_fields

        raw_data = {
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m 'Fix'"},
            "tool_response": '{"output": "[main abc1234] Fix bug\\n 1 file changed", "exitCode": 0}',
        }
        normalized = normalize_tool_fields(dict(raw_data))

        event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            source=SessionSource.CLAUDE,
            session_id="test-session-ext",
            timestamp=datetime.now(UTC),
            data=normalized,
            metadata={"_platform_session_id": SESSION_ID},
        )
        detect_bash_commit(event, variables, SESSION_ID)
        assert variables["task_has_commits"] is True

    def test_full_normalization_flow_plain_string(self, variables) -> None:
        """Plain string tool_result goes through normalization then observer."""
        from gobby.hooks.normalization import normalize_tool_fields

        raw_data = {
            "tool_name": "Bash",
            "tool_input": {"command": "git add . && git commit -m 'Fix'"},
            "tool_result": "[main abc1234] Fix bug\n 1 file changed, 2 insertions(+)",
        }
        normalized = normalize_tool_fields(dict(raw_data))

        event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            source=SessionSource.CLAUDE,
            session_id="test-session-ext",
            timestamp=datetime.now(UTC),
            data=normalized,
            metadata={"_platform_session_id": SESSION_ID},
        )
        detect_bash_commit(event, variables, SESSION_ID)
        assert variables["task_has_commits"] is True

    # ── Command fallback tests ───────────────────────────────────────────

    def test_command_fallback_when_output_lacks_pattern(self, variables) -> None:
        """Fallback detects git commit from command when output is truncated."""
        event = _make_bash_event_dict(
            {"output": "1 file changed, 2 insertions(+)"},
            command="git commit -m 'Fix bug'",
        )
        detect_bash_commit(event, variables, SESSION_ID)
        assert variables["task_has_commits"] is True

    def test_command_fallback_nothing_to_commit(self, variables) -> None:
        """Fallback does NOT fire when output says nothing to commit."""
        event = _make_bash_event_dict(
            {"output": "On branch main\nnothing to commit, working tree clean"},
            command="git commit -m 'Fix bug'",
        )
        detect_bash_commit(event, variables, SESSION_ID)
        assert "task_has_commits" not in variables

    def test_non_commit_command_no_false_positive(self, variables) -> None:
        """Non-commit command doesn't trigger fallback."""
        event = _make_bash_event_dict(
            {"output": "Some output without commit pattern"},
            command="git status",
        )
        detect_bash_commit(event, variables, SESSION_ID)
        assert "task_has_commits" not in variables


def _make_bash_event_dict(
    tool_output: dict[str, object],
    *,
    tool_name: str = "Bash",
    command: str = "git commit -m 'msg'",
    is_error: bool = False,
) -> HookEvent:
    """Helper to create a Bash AFTER_TOOL event with dict output (post-normalization)."""
    data: dict[str, object] = {
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "tool_output": tool_output,
    }
    if is_error:
        data["is_error"] = True
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        source=SessionSource.CLAUDE,
        session_id="test-session-ext",
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"_platform_session_id": SESSION_ID},
    )


# =============================================================================
# Tests for helper functions
# =============================================================================


class TestExtractShellOutputText:
    """Verify _extract_shell_output_text handles all tool_output shapes."""

    def test_string_passthrough(self) -> None:
        assert _extract_shell_output_text("hello") == "hello"

    def test_dict_output_key(self) -> None:
        assert _extract_shell_output_text({"output": "hello"}) == "hello"

    def test_dict_stdout_key(self) -> None:
        assert _extract_shell_output_text({"stdout": "hello"}) == "hello"

    def test_dict_content_key(self) -> None:
        assert _extract_shell_output_text({"content": "hello"}) == "hello"

    def test_dict_priority_order(self) -> None:
        assert _extract_shell_output_text({"output": "a", "stdout": "b"}) == "a"

    def test_empty_dict(self) -> None:
        assert _extract_shell_output_text({}) == ""

    def test_none(self) -> None:
        assert _extract_shell_output_text(None) == ""

    def test_list(self) -> None:
        assert _extract_shell_output_text(["hello"]) == ""


class TestIsGitCommitCommand:
    """Verify _is_git_commit_command matches git commit invocations."""

    def test_simple_commit(self) -> None:
        assert _is_git_commit_command("git commit -m 'msg'") is True

    def test_commit_with_flags(self) -> None:
        assert _is_git_commit_command("git commit --amend --no-edit") is True

    def test_chained_commands(self) -> None:
        assert _is_git_commit_command("git add . && git commit -m 'msg'") is True

    def test_not_commit(self) -> None:
        assert _is_git_commit_command("git status") is False

    def test_empty(self) -> None:
        assert _is_git_commit_command("") is False


class TestLooksLikeCommitSuccess:
    """Verify _looks_like_commit_success filters failed/no-op commits."""

    def test_normal_output(self) -> None:
        assert _looks_like_commit_success("1 file changed") is True

    def test_nothing_to_commit(self) -> None:
        assert _looks_like_commit_success("nothing to commit, working tree clean") is False

    def test_nothing_added(self) -> None:
        assert _looks_like_commit_success("nothing added to commit") is False

    def test_empty(self) -> None:
        assert _looks_like_commit_success("") is False

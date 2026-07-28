# mypy: disable-error-code="no-untyped-def,type-arg,attr-defined"
"""Tests for detection functions in observers module."""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.workflows import observers as observers_module
from gobby.workflows.observers import (
    _extract_shell_output_text,
    _is_git_commit_command,
    _looks_like_commit_success,
    _shell_tool_succeeded,
    detect_bash_commit,
    detect_commit_link,
    detect_mcp_call,
    detect_plan_mode_from_context,
    detect_task_claim,
    reconcile_claimed_tasks,
)

pytestmark = pytest.mark.unit

# Session id columns are native uuid in PostgreSQL; a synthetic id would fail
# with `invalid input syntax for type uuid` where tests hit the real DB.
SESSION_ID = "11111111-1111-4111-8111-111111111111"
AGENT_SESSION_ID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def variables() -> dict[str, Any]:
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


def _claimed_task(
    *,
    task_id: str = "task-uuid-123",
    title: str = "Task",
    description: str | None = None,
    labels: list[str] | None = None,
    category: str | None = "code",
    validation_criteria: str | None = None,
    additional_skills: list[str] | None = None,
    claimed_by_session_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        seq_num=123,
        title=title,
        description=description,
        labels=labels or [],
        category=category,
        validation_criteria=validation_criteria,
        additional_skills=additional_skills,
        claimed_by_session_id=claimed_by_session_id,
        closed_at=None,
        is_escalated=False,
        stages=(),
    )


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
            session_id=AGENT_SESSION_ID,
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

    def test_system_reminder_exit_clears_plan_skill_loaded_without_plan_mode(
        self, variables: dict[str, Any]
    ) -> None:
        variables["mode_level"] = 0
        variables["plan_mode"] = False
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
        variables["plan_mode"] = True
        variables["plan_skill_loaded"] = True
        prompt = "Please fix the bug in the code."
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("mode_level") == 2  # reset to YOLO
        assert variables.get("plan_mode") is False
        assert variables.get("plan_skill_loaded") is False

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

    # --- ACP-style plan marker detection ---

    def test_detects_acp_active_approval_mode_plan(self, variables: dict[str, Any]) -> None:
        prompt = "# Active Approval Mode: Plan\nPlease analyze the codebase."
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("mode_level") == 0
        assert variables.get("plan_mode") is True

    def test_detects_acp_operating_in_plan_mode(self, variables: dict[str, Any]) -> None:
        prompt = "You are operating in **Plan Mode**. Research only."
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("mode_level") == 0
        assert variables.get("plan_mode") is True

    def test_detects_acp_exit_via_execute_mode(self, variables: dict[str, Any]) -> None:
        variables["mode_level"] = 0
        variables["chat_mode"] = "bypass"
        variables["plan_mode"] = True
        variables["plan_skill_loaded"] = True
        prompt = "# Active Approval Mode: Execute\nNow implement."
        detect_plan_mode_from_context(prompt, variables, SESSION_ID)
        assert variables.get("mode_level") == 2
        assert variables.get("plan_mode") is False
        assert variables.get("plan_skill_loaded") is False

    def test_acp_markers_inside_conversation_history_ignored(
        self, variables: dict[str, Any]
    ) -> None:
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
    def test_successful_conditional_close_removes_from_claimed_tasks(
        self,
        variables,
        make_after_tool_event,
        mock_task_manager,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="gobby.workflows.observers")
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
                "arguments": {"task_id": "task-123", "preview": True},
            },
            tool_output={
                "success": True,
                "result": {
                    "id": "task-123",
                    "preview": True,
                    "can_close": True,
                    "closed": True,
                },
            },
        )

        detect_task_claim(event, variables, SESSION_ID, task_manager=mock_task_manager)

        assert variables.get("task_claimed") is True  # Still has task-uuid-456
        assert variables.get("claimed_tasks") == {"task-uuid-456": "#2"}
        removal_record = next(
            record
            for record in caplog.records
            if "removed task-uuid-123 from claimed_tasks" in record.getMessage()
        )
        assert removal_record.levelno == logging.DEBUG

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
            tool_output={
                "success": True,
                "result": {"id": "task-123", "status": "done", "closed": True},
            },
        )

        detect_task_claim(event, variables, SESSION_ID, task_manager=mock_task_manager)

        assert variables.get("task_claimed") is False
        assert variables.get("claimed_tasks") == {}

    def test_blocked_close_preview_preserves_claimed_task(
        self, variables, make_after_tool_event, mock_task_manager
    ) -> None:
        variables["task_claimed"] = True
        variables["claimed_tasks"] = {"task-uuid-123": "#1"}
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": {"task_id": "#1", "preview": True},
            },
            tool_output={
                "success": True,
                "preview": True,
                "can_close": False,
                "closed": False,
            },
        )

        detect_task_claim(event, variables, SESSION_ID, task_manager=mock_task_manager)

        assert variables.get("task_claimed") is True
        assert variables.get("claimed_tasks") == {"task-uuid-123": "#1"}
        mock_task_manager.get_task.assert_not_called()

        mock_task_manager.get_task.return_value = _claimed_task(claimed_by_session_id=SESSION_ID)
        reconcile_claimed_tasks(variables, SESSION_ID, task_manager=mock_task_manager)

        assert variables.get("task_claimed") is True
        assert variables.get("claimed_tasks") == {"task-uuid-123": "#1"}

    def test_close_task_prefers_claimed_ref_before_project_resolution(
        self,
        variables,
        make_after_tool_event,
        mock_task_manager,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_task_manager.get_task.side_effect = ValueError(
            "Task #15126 not found in project [other-project]"
        )
        variables["task_claimed"] = True
        variables["claimed_tasks"] = {"task-uuid-15126": "#15126"}

        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": {"task_id": "#15126"},
            },
            tool_output={"success": True, "result": {"closed": True}},
        )

        with caplog.at_level(logging.WARNING, logger="gobby.workflows.observers"):
            detect_task_claim(
                event,
                variables,
                SESSION_ID,
                task_manager=mock_task_manager,
                project_id="other-project",
            )

        assert variables.get("task_claimed") is False
        assert variables.get("claimed_tasks") == {}
        mock_task_manager.get_task.assert_not_called()
        assert "Cannot resolve closed task ref" not in caplog.text

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

    def test_unresolved_close_refs_emit_thresholded_debug(
        self,
        variables,
        make_after_tool_event,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(observers_module, "_unresolved_close_ref_count", 0)
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": {"task_id": "#404"},
            },
            tool_output={"success": True, "result": {"closed": True}},
        )

        with caplog.at_level(logging.DEBUG, logger="gobby.workflows.observers"):
            for _ in range(10):
                detect_task_claim(event, variables, SESSION_ID)

        assert "Unresolved close_task refs reached 10" in caplog.text
        record = next(
            record
            for record in caplog.records
            if record.message == "Unresolved close_task refs reached 10"
        )
        assert record.latest_ref == "#404"
        assert record.session_id == SESSION_ID


# =============================================================================
# Tests for detect_task_claim - claim operations
# =============================================================================


class TestDetectTaskClaimClaimOperations:
    def test_sets_task_claimed_on_claim_task(
        self, variables, make_after_tool_event, mock_task_manager, caplog
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="gobby.workflows.observers")
        session_task_manager = MagicMock()
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
            session_task_manager=session_task_manager,
            task_manager=mock_task_manager,
        )

        assert variables.get("task_claimed") is True
        assert "task-uuid-123" in variables.get("claimed_tasks", {})
        session_task_manager.link_task.assert_called_once_with(
            SESSION_ID, "task-uuid-123", "worked_on"
        )
        bookkeeping_records = [
            record
            for record in caplog.records
            if "added task-uuid-123 to claimed_tasks" in record.message
            or "Auto-linked task task-uuid-123" in record.message
        ]
        assert len(bookkeeping_records) == 2
        assert all(record.levelno == logging.DEBUG for record in bookkeeping_records)
        assert not any(record.levelno == logging.INFO for record in bookkeeping_records)

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

    def test_create_task_claim_caches_python_skill_metadata(
        self, variables, make_after_tool_event, mock_task_manager
    ) -> None:
        mock_task_manager.get_task.return_value = _claimed_task(
            task_id="new-task-uuid",
            title="Update src/gobby/tasks/metadata.py",
            validation_criteria="src/gobby/tasks/metadata.py handles task metadata",
        )

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

        assert variables["claimed_task_language_skills"] == ["python"]
        assert variables["claimed_task_required_skills"] == [
            "tasks",
            "python",
            "development-discipline",
        ]
        assert "src/gobby/tasks/metadata.py" in variables["claimed_task_files"]

    def test_claim_task_caches_rust_skill_from_affected_files(
        self, variables, make_after_tool_event, mock_task_manager
    ) -> None:
        task = _claimed_task(task_id="task-uuid-123", title="Update parser")
        mock_task_manager.get_task.return_value = task

        with patch(
            "gobby.workflows.claimed_task_skills.TaskAffectedFileManager"
        ) as MockAffectedFiles:
            mock_af_manager = MagicMock()
            mock_af_manager.get_files.return_value = [
                SimpleNamespace(file_path="crates/gobby/src/lib.rs")
            ]
            MockAffectedFiles.return_value = mock_af_manager

            event = make_after_tool_event(
                "mcp__gobby__call_tool",
                tool_input={
                    "server_name": "gobby-tasks",
                    "tool_name": "claim_task",
                    "arguments": {"task_id": "task-123"},
                },
                tool_output={
                    "success": True,
                    "result": {"id": "task-uuid-123", "status": "in_progress"},
                },
            )

            detect_task_claim(event, variables, SESSION_ID, task_manager=mock_task_manager)

        assert variables["claimed_task_language_skills"] == ["rust"]
        assert variables["claimed_task_required_skills"] == [
            "tasks",
            "rust",
            "development-discipline",
        ]
        assert variables["claimed_task_files"] == ["crates/gobby/src/lib.rs"]

    @pytest.mark.parametrize(
        ("labels", "additional_skills", "validation_criteria"),
        [
            (["tdd:required"], [], "Implement src/app.py"),
            ([], ["test-driven-development"], "Implement src/app.py"),
            (
                [],
                [],
                "TDD evidence required: red, green, refactor/final-green, exact test command.",
            ),
        ],
    )
    def test_tdd_markers_cache_test_driven_development_skill(
        self,
        variables,
        make_after_tool_event,
        mock_task_manager,
        labels,
        additional_skills,
        validation_criteria,
    ) -> None:
        mock_task_manager.get_task.return_value = _claimed_task(
            labels=labels,
            additional_skills=additional_skills,
            validation_criteria=validation_criteria,
        )

        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "claim_task",
                "arguments": {"task_id": "task-123"},
            },
            tool_output={"success": True, "result": {"id": "task-uuid-123"}},
        )

        detect_task_claim(event, variables, SESSION_ID, task_manager=mock_task_manager)

        assert "test-driven-development" in variables["claimed_task_required_skills"]

    def test_reconcile_claimed_tasks_refreshes_skill_metadata(
        self, variables, mock_task_manager
    ) -> None:
        task = _claimed_task(
            task_id="task-uuid-123",
            title="Update src/gobby/workflows/hooks.py",
            validation_criteria="src/gobby/workflows/hooks.py caches metadata",
            claimed_by_session_id=SESSION_ID,
        )
        mock_task_manager.get_task.return_value = task
        variables["claimed_tasks"] = {"task-uuid-123": "#123"}

        reconcile_claimed_tasks(variables, SESSION_ID, task_manager=mock_task_manager)

        assert variables["task_claimed"] is True
        assert variables["claimed_task_required_skills"] == [
            "tasks",
            "python",
            "development-discipline",
        ]

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
            tool_output={"success": True, "result": {"closed": True}},
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

    def test_task_not_found(
        self,
        variables,
        make_after_tool_event,
        mock_task_manager,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
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

        with caplog.at_level(logging.WARNING, logger="gobby.workflows.observers"):
            detect_task_claim(event, variables, SESSION_ID, task_manager=mock_task_manager)

        assert "task_claimed" not in variables
        resolution_record = next(
            record
            for record in caplog.records
            if record.getMessage().startswith("Cannot resolve task ref")
        )
        assert resolution_record.levelno == logging.WARNING

    def test_auto_link_failure_handled(
        self,
        variables,
        make_after_tool_event,
        mock_task_manager,
        caplog: pytest.LogCaptureFixture,
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

        with caplog.at_level(logging.WARNING, logger="gobby.workflows.observers"):
            detect_task_claim(
                event,
                variables,
                SESSION_ID,
                task_manager=mock_task_manager,
                session_task_manager=mock_session_manager,
            )

        assert variables.get("task_claimed") is True
        failure_record = next(
            record
            for record in caplog.records
            if record.getMessage().startswith("Failed to auto-link task")
        )
        assert failure_record.levelno == logging.WARNING


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
        assert mcp_results["demo-server"]["demo-tool"] == {}

    def test_tracks_only_bounded_scalar_result_fields(
        self, variables, make_after_tool_event
    ) -> None:
        @dataclass
        class DemoResult:
            id: str
            count: int

        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={"server_name": "demo-server", "tool_name": "demo-tool"},
            tool_output={
                "result": {
                    "timed_out": True,
                    "status": "ready",
                    "items": [DemoResult(id="a", count=1)],
                    "detail": "x" * 257,
                    **{f"field-{index}": index for index in range(20)},
                }
            },
        )

        detect_mcp_call(event, variables, SESSION_ID)

        stored = variables["mcp_results"]["demo-server"]["demo-tool"]
        assert stored["timed_out"] is True
        assert stored["status"] == "ready"
        assert "items" not in stored
        assert "detail" not in stored
        assert len(stored) == 16
        json.dumps(variables)

    def test_keeps_only_latest_64_mcp_results(self, variables, make_after_tool_event) -> None:
        for index in range(65):
            event = make_after_tool_event(
                "mcp__gobby__call_tool",
                tool_input={"server_name": "demo-server", "tool_name": f"tool-{index}"},
                tool_output={"result": {"status": "ready"}},
            )
            detect_mcp_call(event, variables, SESSION_ID)

        stored = variables["mcp_results"]["demo-server"]
        assert len(stored) == 64
        assert "tool-0" not in stored
        assert stored["tool-64"] == {"status": "ready"}

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

        assert variables["mcp_results"] == {"demo-server": {"demo-tool": {}}}

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
        assert variables["mcp_results"] == {"demo-server": {"demo-tool": {}}}

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

    def test_missing_required_skill_is_recorded_as_unresolvable(
        self, variables, make_after_tool_event, caplog
    ) -> None:
        variables["claimed_task_required_skills"] = ["typo-skill"]
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-skills",
                "tool_name": "get_skill",
                "arguments": {"name": "typo-skill"},
            },
            tool_output={"result": {"success": False, "error": "Skill not found: typo-skill"}},
        )

        with caplog.at_level(logging.WARNING, logger="gobby.workflows.observers"):
            detect_mcp_call(event, variables, SESSION_ID)

        assert variables["unresolvable_required_skills"] == ["typo-skill"]
        assert "dropping unresolvable required skill typo-skill" in caplog.text

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
            tool_output={"success": True, "result": {"closed": True}},
        )

        detect_commit_link(event, variables, SESSION_ID)

        assert variables["task_has_commits"] is True

    def test_blocked_close_task_preview_does_not_set_task_has_commits(
        self, variables, make_after_tool_event
    ) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": {
                    "task_id": "#123",
                    "commit_sha": "abc123",
                    "preview": True,
                },
            },
            tool_output={
                "success": True,
                "preview": True,
                "can_close": False,
                "closed": False,
            },
        )

        detect_commit_link(event, variables, SESSION_ID)

        assert "task_has_commits" not in variables

    def test_successful_close_task_preview_sets_task_has_commits(
        self, variables, make_after_tool_event
    ) -> None:
        event = make_after_tool_event(
            "mcp__gobby__call_tool",
            tool_input={
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": {
                    "task_id": "#123",
                    "commit_sha": "abc123",
                    "preview": True,
                },
            },
            tool_output={
                "success": True,
                "preview": True,
                "can_close": True,
                "closed": True,
            },
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
    is_error: bool | None = False,
    cwd: str | None = None,
) -> HookEvent:
    """Helper to create a Bash AFTER_TOOL event with string output."""
    data: dict[str, object] = {
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "tool_output": tool_output,
    }
    if is_error is not None:
        data["is_error"] = is_error
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        source=SessionSource.CLAUDE,
        session_id=AGENT_SESSION_ID,
        timestamp=datetime.now(UTC),
        data=data,
        cwd=cwd,
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

    def test_git_commit_detached_head_output(self, variables) -> None:
        event = _make_bash_event("[detached HEAD 9a3b2c1e] Fix bug\n 1 file changed")

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
        event = _make_bash_event_dict(
            {
                "stdout": "[feat/x 1a2b3c4] Add feature\n 2 files changed",
                "success": True,
            }
        )
        detect_bash_commit(event, variables, SESSION_ID)
        assert variables["task_has_commits"] is True

    @pytest.mark.parametrize(
        "failure_signal",
        [
            {"exitCode": 1},
            {"exit_code": 1},
            {"returncode": 1},
            {"success": False},
            {"status": "failed"},
        ],
    )
    def test_structured_failure_with_commit_output_is_ignored(
        self, variables, failure_signal: dict[str, object]
    ) -> None:
        event = _make_bash_event_dict(
            {
                "output": "[main abc1234] Fix bug\n 1 file changed",
                **failure_signal,
            }
        )

        detect_bash_commit(event, variables, SESSION_ID)

        assert "task_has_commits" not in variables

    def test_unrelated_command_with_commit_output_is_ignored(self, variables) -> None:
        event = _make_bash_event(
            "[main abc1234] Fix bug\n 1 file changed",
            command="cat commit-output.txt",
        )

        detect_bash_commit(event, variables, SESSION_ID)

        assert "task_has_commits" not in variables

    def test_metadata_failure_with_commit_output_is_ignored(self, variables) -> None:
        event = _make_bash_event("[main abc1234] Fix bug\n 1 file changed", is_error=None)
        event.metadata["is_failure"] = True

        detect_bash_commit(event, variables, SESSION_ID)

        assert "task_has_commits" not in variables

    @pytest.mark.parametrize(
        "command",
        [
            "git add file.py && git commit -m 'Fix'",
            "git -C /repo commit -m 'Fix'",
            "/usr/bin/git commit -m 'Fix'",
        ],
    )
    def test_supported_commit_commands_with_successful_dict_output(
        self, variables, command: str
    ) -> None:
        event = _make_bash_event_dict(
            {"output": "[main abc1234] Fix bug\n 1 file changed", "exitCode": 0},
            command=command,
        )

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
            session_id=AGENT_SESSION_ID,
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
            session_id=AGENT_SESSION_ID,
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
            {"output": "1 file changed, 2 insertions(+)", "exitCode": 0},
            command="git commit -m 'Fix bug'",
        )
        detect_bash_commit(event, variables, SESSION_ID)
        assert variables["task_has_commits"] is True

    def test_command_fallback_requires_definitive_success(self, variables) -> None:
        event = _make_bash_event(
            "1 file changed, 2 insertions(+)",
            command="git commit -m 'Fix bug'",
            is_error=None,
        )

        detect_bash_commit(event, variables, SESSION_ID)

        assert "task_has_commits" not in variables

    def test_strict_commit_output_matches_with_unknown_outcome(self, variables) -> None:
        event = _make_bash_event(
            "[main abc1234] Fix bug\n 1 file changed",
            command="git commit -m 'Fix bug'",
            is_error=None,
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
        session_id=AGENT_SESSION_ID,
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


class TestShellToolSucceeded:
    """Verify shell outcomes stay unknown until an adapter supplies a signal."""

    @pytest.mark.parametrize(
        ("tool_output", "expected"),
        [
            ({"exitCode": 0}, True),
            ({"exit_code": 2}, False),
            ({"success": True}, True),
            ({"status": "failed"}, False),
            ({"status": "complete"}, None),
            ({"status": "completed"}, None),
            ({"output": "failed but outcome is not structured"}, None),
        ],
    )
    def test_dict_outcomes_are_three_valued(
        self, tool_output: dict[str, object], expected: bool | None
    ) -> None:
        event = _make_bash_event_dict(tool_output)

        assert _shell_tool_succeeded(event) is expected

    def test_explicit_failure_conflicting_with_success_signal_is_unknown(self) -> None:
        event = _make_bash_event_dict({"exitCode": 0}, is_error=True)

        assert _shell_tool_succeeded(event) is None


class TestIsGitCommitCommand:
    """Verify _is_git_commit_command matches git commit invocations."""

    def test_simple_commit(self) -> None:
        assert _is_git_commit_command("git commit -m 'msg'") is True

    def test_commit_with_flags(self) -> None:
        assert _is_git_commit_command("git commit --amend --no-edit") is True

    def test_chained_commands(self) -> None:
        assert _is_git_commit_command("git add . && git commit -m 'msg'") is True

    def test_global_option_before_commit(self) -> None:
        assert _is_git_commit_command("git -C /repo commit -m 'msg'") is True

    def test_git_binary_path_before_commit(self) -> None:
        assert _is_git_commit_command("/usr/bin/git -c user.name=Gobby commit -m 'msg'") is True

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

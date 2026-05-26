"""Tests for Claude Code adapter hook translation.

Exercises ClaudeCodeAdapter with real HookEvent/HookResponse objects.
Only external I/O (HookManager daemon calls) is mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.adapters.base import ADAPTER_EMPTY_BLOCK_REASON_SENTINEL
from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.claude_contract import (
    CLAUDE_HOOK_EVENT_NAME_MAP,
    CLAUDE_NATIVE_HOOK_NAMES,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource

pytestmark = pytest.mark.unit


class TestClaudeCodeAdapterInit:
    """Test adapter initialization."""

    def test_source_is_claude(self) -> None:
        adapter = ClaudeCodeAdapter()
        assert adapter.source == SessionSource.CLAUDE

    def test_init_without_hook_manager(self) -> None:
        adapter = ClaudeCodeAdapter()
        assert adapter._hook_manager is None

    def test_init_with_hook_manager(self) -> None:
        mock_hm = MagicMock()
        adapter = ClaudeCodeAdapter(hook_manager=mock_hm)
        assert adapter._hook_manager is mock_hm


class TestEventMap:
    """Verify all Claude Code hook types are correctly mapped."""

    def test_all_hook_types_mapped(self) -> None:
        adapter = ClaudeCodeAdapter()
        assert set(adapter.EVENT_MAP.keys()) == set(CLAUDE_NATIVE_HOOK_NAMES)

    @pytest.mark.parametrize(
        "hook_type,expected_event_type",
        [
            ("session-start", HookEventType.SESSION_START),
            ("session-end", HookEventType.SESSION_END),
            ("user-prompt-submit", HookEventType.BEFORE_AGENT),
            ("stop", HookEventType.STOP),
            ("pre-tool-use", HookEventType.BEFORE_TOOL),
            ("post-tool-use", HookEventType.AFTER_TOOL),
            ("post-tool-use-failure", HookEventType.AFTER_TOOL),
            ("pre-compact", HookEventType.PRE_COMPACT),
            ("subagent-start", HookEventType.SUBAGENT_START),
            ("subagent-stop", HookEventType.SUBAGENT_STOP),
            ("permission-request", HookEventType.PERMISSION_REQUEST),
            ("notification", HookEventType.NOTIFICATION),
        ],
    )
    def test_event_map_entry(self, hook_type: str, expected_event_type: HookEventType) -> None:
        adapter = ClaudeCodeAdapter()
        assert adapter.EVENT_MAP[hook_type] == expected_event_type


class TestHookEventNameMap:
    """Verify HOOK_EVENT_NAME_MAP completeness."""

    def test_all_event_map_keys_have_names(self) -> None:
        adapter = ClaudeCodeAdapter()
        for key in adapter.EVENT_MAP:
            assert key in adapter.HOOK_EVENT_NAME_MAP, f"Missing HOOK_EVENT_NAME_MAP for {key}"

    @pytest.mark.parametrize(
        "hook_type,expected_name",
        [
            ("session-start", "SessionStart"),
            ("session-end", "SessionEnd"),
            ("user-prompt-submit", "UserPromptSubmit"),
            ("stop", "Stop"),
            ("pre-tool-use", "PreToolUse"),
            ("post-tool-use", "PostToolUse"),
            ("post-tool-use-failure", "PostToolUseFailure"),
            ("pre-compact", "PreCompact"),
            ("subagent-start", "SubagentStart"),
            ("subagent-stop", "SubagentStop"),
            ("permission-request", "PermissionRequest"),
            ("notification", "Notification"),
        ],
    )
    def test_hook_event_name(self, hook_type: str, expected_name: str) -> None:
        adapter = ClaudeCodeAdapter()
        assert adapter.HOOK_EVENT_NAME_MAP[hook_type] == expected_name

    def test_matches_shared_contract(self) -> None:
        adapter = ClaudeCodeAdapter()
        assert adapter.HOOK_EVENT_NAME_MAP == dict(CLAUDE_HOOK_EVENT_NAME_MAP)


class TestTranslateToHookEvent:
    """Test translation from Claude Code native events to unified HookEvent."""

    def test_session_start_full(self) -> None:
        adapter = ClaudeCodeAdapter()
        native = {
            "hook_type": "session-start",
            "input_data": {
                "session_id": "ext-123",
                "machine_id": "machine-abc",
                "cwd": "/projects/test",
            },
        }
        event = adapter.translate_to_hook_event(native)
        assert event.event_type == HookEventType.SESSION_START
        assert event.session_id == "ext-123"
        assert event.source == SessionSource.CLAUDE
        assert event.machine_id == "machine-abc"
        assert event.cwd == "/projects/test"
        assert event.timestamp is not None
        assert event.metadata == {}

    def test_top_level_platform_session_id_copied_to_metadata(self) -> None:
        adapter = ClaudeCodeAdapter()
        native = {
            "hook_type": "pre-tool-use",
            "_platform_session_id": "platform-session-123",
            "input_data": {
                "session_id": "claude-external-456",
                "tool_name": "Write",
                "tool_input": {"file_path": "/project/.gobby/plans/task.md"},
            },
        }

        event = adapter.translate_to_hook_event(native)

        assert event.session_id == "claude-external-456"
        assert event.metadata["_platform_session_id"] == "platform-session-123"

    def test_pre_tool_use(self) -> None:
        adapter = ClaudeCodeAdapter()
        native = {
            "hook_type": "pre-tool-use",
            "input_data": {
                "session_id": "ext-456",
                "tool_name": "Read",
                "tool_input": {"file_path": "/src/main.py"},
            },
        }
        event = adapter.translate_to_hook_event(native)
        assert event.event_type == HookEventType.BEFORE_TOOL
        assert event.data["tool_name"] == "Read"
        assert event.data["tool_input"] == {"file_path": "/src/main.py"}

    def test_post_tool_use(self) -> None:
        adapter = ClaudeCodeAdapter()
        native = {
            "hook_type": "post-tool-use",
            "input_data": {
                "session_id": "ext-789",
                "tool_name": "Bash",
                "tool_result": "command output",
            },
        }
        event = adapter.translate_to_hook_event(native)
        assert event.event_type == HookEventType.AFTER_TOOL
        assert event.metadata == {}
        # tool_result should be normalized to tool_output
        assert event.data["tool_output"] == "command output"

    def test_post_tool_use_failure_sets_is_failure(self) -> None:
        adapter = ClaudeCodeAdapter()
        native = {
            "hook_type": "post-tool-use-failure",
            "input_data": {
                "session_id": "ext-789",
                "tool_name": "Bash",
                "tool_result": "error output",
            },
        }
        event = adapter.translate_to_hook_event(native)
        assert event.event_type == HookEventType.AFTER_TOOL
        assert event.metadata["is_failure"] is True

    def test_unknown_hook_type_fallback_to_notification(self) -> None:
        adapter = ClaudeCodeAdapter()
        native = {
            "hook_type": "unknown-future-hook",
            "input_data": {"session_id": "ext-000"},
        }
        event = adapter.translate_to_hook_event(native)
        assert event.event_type == HookEventType.NOTIFICATION

    def test_empty_hook_type(self) -> None:
        adapter = ClaudeCodeAdapter()
        native = {"input_data": {"session_id": "ext-000"}}
        event = adapter.translate_to_hook_event(native)
        assert event.event_type == HookEventType.NOTIFICATION

    def test_missing_input_data(self) -> None:
        adapter = ClaudeCodeAdapter()
        native = {"hook_type": "session-start"}
        event = adapter.translate_to_hook_event(native)
        assert event.session_id == ""
        assert event.machine_id is None
        assert event.cwd is None
        assert event.data == {}

    def test_none_input_data(self) -> None:
        adapter = ClaudeCodeAdapter()
        native = {"hook_type": "session-start", "input_data": None}
        event = adapter.translate_to_hook_event(native)
        assert event.session_id == ""
        assert event.data == {}

    def test_session_end(self) -> None:
        adapter = ClaudeCodeAdapter()
        native = {
            "hook_type": "session-end",
            "input_data": {"session_id": "ext-end"},
        }
        event = adapter.translate_to_hook_event(native)
        assert event.event_type == HookEventType.SESSION_END

    def test_user_prompt_submit(self) -> None:
        adapter = ClaudeCodeAdapter()
        native = {
            "hook_type": "user-prompt-submit",
            "input_data": {"session_id": "ext-prompt", "user_prompt": "Hello"},
        }
        event = adapter.translate_to_hook_event(native)
        assert event.event_type == HookEventType.BEFORE_AGENT
        assert event.data["prompt"] == "Hello"
        assert event.data["user_prompt"] == "Hello"

    def test_stop(self) -> None:
        adapter = ClaudeCodeAdapter()
        native = {
            "hook_type": "stop",
            "input_data": {"session_id": "ext-stop"},
        }
        event = adapter.translate_to_hook_event(native)
        assert event.event_type == HookEventType.STOP

    def test_pre_compact(self) -> None:
        adapter = ClaudeCodeAdapter()
        native = {
            "hook_type": "pre-compact",
            "input_data": {"session_id": "ext-compact"},
        }
        event = adapter.translate_to_hook_event(native)
        assert event.event_type == HookEventType.PRE_COMPACT

    def test_subagent_start(self) -> None:
        adapter = ClaudeCodeAdapter()
        native = {
            "hook_type": "subagent-start",
            "input_data": {"session_id": "ext-sub"},
        }
        event = adapter.translate_to_hook_event(native)
        assert event.event_type == HookEventType.SUBAGENT_START

    def test_subagent_stop(self) -> None:
        adapter = ClaudeCodeAdapter()
        native = {
            "hook_type": "subagent-stop",
            "input_data": {"session_id": "ext-sub"},
        }
        event = adapter.translate_to_hook_event(native)
        assert event.event_type == HookEventType.SUBAGENT_STOP

    def test_permission_request(self) -> None:
        adapter = ClaudeCodeAdapter()
        native = {
            "hook_type": "permission-request",
            "input_data": {"session_id": "ext-perm"},
        }
        event = adapter.translate_to_hook_event(native)
        assert event.event_type == HookEventType.PERMISSION_REQUEST


class TestBashFailureDetection:
    """Test that Bash failures are detected via tool_result content."""

    def test_post_tool_use_bash_failure_sets_is_failure(self) -> None:
        """post-tool-use with Bash failure content → metadata.is_failure is True."""
        adapter = ClaudeCodeAdapter()
        native = {
            "hook_type": "post-tool-use",
            "input_data": {
                "session_id": "ext-bash",
                "tool_name": "Bash",
                "tool_result": "ruff check failed\nExit code: 1",
            },
        }
        event = adapter.translate_to_hook_event(native)
        assert event.event_type == HookEventType.AFTER_TOOL
        assert event.metadata.get("is_failure") is True
        assert event.data.get("is_error") is True

    def test_post_tool_use_bash_success_no_failure(self) -> None:
        """post-tool-use with Bash success → no is_failure metadata."""
        adapter = ClaudeCodeAdapter()
        native = {
            "hook_type": "post-tool-use",
            "input_data": {
                "session_id": "ext-bash",
                "tool_name": "Bash",
                "tool_result": "all good\nExit code: 0",
            },
        }
        event = adapter.translate_to_hook_event(native)
        assert event.metadata == {}

    def test_post_tool_use_non_bash_unaffected(self) -> None:
        """post-tool-use with non-Bash tool → no is_failure even with exit code text."""
        adapter = ClaudeCodeAdapter()
        native = {
            "hook_type": "post-tool-use",
            "input_data": {
                "session_id": "ext-read",
                "tool_name": "Read",
                "tool_result": "Exit code: 1",
            },
        }
        event = adapter.translate_to_hook_event(native)
        assert event.metadata == {}


class TestNormalizeEventData:
    """Test _normalize_event_data normalization logic."""

    def test_call_tool_mcp_extraction(self) -> None:
        adapter = ClaudeCodeAdapter()
        data = {
            "tool_name": "call_tool",
            "tool_input": {"server_name": "gobby", "tool_name": "create_task"},
        }
        result = adapter._normalize_event_data(data)
        assert result["mcp_server"] == "gobby"
        assert result["mcp_tool"] == "create_task"

    def test_prefixed_call_tool_mcp_extraction(self) -> None:
        adapter = ClaudeCodeAdapter()
        data = {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {"server_name": "gobby-tasks", "tool_name": "list_tasks"},
        }
        result = adapter._normalize_event_data(data)
        assert result["mcp_server"] == "gobby-tasks"
        assert result["mcp_tool"] == "list_tasks"

    def test_no_overwrite_existing_mcp_fields(self) -> None:
        adapter = ClaudeCodeAdapter()
        data = {
            "tool_name": "call_tool",
            "tool_input": {"server_name": "new", "tool_name": "new_tool"},
            "mcp_server": "existing",
            "mcp_tool": "existing_tool",
        }
        result = adapter._normalize_event_data(data)
        assert result["mcp_server"] == "existing"
        assert result["mcp_tool"] == "existing_tool"

    def test_non_call_tool_no_mcp_extraction(self) -> None:
        adapter = ClaudeCodeAdapter()
        data = {
            "tool_name": "Read",
            "tool_input": {"server_name": "something", "tool_name": "other"},
        }
        result = adapter._normalize_event_data(data)
        assert "mcp_server" not in result
        assert "mcp_tool" not in result

    def test_mcp_prefix_parsed_for_native_tools(self) -> None:
        """Native MCP tools like mcp__gobby__list_tools get mcp_server/mcp_tool from prefix."""
        adapter = ClaudeCodeAdapter()
        data = {
            "tool_name": "mcp__gobby__list_tools",
            "tool_input": {"server_name": "gobby-tasks"},
        }
        result = adapter._normalize_event_data(data)
        assert result["mcp_server"] == "gobby"
        assert result["mcp_tool"] == "list_tools"

    def test_mcp_prefix_parsed_for_get_tool_schema(self) -> None:
        """get_tool_schema should be recognized as MCP tool."""
        adapter = ClaudeCodeAdapter()
        data = {
            "tool_name": "mcp__gobby__get_tool_schema",
            "tool_input": {"server_name": "gobby-tasks", "tool_name": "create_task"},
        }
        result = adapter._normalize_event_data(data)
        assert result["mcp_server"] == "gobby"
        assert result["mcp_tool"] == "get_tool_schema"

    def test_user_prompt_sets_canonical_prompt_without_overwriting_existing_prompt(self) -> None:
        adapter = ClaudeCodeAdapter()
        data = {
            "prompt": "Canonical prompt",
            "user_prompt": "Raw Claude prompt",
        }
        result = adapter._normalize_event_data(data)
        assert result["prompt"] == "Canonical prompt"
        assert result["user_prompt"] == "Raw Claude prompt"

    def test_mcp_prefix_call_tool_overrides_with_inner(self) -> None:
        """mcp__gobby__call_tool should use inner server/tool from tool_input."""
        adapter = ClaudeCodeAdapter()
        data = {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {"server_name": "gobby-tasks", "tool_name": "create_task"},
        }
        result = adapter._normalize_event_data(data)
        # Inner target overrides prefix-parsed "gobby"/"call_tool"
        assert result["mcp_server"] == "gobby-tasks"
        assert result["mcp_tool"] == "create_task"

    def test_mcp_prefix_not_parsed_for_short_prefix(self) -> None:
        """Tool names with only one __ segment should not be parsed."""
        adapter = ClaudeCodeAdapter()
        data = {"tool_name": "mcp__weird", "tool_input": {}}
        result = adapter._normalize_event_data(data)
        assert "mcp_server" not in result
        assert "mcp_tool" not in result

    def test_tool_result_to_output(self) -> None:
        adapter = ClaudeCodeAdapter()
        data = {"tool_name": "Read", "tool_result": "file contents"}
        result = adapter._normalize_event_data(data)
        assert result["tool_output"] == "file contents"

    def test_no_overwrite_existing_tool_output(self) -> None:
        adapter = ClaudeCodeAdapter()
        data = {
            "tool_name": "Read",
            "tool_result": "raw",
            "tool_output": "already set",
        }
        result = adapter._normalize_event_data(data)
        assert result["tool_output"] == "already set"

    def test_no_tool_result_no_tool_output(self) -> None:
        adapter = ClaudeCodeAdapter()
        data = {"tool_name": "Read"}
        result = adapter._normalize_event_data(data)
        assert "tool_output" not in result

    def test_none_tool_input(self) -> None:
        adapter = ClaudeCodeAdapter()
        data = {"tool_name": "call_tool", "tool_input": None}
        result = adapter._normalize_event_data(data)
        # With None tool_input, no mcp_server/mcp_tool should be set
        assert "mcp_server" not in result
        assert "mcp_tool" not in result

    def test_empty_input_data(self) -> None:
        adapter = ClaudeCodeAdapter()
        result = adapter._normalize_event_data({})
        assert result == {}

    def test_original_dict_not_mutated(self) -> None:
        adapter = ClaudeCodeAdapter()
        original = {"tool_name": "Read", "tool_result": "data"}
        adapter._normalize_event_data(original)
        assert "tool_output" not in original


class TestTranslateFromHookResponse:
    """Test translation from unified HookResponse to Claude Code format."""

    def test_allow_decision(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(decision="allow")
        result = adapter.translate_from_hook_response(response, hook_type="pre-tool-use")
        assert result["continue"] is True
        assert "decision" not in result
        assert "stopReason" not in result

    def test_deny_decision(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(decision="deny", reason="Not allowed")
        result = adapter.translate_from_hook_response(response, hook_type="pre-tool-use")
        assert result["continue"] is True
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert result["hookSpecificOutput"]["permissionDecisionReason"] == "Not allowed"

    def test_pre_tool_use_block_emits_single_channel(self) -> None:
        """Rule-engine block on PreToolUse must surface only via the structured
        permissionDecisionReason channel — never as top-level stopReason +
        continue:false. Two channels both render in Claude (PreToolUse: ...
        blocking error AND Error: ...), so the adapter must pick exactly one.
        """
        adapter = ClaudeCodeAdapter()
        response = HookResponse(
            decision="block",
            reason="Rule enforced by Gobby: [require-code-index-skill]\nUse gcode.",
        )
        result = adapter.translate_from_hook_response(response, hook_type="pre-tool-use")

        assert result["continue"] is True
        assert "reason" not in result
        assert "stopReason" not in result
        assert "decision" not in result
        hso = result["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert hso["permissionDecision"] == "deny"
        assert hso["permissionDecisionReason"] == (
            "Gobby blocked [require-code-index-skill]: Use gcode."
        )

    def test_pre_tool_use_block_compacts_rule_reason_and_preserves_action(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(
            decision="block",
            reason=(
                "Rule enforced by Gobby: [require-task-creation-skill-loaded]\n"
                "Task lifecycle tools require the task creation skill.\n"
                'Call get_skill(name="task-creation") on gobby-skills through mcp__gobby__ progressive discovery'
            ),
        )
        result = adapter.translate_from_hook_response(response, hook_type="pre-tool-use")

        assert result == {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Gobby blocked [require-task-creation-skill-loaded]: "
                    "Task lifecycle tools require the task creation skill; "
                    'Call get_skill(name="task-creation") on gobby-skills through mcp__gobby__ progressive discovery'
                ),
            },
        }

    def test_block_decision(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(decision="block", reason="Blocked by policy")
        result = adapter.translate_from_hook_response(response)
        assert result["continue"] is False
        assert result["stopReason"] == "Blocked by policy"
        assert "decision" not in result

    def test_deny_without_reason(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(decision="deny")
        result = adapter.translate_from_hook_response(response)
        assert result["continue"] is False
        assert result["stopReason"] == ADAPTER_EMPTY_BLOCK_REASON_SENTINEL
        assert "decision" not in result

    def test_ask_decision(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(decision="ask")
        result = adapter.translate_from_hook_response(response)
        assert result["continue"] is True
        assert "decision" not in result

    def test_modify_decision(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(decision="modify")
        result = adapter.translate_from_hook_response(response)
        assert result["continue"] is True
        assert "decision" not in result

    def test_system_message(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(decision="allow", system_message="System notification")
        result = adapter.translate_from_hook_response(response)
        assert result["systemMessage"] == "System notification"

    def test_no_system_message(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(decision="allow")
        result = adapter.translate_from_hook_response(response)
        assert "systemMessage" not in result

    def test_context_injection_pre_tool_use(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(decision="allow", context="Important context")
        result = adapter.translate_from_hook_response(response, hook_type="pre-tool-use")
        assert "hookSpecificOutput" in result
        hso = result["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert "Important context" in hso["additionalContext"]

    def test_context_injection_user_prompt_submit(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(decision="allow", context="Prompt context")
        result = adapter.translate_from_hook_response(response, hook_type="user-prompt-submit")
        assert "hookSpecificOutput" in result
        assert result["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"

    def test_context_injection_session_start(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(decision="allow", context="Welcome context")
        result = adapter.translate_from_hook_response(response, hook_type="session-start")
        assert "hookSpecificOutput" in result
        assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"

    def test_session_start_routes_banner_to_additional_context_only(self) -> None:
        adapter = ClaudeCodeAdapter()
        banner = "Gobby Session ID: #100 (uuid-123)"
        response = HookResponse(decision="allow", system_message=banner)

        result = adapter.translate_from_hook_response(response, hook_type="session-start")

        assert "systemMessage" not in result
        assert result["hookSpecificOutput"]["additionalContext"].count(banner) == 1

    def test_context_injection_post_tool_use(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(decision="allow", context="Post tool context")
        result = adapter.translate_from_hook_response(response, hook_type="post-tool-use")
        assert "hookSpecificOutput" in result
        assert result["hookSpecificOutput"]["hookEventName"] == "PostToolUse"

    def test_context_injection_post_tool_use_failure(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(decision="allow", context="Failure context")
        result = adapter.translate_from_hook_response(response, hook_type="post-tool-use-failure")
        assert result["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
        assert "Failure context" in result["hookSpecificOutput"]["additionalContext"]

    def test_context_injection_notification(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(decision="allow", context="Notification context")
        result = adapter.translate_from_hook_response(response, hook_type="notification")
        assert result["hookSpecificOutput"]["hookEventName"] == "Notification"
        assert "Notification context" in result["hookSpecificOutput"]["additionalContext"]

    def test_context_injection_subagent_start(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(decision="allow", context="Subagent context")
        result = adapter.translate_from_hook_response(response, hook_type="subagent-start")
        assert result["hookSpecificOutput"]["hookEventName"] == "SubagentStart"
        assert "Subagent context" in result["hookSpecificOutput"]["additionalContext"]

    def test_permission_request_allow_uses_nested_decision(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(
            decision="allow",
            modified_input={"command": "npm run lint"},
            updated_permissions=[
                {"type": "setMode", "mode": "acceptEdits", "destination": "session"}
            ],
        )
        result = adapter.translate_from_hook_response(response, hook_type="permission-request")
        decision = result["hookSpecificOutput"]["decision"]
        assert decision["behavior"] == "allow"
        assert decision["updatedInput"] == {"command": "npm run lint"}
        assert decision["updatedPermissions"] == [
            {"type": "setMode", "mode": "acceptEdits", "destination": "session"}
        ]

    def test_permission_request_block_sets_interrupt(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(decision="block", reason="Needs human review")
        result = adapter.translate_from_hook_response(response, hook_type="permission-request")
        decision = result["hookSpecificOutput"]["decision"]
        assert result["continue"] is True
        assert decision["behavior"] == "deny"
        assert decision["message"] == "Needs human review"
        assert decision["interrupt"] is True

    def test_no_context_no_hook_specific_output(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(decision="allow")
        result = adapter.translate_from_hook_response(response, hook_type="pre-tool-use")
        assert "hookSpecificOutput" not in result

    def test_non_context_hook_event_name_no_hook_specific_output(self) -> None:
        """Hook types without additionalContext support should not produce hookSpecificOutput."""
        adapter = ClaudeCodeAdapter()
        response = HookResponse(decision="allow", context="Some context")
        result = adapter.translate_from_hook_response(response, hook_type="pre-compact")
        assert "hookSpecificOutput" not in result

    def test_stop_hook_no_hook_specific_output(self) -> None:
        """Stop hook should not produce hookSpecificOutput."""
        adapter = ClaudeCodeAdapter()
        response = HookResponse(decision="allow", context="Stop context")
        result = adapter.translate_from_hook_response(response, hook_type="stop")
        assert "hookSpecificOutput" not in result

    def test_no_hook_type_no_hook_specific_output(self) -> None:
        """No hook_type provided (None) with context should not produce hookSpecificOutput."""
        adapter = ClaudeCodeAdapter()
        response = HookResponse(decision="allow", context="context")
        result = adapter.translate_from_hook_response(response, hook_type=None)
        # "Unknown" is not in valid_hook_event_names
        assert "hookSpecificOutput" not in result


class TestResponseMetadata:
    """Test metadata injection into hookSpecificOutput."""

    def test_first_hook_full_metadata(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(
            decision="allow",
            metadata={
                "session_id": "uuid-123",
                "session_ref": "#100",
                "external_id": "ext-id-456",
                "_first_hook_for_session": True,
                "parent_session_id": "parent-uuid",
                "machine_id": "machine-abc",
                "project_id": "proj-xyz",
                "terminal_term_program": "iTerm2",
                "terminal_tty": "/dev/ttys005",
                "terminal_parent_pid": "12345",
            },
        )
        result = adapter.translate_from_hook_response(response, hook_type="session-start")
        assert "hookSpecificOutput" in result
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Gobby Session ID: #100 (uuid-123)" in ctx
        assert "ext-id-456" in ctx
        assert "parent-uuid" in ctx
        assert "machine-abc" in ctx
        assert "proj-xyz" in ctx
        assert "iTerm2" in ctx
        assert "/dev/ttys005" in ctx
        assert "12345" in ctx

    def test_first_hook_without_session_ref(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(
            decision="allow",
            metadata={
                "session_id": "uuid-123",
                "_first_hook_for_session": True,
            },
        )
        result = adapter.translate_from_hook_response(response, hook_type="session-start")
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Gobby Session ID: uuid-123" in ctx

    def test_no_metadata_on_subsequent_hooks(self) -> None:
        """Subsequent hooks do not inject session ref."""
        adapter = ClaudeCodeAdapter()
        response = HookResponse(
            decision="allow",
            metadata={
                "session_id": "uuid-123",
                "session_ref": "#100",
                "_first_hook_for_session": False,
            },
        )
        result = adapter.translate_from_hook_response(response, hook_type="pre-tool-use")
        assert "hookSpecificOutput" not in result

    def test_terminal_session_ids(self) -> None:
        """Terminal-specific session IDs are included in first hook."""
        adapter = ClaudeCodeAdapter()
        terminal_keys = [
            ("terminal_tmux_pane", "%42"),
        ]
        for key, value in terminal_keys:
            response = HookResponse(
                decision="allow",
                metadata={
                    "session_id": "uuid-123",
                    "_first_hook_for_session": True,
                    key: value,
                },
            )
            result = adapter.translate_from_hook_response(response, hook_type="session-start")
            ctx = result["hookSpecificOutput"]["additionalContext"]
            assert value in ctx, f"Expected {value} in context for {key}"

    def test_context_and_metadata_combined(self) -> None:
        """Both workflow context and session metadata appear in output."""
        adapter = ClaudeCodeAdapter()
        response = HookResponse(
            decision="allow",
            context="Workflow injected context",
            metadata={
                "session_id": "uuid-123",
                "session_ref": "#100",
                "_first_hook_for_session": True,
            },
        )
        result = adapter.translate_from_hook_response(response, hook_type="session-start")
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Workflow injected context" in ctx
        assert "#100" in ctx

    def test_session_start_banner_and_metadata_include_session_id_once(self) -> None:
        """SessionStart does not duplicate the session ID between banner and metadata."""
        adapter = ClaudeCodeAdapter()
        banner = "Gobby Session ID: #100 (uuid-123)"
        response = HookResponse(
            decision="allow",
            system_message=banner,
            metadata={
                "session_id": "uuid-123",
                "session_ref": "#100",
                "external_id": "ext-id-456",
                "_first_hook_for_session": True,
                "project_id": "proj-xyz",
            },
        )

        result = adapter.translate_from_hook_response(response, hook_type="session-start")

        assert "systemMessage" not in result
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert ctx.count(banner) == 1
        assert "ext-id-456" in ctx
        assert "proj-xyz" in ctx

    def test_empty_metadata(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(decision="allow", metadata={})
        result = adapter.translate_from_hook_response(response, hook_type="pre-tool-use")
        assert "hookSpecificOutput" not in result


class TestHandleNative:
    """Test the full handle_native round-trip."""

    def test_handle_native_session_start(self) -> None:
        """Full round-trip: native event -> HookEvent -> HookManager.handle -> Claude response."""
        adapter = ClaudeCodeAdapter()
        mock_hook_manager = MagicMock()
        mock_hook_manager.handle.return_value = HookResponse(
            decision="allow",
            context="Welcome to session",
            metadata={
                "session_id": "plat-id",
                "session_ref": "#42",
                "_first_hook_for_session": True,
            },
        )

        native = {
            "hook_type": "session-start",
            "input_data": {
                "session_id": "ext-123",
                "machine_id": "machine-1",
                "cwd": "/project",
            },
        }
        result = adapter.handle_native(native, mock_hook_manager)

        # Verify HookManager.handle was called with correct HookEvent
        mock_hook_manager.handle.assert_called_once()
        event = mock_hook_manager.handle.call_args[0][0]
        assert isinstance(event, HookEvent)
        assert event.event_type == HookEventType.SESSION_START
        assert event.session_id == "ext-123"
        assert event.source == SessionSource.CLAUDE

        # Verify the response
        assert result["continue"] is True
        assert "hookSpecificOutput" in result
        assert "#42" in result["hookSpecificOutput"]["additionalContext"]

    def test_handle_native_pre_tool_deny(self) -> None:
        """Pre-tool-use denied by workflow."""
        adapter = ClaudeCodeAdapter()
        mock_hook_manager = MagicMock()
        mock_hook_manager.handle.return_value = HookResponse(
            decision="deny",
            reason="Tool not allowed in this workflow step",
        )

        native = {
            "hook_type": "pre-tool-use",
            "input_data": {
                "session_id": "ext-456",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
            },
        }
        result = adapter.handle_native(native, mock_hook_manager)

        assert result["continue"] is True
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert (
            result["hookSpecificOutput"]["permissionDecisionReason"]
            == "Tool not allowed in this workflow step"
        )

    def test_handle_native_preserves_hook_type_in_response(self) -> None:
        """hook_type is used for hookEventName in response."""
        adapter = ClaudeCodeAdapter()
        mock_hook_manager = MagicMock()
        mock_hook_manager.handle.return_value = HookResponse(
            decision="allow",
            context="Post-tool analysis",
        )

        native = {
            "hook_type": "post-tool-use",
            "input_data": {
                "session_id": "ext-789",
                "tool_name": "Read",
                "tool_result": "file content",
            },
        }
        result = adapter.handle_native(native, mock_hook_manager)
        assert result["hookSpecificOutput"]["hookEventName"] == "PostToolUse"

    def test_handle_native_notification_context_uses_hookspecific(self) -> None:
        """Notification now supports docs-backed additionalContext."""
        adapter = ClaudeCodeAdapter()
        mock_hook_manager = MagicMock()
        mock_hook_manager.handle.return_value = HookResponse(
            decision="allow",
            context="some notification context",
        )

        native = {
            "hook_type": "notification",
            "input_data": {"session_id": "ext-notif"},
        }
        result = adapter.handle_native(native, mock_hook_manager)
        assert result["hookSpecificOutput"]["hookEventName"] == "Notification"
        assert "some notification context" in result["hookSpecificOutput"]["additionalContext"]

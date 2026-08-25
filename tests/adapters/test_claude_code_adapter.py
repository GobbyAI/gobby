"""Tests for Claude Code adapter hook translation.

Exercises ClaudeCodeAdapter with real HookEvent/HookResponse objects.
Only external I/O (HookManager daemon calls) is mocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gobby.adapters.base import ADAPTER_EMPTY_BLOCK_REASON_SENTINEL
from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.claude_contract import (
    CLAUDE_HOOK_EVENT_NAME_MAP,
    CLAUDE_NATIVE_HOOK_NAMES,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.skills.formatting import skill_fetch_directive
from tests.framing_corpus import (
    SKILL_FETCH_REASON_TEMPLATE as _SKILL_FETCH_REASON_TEMPLATE,
)
from tests.framing_corpus import (
    bundled_before_tool_block_reasons as _bundled_before_tool_block_reasons,
)

pytestmark = pytest.mark.unit

_BUNDLED_BEFORE_TOOL_BLOCK_REASONS = _bundled_before_tool_block_reasons()


class TestBundledBlockReasonFraming:
    @pytest.mark.parametrize(
        ("rule_name", "raw_reason"),
        sorted(_BUNDLED_BEFORE_TOOL_BLOCK_REASONS.items()),
        ids=sorted(_BUNDLED_BEFORE_TOOL_BLOCK_REASONS),
    )
    def test_live_corpus_reaches_agent_without_compaction(
        self,
        rule_name: str,
        raw_reason: str,
    ) -> None:
        reason = (
            skill_fetch_directive("python")
            if rule_name == "require-claimed-task-required-skills"
            else raw_reason
        )
        agent_reason = f"Rule enforced by Gobby: [{rule_name}]\n{reason.rstrip()}"
        response = HookResponse(decision="block", reason=agent_reason)

        result = ClaudeCodeAdapter().translate_from_hook_response(
            response,
            hook_type="pre-tool-use",
        )

        assert result["hookSpecificOutput"]["permissionDecisionReason"] == agent_reason

    def test_skill_fetch_template_renders_call_at_offset_zero(self) -> None:
        assert (
            _BUNDLED_BEFORE_TOOL_BLOCK_REASONS["require-claimed-task-required-skills"]
            == _SKILL_FETCH_REASON_TEMPLATE
        )
        assert skill_fetch_directive("python").startswith("Load and fully read the skill")


def test_deny_reason_not_compacted() -> None:
    rule_name = "require-clean-tree-before-status"
    template_reason = _BUNDLED_BEFORE_TOOL_BLOCK_REASONS[rule_name]
    agent_reason = f"Rule enforced by Gobby: [{rule_name}]\n{template_reason.rstrip()}"
    assert len(agent_reason) > 300

    result = ClaudeCodeAdapter().translate_from_hook_response(
        HookResponse(decision="block", reason=agent_reason),
        hook_type="pre-tool-use",
    )

    visible_reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert visible_reason == agent_reason
    assert "release_task_paths" in visible_reason


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

    def test_class_docstring_describes_current_handler_only(self) -> None:
        assert ClaudeCodeAdapter.__doc__ is not None
        assert "HookManager.handle" in ClaudeCodeAdapter.__doc__
        assert "set_legacy_mode" not in ClaudeCodeAdapter.__doc__


class TestEventMap:
    """Verify all Claude Code hook types are correctly mapped."""

    def test_all_hook_types_mapped(self) -> None:
        adapter = ClaudeCodeAdapter()
        assert set(adapter.EVENT_MAP.keys()) == set(CLAUDE_NATIVE_HOOK_NAMES)

    @pytest.mark.parametrize(
        "hook_type,expected_event_type",
        [
            ("session-start", HookEventType.SESSION_START),
            ("setup", HookEventType.SETUP),
            ("session-end", HookEventType.SESSION_END),
            ("user-prompt-submit", HookEventType.BEFORE_AGENT),
            ("user-prompt-expansion", HookEventType.USER_PROMPT_EXPANSION),
            ("stop", HookEventType.STOP),
            ("pre-tool-use", HookEventType.BEFORE_TOOL),
            ("post-tool-use", HookEventType.AFTER_TOOL),
            ("post-tool-use-failure", HookEventType.AFTER_TOOL),
            ("post-tool-batch", HookEventType.POST_TOOL_BATCH),
            ("pre-compact", HookEventType.PRE_COMPACT),
            ("subagent-start", HookEventType.SUBAGENT_START),
            ("subagent-stop", HookEventType.SUBAGENT_STOP),
            ("permission-request", HookEventType.PERMISSION_REQUEST),
            ("notification", HookEventType.NOTIFICATION),
            ("message-display", HookEventType.MESSAGE_DISPLAY),
            ("directory-added", HookEventType.DIRECTORY_ADDED),
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
            ("setup", "Setup"),
            ("session-end", "SessionEnd"),
            ("user-prompt-submit", "UserPromptSubmit"),
            ("user-prompt-expansion", "UserPromptExpansion"),
            ("stop", "Stop"),
            ("pre-tool-use", "PreToolUse"),
            ("post-tool-use", "PostToolUse"),
            ("post-tool-use-failure", "PostToolUseFailure"),
            ("post-tool-batch", "PostToolBatch"),
            ("pre-compact", "PreCompact"),
            ("subagent-start", "SubagentStart"),
            ("subagent-stop", "SubagentStop"),
            ("permission-request", "PermissionRequest"),
            ("notification", "Notification"),
            ("message-display", "MessageDisplay"),
            ("directory-added", "DirectoryAdded"),
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
                "machine_id": "21000000-0000-4000-8000-000000000007",
                "cwd": "/projects/test",
            },
        }
        event = adapter.translate_to_hook_event(native)
        assert event.event_type == HookEventType.SESSION_START
        assert event.session_id == "ext-123"
        assert event.source == SessionSource.CLAUDE
        assert event.machine_id == "21000000-0000-4000-8000-000000000007"
        assert event.cwd == "/projects/test"
        assert event.timestamp is not None
        assert event.metadata == {"_native_hook_type": "session-start"}

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
        assert event.metadata == {
            "_native_hook_type": "post-tool-use",
            "is_failure": False,
        }
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

    @pytest.mark.parametrize(
        "hook_type,expected_event_type",
        [
            ("UserPromptSubmit", HookEventType.BEFORE_AGENT),
            ("PreToolUse", HookEventType.BEFORE_TOOL),
            ("PostToolUse", HookEventType.AFTER_TOOL),
            ("Stop", HookEventType.STOP),
            ("SessionStart", HookEventType.SESSION_START),
        ],
    )
    def test_pascalcase_hook_types_route_to_lifecycle_events(
        self, hook_type: str, expected_event_type: HookEventType
    ) -> None:
        """PascalCase hook event names must route like their kebab natives.

        Claude settings.json keys are PascalCase; an install that passes the
        PascalCase token through to ``--type`` must still route correctly instead
        of silently dropping to NOTIFICATION.
        """
        adapter = ClaudeCodeAdapter()
        native = {"hook_type": hook_type, "input_data": {"session_id": "ext-1"}}
        event = adapter.translate_to_hook_event(native)
        assert event.event_type == expected_event_type

    def test_pascalcase_post_tool_use_failure_sets_is_failure(self) -> None:
        adapter = ClaudeCodeAdapter()
        native = {
            "hook_type": "PostToolUseFailure",
            "input_data": {
                "session_id": "ext-789",
                "tool_name": "Bash",
                "tool_result": "error output",
            },
        }
        event = adapter.translate_to_hook_event(native)
        assert event.event_type == HookEventType.AFTER_TOOL
        assert event.metadata["is_failure"] is True

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

    @pytest.mark.parametrize(
        ("hook_type", "event_type", "payload"),
        [
            (
                "setup",
                HookEventType.SETUP,
                {
                    "session_id": "sess-setup",
                    "trigger": "maintenance",
                    "transcript_path": "/tmp/transcript.jsonl",
                    "cwd": "/tmp/project",
                    "permission_mode": "acceptEdits",
                },
            ),
            (
                "user-prompt-expansion",
                HookEventType.USER_PROMPT_EXPANSION,
                {
                    "session_id": "sess-expand",
                    "expansion_type": "skill",
                    "command_name": "review",
                    "command_args": "--strict",
                    "command_source": "project",
                    "prompt": "Expanded prompt",
                },
            ),
            (
                "post-tool-batch",
                HookEventType.POST_TOOL_BATCH,
                {
                    "session_id": "sess-batch",
                    "tool_calls": [
                        {
                            "tool_name": "Read",
                            "tool_input": {"file_path": "/tmp/a.py"},
                            "tool_use_id": "tool-1",
                            "tool_response": [{"type": "text", "text": "contents"}],
                        },
                        {
                            "tool_name": "Bash",
                            "tool_input": {"command": "false"},
                            "tool_use_id": "tool-2",
                            "tool_response": "exit code 1",
                        },
                    ],
                },
            ),
            (
                "message-display",
                HookEventType.MESSAGE_DISPLAY,
                {
                    "session_id": "sess-display",
                    "turn_id": "turn-1",
                    "message_id": "message-1",
                    "index": 3,
                    "final": True,
                    "delta": "Original transcript delta",
                    "transcript_path": "/tmp/transcript.jsonl",
                },
            ),
            (
                "directory-added",
                HookEventType.DIRECTORY_ADDED,
                {
                    "session_id": "sess-directory",
                    "directory": "/tmp/repo",
                    "source": "register_repo_root",
                },
            ),
        ],
    )
    def test_current_hook_payloads_preserve_native_fields(
        self,
        hook_type: str,
        event_type: HookEventType,
        payload: dict[str, object],
    ) -> None:
        event = ClaudeCodeAdapter().translate_to_hook_event(
            {"hook_type": hook_type, "input_data": payload}
        )

        assert event.event_type == event_type
        for field, value in payload.items():
            assert event.data[field] == value


class TestBashFailureDetection:
    """Test that Bash failures are detected via tool_result content."""

    def test_post_tool_use_bash_failure_text_keeps_success_hook_contract(self) -> None:
        """PostToolUse remains the definitive success event contract."""
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
        assert event.metadata["is_failure"] is False
        assert "is_error" not in event.data
        assert event.data["tool_outcome"]["status"] == "succeeded"

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
        assert event.metadata == {
            "_native_hook_type": "post-tool-use",
            "is_failure": False,
        }

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
        assert event.metadata == {
            "_native_hook_type": "post-tool-use",
            "is_failure": False,
        }

    def test_live_session_8944_post_tool_use_sets_definitive_success(self) -> None:
        fixture_path = (
            Path(__file__).parents[1]
            / "fixtures"
            / "provider_contracts"
            / "claude"
            / "session-8944-post-tool-use.json"
        )
        native = json.loads(fixture_path.read_text())

        event = ClaudeCodeAdapter().translate_to_hook_event(native)

        assert event.metadata["is_failure"] is False
        assert event.data["tool_output"]["stdout"].endswith("110 passed in 1.56s")


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

        The full reason must also preserve the concrete gcode replacement
        command so the agent sees the actionable directive intact.
        """
        adapter = ClaudeCodeAdapter()
        reason = (
            "Rule enforced by Gobby: [prefer-gcode-for-code-search]\n"
            'Use `gcode grep "pattern" [PATH...] -m 50` for exact text search, '
            'or `gcode search-content "query" [PATH...]` for ranked content search.'
        )
        response = HookResponse(
            decision="block",
            reason=reason,
        )
        result = adapter.translate_from_hook_response(response, hook_type="pre-tool-use")

        assert result["continue"] is True
        assert "reason" not in result
        assert "stopReason" not in result
        assert "decision" not in result
        hso = result["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert hso["permissionDecision"] == "deny"
        assert hso["permissionDecisionReason"] == reason

    def test_pre_tool_use_block_overrides_permission_allow(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(
            decision="block",
            permission_decision="allow",
            modified_input={"command": "unsafe command"},
            reason="Blocked by a lower-priority rule",
        )

        result = adapter.translate_from_hook_response(response, hook_type="pre-tool-use")

        assert result["continue"] is True
        hook_output = result["hookSpecificOutput"]
        assert hook_output["permissionDecision"] == "deny"
        assert hook_output["permissionDecisionReason"] == "Blocked by a lower-priority rule"
        assert "updatedInput" not in hook_output

    def test_pre_tool_use_code_index_skill_block_preserves_get_skill_directive(self) -> None:
        adapter = ClaudeCodeAdapter()
        directive = (
            'Call get_skill(name="code-index") on gobby-skills directly through '
            "mcp__gobby__call_tool: "
            'call_tool("gobby-skills", "get_skill", {"name": "code-index"}). Then continue.'
        )
        reason = (
            "Rule enforced by Gobby: [require-code-index-skill]\n"
            f"{directive} "
            'After loading, retry with `gcode grep "pattern" [PATH...] -m 50`, '
            '`gcode search-content "query" [PATH...]`, `gcode outline path/to/file`, '
            "or `gcode symbol <id>`."
        )
        response = HookResponse(
            decision="block",
            reason=reason,
        )
        result = adapter.translate_from_hook_response(response, hook_type="pre-tool-use")

        assert result["hookSpecificOutput"]["permissionDecisionReason"] == reason

    def test_pre_tool_use_source_read_block_preserves_gcode_replacements(self) -> None:
        adapter = ClaudeCodeAdapter()
        guidance = (
            "Use `gcode outline path/to/file` to inspect file structure or "
            "`gcode symbol <id>` to retrieve a target symbol before broad source reads."
        )
        reason = (
            "Rule enforced by Gobby: [prefer-gcode-for-source-read]\n"
            f"{guidance} Keep follow-up line reads to 40 lines or fewer."
        )
        response = HookResponse(
            decision="block",
            reason=reason,
        )
        result = adapter.translate_from_hook_response(response, hook_type="pre-tool-use")

        assert result["hookSpecificOutput"]["permissionDecisionReason"] == reason

    def test_pre_tool_use_aggregate_block_preserves_each_gate_action(self) -> None:
        adapter = ClaudeCodeAdapter()
        reason = (
            "Rule enforced by Gobby: [aggregated:2-gates]\n"
            "Multiple gates blocked while retrying Edit.\n"
            '1. [require-code-index-skill] Call get_skill(name="code-index") on '
            "gobby-skills directly through mcp__gobby__call_tool. Then continue.\n"
            '2. [prefer-gcode-for-code-search] Use `gcode grep "pattern" [PATH...] '
            "-m 50` for exact text search."
        )
        response = HookResponse(
            decision="block",
            reason=reason,
        )

        result = adapter.translate_from_hook_response(response, hook_type="pre-tool-use")

        assert result["hookSpecificOutput"]["permissionDecisionReason"] == reason

    def test_pre_tool_use_aggregate_block_reason_is_not_compacted(self) -> None:
        adapter = ClaudeCodeAdapter()
        reason = (
            "Rule enforced by Gobby: [aggregated:2-gates]\n"
            "Multiple gates blocked while retrying Edit.\n"
            '1. [prefer-gcode-for-code-search] Use `gcode grep "pattern" -m 50` or '
            "`gcode search-content` — the code index has full access to this repo "
            "and returns ranked, token-cheap results.\n"
            "2. [no-force-kill] Force-killing processes is not allowed. "
            "Use graceful signals."
        )
        response = HookResponse(
            decision="block",
            reason=reason,
        )

        result = adapter.translate_from_hook_response(response, hook_type="pre-tool-use")

        assert result["hookSpecificOutput"]["permissionDecisionReason"] == reason

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

    def test_provider_referenced_overflow_is_omitted_not_prefix_sliced(
        self,
    ) -> None:
        from gobby.llm.sdk_utils import ADDITIONAL_CONTEXT_LIMIT

        adapter = ClaudeCodeAdapter()
        unique_head = "UNIQUE_CLAUDE_OVERFLOW_7f3a9c"
        context = unique_head + ("x" * 10_001)
        assert len(context) > 10_000
        response = HookResponse(decision="allow", context=context)

        result = adapter.translate_from_hook_response(response, hook_type="user-prompt-submit")

        additional_context = result["hookSpecificOutput"]["additionalContext"]
        assert unique_head not in additional_context
        assert "[truncated]" not in additional_context
        assert "omitted contributors=[response.context]" in additional_context
        assert len(additional_context) <= ADDITIONAL_CONTEXT_LIMIT

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

    def test_session_start_live_context_does_not_replay_persona(self) -> None:
        adapter = ClaudeCodeAdapter()
        response = HookResponse(
            decision="allow",
            system_message="Gobby Session ID: #6273 (sess-live-123)",
            context="Claimed task refs: #15237 [in_progress]",
        )

        result = adapter.translate_from_hook_response(response, hook_type="session-start")

        assert "systemMessage" not in result
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Gobby Session ID: #6273 (sess-live-123)" in ctx
        assert "Claimed task refs: #15237 [in_progress]" in ctx
        assert "## Role" not in ctx
        assert "## Personality" not in ctx

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

    def test_setup_context_ignores_block(self) -> None:
        result = ClaudeCodeAdapter().translate_from_hook_response(
            HookResponse(decision="block", reason="ignored", context="Startup context"),
            hook_type="setup",
        )

        assert result == {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "Setup",
                "additionalContext": "Startup context",
            },
        }

    @pytest.mark.parametrize(
        ("hook_type", "hook_event_name"),
        [
            ("user-prompt-expansion", "UserPromptExpansion"),
            ("post-tool-batch", "PostToolBatch"),
        ],
    )
    def test_current_pre_model_hooks_block_with_context(
        self, hook_type: str, hook_event_name: str
    ) -> None:
        result = ClaudeCodeAdapter().translate_from_hook_response(
            HookResponse(decision="block", reason="Revise input", context="Relevant context"),
            hook_type=hook_type,
        )

        assert result == {
            "continue": True,
            "decision": "block",
            "reason": "Revise input",
            "hookSpecificOutput": {
                "hookEventName": hook_event_name,
                "additionalContext": "Relevant context",
            },
        }

    def test_message_display_emits_only_replacement_and_ignores_block(self) -> None:
        result = ClaudeCodeAdapter().translate_from_hook_response(
            HookResponse(
                decision="block",
                reason="ignored",
                context="ignored",
                system_message="ignored",
                display_content="Replacement delta",
            ),
            hook_type="message-display",
        )

        assert result == {
            "hookSpecificOutput": {
                "hookEventName": "MessageDisplay",
                "displayContent": "Replacement delta",
            }
        }

    def test_directory_added_ignores_block_and_keeps_system_message(self) -> None:
        result = ClaudeCodeAdapter().translate_from_hook_response(
            HookResponse(decision="block", reason="ignored", system_message="Directory tracked"),
            hook_type="directory-added",
        )

        assert result == {"continue": True, "systemMessage": "Directory tracked"}

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
                "machine_id": "21000000-0000-4000-8000-000000000007",
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
        assert "21000000-0000-4000-8000-000000000007" in ctx
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
                "machine_id": "21000000-0000-4000-8000-000000000001",
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

    def test_handle_native_pre_tool_rewrite_allows_updated_input(self) -> None:
        """PreToolUse rewrites preserve permission handling without auto_approve."""
        adapter = ClaudeCodeAdapter()
        mock_hook_manager = MagicMock()
        rewritten_input = {"command": "npm run lint"}
        mock_hook_manager.handle.return_value = HookResponse(
            decision="allow",
            modified_input=rewritten_input,
        )

        native = {
            "hook_type": "pre-tool-use",
            "input_data": {
                "session_id": "ext-457",
                "tool_name": "Bash",
                "tool_input": {"command": "npm test"},
            },
        }
        result = adapter.handle_native(native, mock_hook_manager)

        hook_output = result["hookSpecificOutput"]
        assert result["continue"] is True
        assert hook_output["hookEventName"] == "PreToolUse"
        assert "permissionDecision" not in hook_output
        assert hook_output["updatedInput"] == rewritten_input

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

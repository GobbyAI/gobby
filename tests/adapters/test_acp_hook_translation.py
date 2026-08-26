"""Tests for ACP hook adapter event and response translation."""

from datetime import UTC, datetime

import pytest

# Qwen now has a native terminal-hook contract; this file tests the shared ACP base.
from gobby.adapters.acp_hook_adapter import ACPHookAdapter as QwenAdapter
from gobby.adapters.grok import GrokAdapter
from gobby.hooks.events import HookEventType, HookResponse, SessionSource

pytestmark = pytest.mark.unit


class TestTranslateToHookEvent:
    """Tests for translate_to_hook_event() method."""

    def test_session_start_with_dispatcher_wrapper(self, adapter: QwenAdapter) -> None:
        """Translates SessionStart event with dispatcher wrapper format."""
        native_event = {
            "source": "qwen",
            "hook_type": "SessionStart",
            "input_data": {
                "hook_event_name": "SessionStart",
                "session_id": "qwen-sess-123",
                "cwd": "/home/user/project",
                "timestamp": "2025-01-15T10:30:00Z",
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.event_type == HookEventType.SESSION_START
        assert event.session_id == "qwen-sess-123"
        assert event.source == SessionSource.GROK
        assert event.cwd == "/home/user/project"
        assert event.data == native_event["input_data"]

    def test_session_start_without_wrapper(self, adapter: QwenAdapter) -> None:
        """Translates SessionStart event without dispatcher wrapper."""
        native_event = {
            "hook_event_name": "SessionStart",
            "session_id": "acp-sess-456",
            "cwd": "/tmp/project",
            "timestamp": "2025-01-15T11:00:00+00:00",
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.event_type == HookEventType.SESSION_START
        assert event.session_id == "acp-sess-456"
        assert event.cwd == "/tmp/project"

    def test_before_tool_with_tool_name(self, adapter: QwenAdapter) -> None:
        """Translates BeforeTool event and normalizes tool name."""
        native_event = {
            "hook_type": "BeforeTool",
            "input_data": {
                "hook_event_name": "BeforeTool",
                "session_id": "sess-789",
                "tool_name": "RunShellCommand",
                "tool_input": {"command": "ls -la"},
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.event_type == HookEventType.BEFORE_TOOL
        assert event.metadata["original_tool_name"] == "RunShellCommand"
        assert event.metadata["normalized_tool_name"] == "Bash"

    def test_after_tool_with_tool_name(self, adapter: QwenAdapter) -> None:
        """Translates AfterTool event and normalizes tool name."""
        native_event = {
            "hook_type": "AfterTool",
            "input_data": {
                "hook_event_name": "AfterTool",
                "session_id": "sess-789",
                "tool_name": "ReadFileTool",
                "tool_output": "file contents...",
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.event_type == HookEventType.AFTER_TOOL
        assert event.metadata["original_tool_name"] == "ReadFileTool"
        assert event.metadata["normalized_tool_name"] == "Read"

    def test_before_model_event(self, adapter: QwenAdapter) -> None:
        """Translates BeforeModel event (ACP-specific)."""
        native_event = {
            "hook_type": "BeforeModel",
            "input_data": {
                "hook_event_name": "BeforeModel",
                "session_id": "sess-model",
                "model": "provider-model",
                "prompt": "Hello, world!",
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.event_type == HookEventType.BEFORE_MODEL
        assert event.data["model"] == "provider-model"

    def test_after_model_event(self, adapter: QwenAdapter) -> None:
        """Translates AfterModel event (ACP-specific)."""
        native_event = {
            "hook_type": "AfterModel",
            "input_data": {
                "hook_event_name": "AfterModel",
                "session_id": "sess-model",
                "response": {"content": "Hello!"},
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.event_type == HookEventType.AFTER_MODEL

    def test_after_agent_normalizes_prompt_response(self, adapter: QwenAdapter) -> None:
        """ACP AfterAgent prompt_response is normalized to response."""
        native_event = {
            "hook_type": "AfterAgent",
            "input_data": {
                "hook_event_name": "AfterAgent",
                "session_id": "sess-after-agent",
                "prompt_response": "Completed answer",
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.event_type == HookEventType.AFTER_AGENT
        assert event.data["prompt_response"] == "Completed answer"
        assert event.data["response"] == "Completed answer"

    def test_before_tool_selection_event(self, adapter: QwenAdapter) -> None:
        """Translates BeforeToolSelection event (ACP-specific)."""
        native_event = {
            "hook_type": "BeforeToolSelection",
            "input_data": {
                "hook_event_name": "BeforeToolSelection",
                "session_id": "sess-tools",
                "available_tools": ["read_file", "write_file"],
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.event_type == HookEventType.BEFORE_TOOL_SELECTION

    def test_pre_compress_event(self, adapter: QwenAdapter) -> None:
        """Translates PreCompress to PRE_COMPACT."""
        native_event = {
            "hook_type": "PreCompress",
            "input_data": {
                "hook_event_name": "PreCompress",
                "session_id": "sess-compress",
                "context_length": 50000,
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.event_type == HookEventType.PRE_COMPACT

    def test_notification_event(self, adapter: QwenAdapter) -> None:
        """Translates Notification event."""
        native_event = {
            "hook_type": "Notification",
            "input_data": {
                "hook_event_name": "Notification",
                "session_id": "sess-notify",
                "message": "Task completed",
                "level": "info",
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.event_type == HookEventType.NOTIFICATION

    def test_unknown_event_type_defaults_to_notification(self, adapter: QwenAdapter) -> None:
        """Unknown event types default to NOTIFICATION (fail-open)."""
        native_event = {
            "hook_type": "UnknownHookType",
            "input_data": {
                "hook_event_name": "UnknownHookType",
                "session_id": "sess-unknown",
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.event_type == HookEventType.NOTIFICATION

    def test_timestamp_parsing_iso_with_z(self, adapter: QwenAdapter) -> None:
        """Parses ISO timestamp with Z suffix."""
        native_event = {
            "hook_type": "SessionStart",
            "input_data": {
                "session_id": "sess-time",
                "timestamp": "2025-01-15T10:30:00Z",
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.timestamp.year == 2025
        assert event.timestamp.month == 1
        assert event.timestamp.day == 15
        assert event.timestamp.hour == 10
        assert event.timestamp.minute == 30

    def test_timestamp_parsing_iso_with_offset(self, adapter: QwenAdapter) -> None:
        """Parses ISO timestamp with timezone offset."""
        native_event = {
            "hook_type": "SessionStart",
            "input_data": {
                "session_id": "sess-time",
                "timestamp": "2025-01-15T15:30:00+05:00",
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.timestamp.year == 2025
        assert event.timestamp.hour == 15

    def test_timestamp_missing_uses_current_time(self, adapter: QwenAdapter) -> None:
        """Missing timestamp uses current time."""
        native_event = {
            "hook_type": "SessionStart",
            "input_data": {
                "session_id": "sess-no-time",
            },
        }

        before = datetime.now(UTC)
        event = adapter.translate_to_hook_event(native_event)
        after = datetime.now(UTC)

        assert before <= event.timestamp <= after

    def test_timestamp_invalid_uses_current_time(self, adapter: QwenAdapter) -> None:
        """Invalid timestamp format uses current time."""
        native_event = {
            "hook_type": "SessionStart",
            "input_data": {
                "session_id": "sess-bad-time",
                "timestamp": "not-a-valid-timestamp",
            },
        }

        before = datetime.now(UTC)
        event = adapter.translate_to_hook_event(native_event)
        after = datetime.now(UTC)

        assert before <= event.timestamp <= after

    def test_machine_id_from_payload(self, adapter: QwenAdapter) -> None:
        """Uses machine_id from payload if provided."""
        native_event = {
            "hook_type": "SessionStart",
            "input_data": {
                "session_id": "sess-machine",
                "machine_id": "provided-machine-id",
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.machine_id == "provided-machine-id"

    def test_machine_id_none_when_missing(self, adapter: QwenAdapter) -> None:
        """Returns None for machine_id when not in payload (base adapter injects later)."""
        native_event = {
            "hook_type": "SessionStart",
            "input_data": {
                "session_id": "sess-no-machine",
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        # machine_id is None at translation time; base adapter's handle_native() injects it
        assert event.machine_id is None

    def test_empty_session_id(self, adapter: QwenAdapter) -> None:
        """Handles empty session_id."""
        native_event = {
            "hook_type": "SessionStart",
            "input_data": {},
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.session_id == ""

    def test_cwd_extracted_from_input_data(self, adapter: QwenAdapter) -> None:
        """Extracts cwd from input_data."""
        native_event = {
            "hook_type": "SessionStart",
            "input_data": {
                "session_id": "sess-cwd",
                "cwd": "/path/to/project",
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.cwd == "/path/to/project"

    def test_cwd_none_when_missing(self, adapter: QwenAdapter) -> None:
        """cwd is None when not in payload."""
        native_event = {
            "hook_type": "SessionStart",
            "input_data": {
                "session_id": "sess-no-cwd",
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.cwd is None

    def test_no_metadata_when_no_tool_name(self, adapter: QwenAdapter) -> None:
        """Metadata is empty when no tool_name in event."""
        native_event = {
            "hook_type": "SessionStart",
            "input_data": {
                "session_id": "sess-no-tool",
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.metadata == {"_native_hook_type": "SessionStart"}


class TestTranslateFromHookResponse:
    """Tests for translate_from_hook_response() method."""

    def test_allow_decision(self, adapter: QwenAdapter) -> None:
        """Translates allow decision."""
        response = HookResponse(decision="allow")

        result = adapter.translate_from_hook_response(response)

        assert result["decision"] == "allow"
        assert result["continue"] is True
        assert "reason" not in result
        assert "hookSpecificOutput" not in result

    def test_deny_decision_with_reason(self, adapter: QwenAdapter) -> None:
        """Unknown/no-hook-type deny decisions remain hard stops."""
        response = HookResponse(decision="deny", reason="Policy violation")

        result = adapter.translate_from_hook_response(response)

        assert result["decision"] == "deny"
        assert result["continue"] is False
        assert result["reason"] == "Policy violation"

    def test_block_decision(self, adapter: QwenAdapter) -> None:
        """Unknown/no-hook-type block decisions are mapped to deny (hard stops)."""
        response = HookResponse(decision="block", reason="Blocked by workflow")

        result = adapter.translate_from_hook_response(response)

        assert result["decision"] == "deny"
        assert result["continue"] is False
        assert result["reason"] == "Blocked by workflow"

    def test_before_tool_block_decision_is_recoverable(self, adapter: QwenAdapter) -> None:
        """ACP adapter keeps the turn alive on a BeforeTool block (recoverable)."""
        reason = (
            "Rule enforced by Gobby: [require-code-index-skill]\n"
            'Call get_skill(name="code-index") on gobby-skills directly through '
            "mcp__gobby__call_tool"
        )
        response = HookResponse(decision="block", reason=reason)

        result = adapter.translate_from_hook_response(response, hook_type="BeforeTool")

        assert result == {
            "decision": "deny",
            "continue": True,
            "reason": reason,
        }

    def test_after_tool_deny_decision_is_recoverable(self, adapter: QwenAdapter) -> None:
        """ACP adapter keeps the turn alive on an AfterTool deny (recoverable)."""
        response = HookResponse(decision="deny", reason="Post-tool gate failed")

        result = adapter.translate_from_hook_response(response, hook_type="AfterTool")

        assert result["decision"] == "deny"
        assert result["continue"] is True
        assert result["reason"] == "Post-tool gate failed"

    def test_unknown_hook_block_decision_hard_stops(self, adapter: QwenAdapter) -> None:
        """Denied responses for unsupported hooks keep the hard-stop fallback."""
        response = HookResponse(decision="block", reason="Unsupported hook")

        result = adapter.translate_from_hook_response(response, hook_type="FutureHook")

        assert result["decision"] == "deny"
        assert result["continue"] is False
        assert result["reason"] == "Unsupported hook"

    def test_context_injection(self, adapter: QwenAdapter) -> None:
        """Translates context to hookSpecificOutput.additionalContext."""
        response = HookResponse(
            decision="allow",
            context="Remember to follow coding standards.",
        )

        result = adapter.translate_from_hook_response(response)

        assert result["decision"] == "allow"
        assert result["hookSpecificOutput"]["additionalContext"] == (
            "Remember to follow coding standards."
        )

    def test_system_message(self, adapter: QwenAdapter) -> None:
        """Translates system_message to systemMessage."""
        response = HookResponse(
            decision="allow",
            system_message="Session handoff in progress",
        )

        result = adapter.translate_from_hook_response(response)

        assert result["systemMessage"] == "Session handoff in progress"

    def test_session_start_routes_banner_to_additional_context_only(
        self, adapter: QwenAdapter
    ) -> None:
        """SessionStart keeps the startup banner in additionalContext only."""
        banner = "Gobby Session ID: #42 (uuid-123)"
        response = HookResponse(decision="allow", system_message=banner)

        result = adapter.translate_from_hook_response(response, hook_type="SessionStart")

        assert "systemMessage" not in result
        assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert result["hookSpecificOutput"]["additionalContext"].count(banner) == 1

    def test_session_start_live_context_does_not_replay_persona(self, adapter: QwenAdapter) -> None:
        """Live SessionStart context reaches ACP provider without startup persona replay."""
        response = HookResponse(
            decision="allow",
            system_message="Gobby Session ID: #6273 (sess-live-123)",
            context="Claimed task refs: #15237 [in_progress]",
        )

        result = adapter.translate_from_hook_response(response, hook_type="SessionStart")

        assert "systemMessage" not in result
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Gobby Session ID: #6273 (sess-live-123)" in ctx
        assert "Claimed task refs: #15237 [in_progress]" in ctx
        assert "## Role" not in ctx
        assert "## Personality" not in ctx

    def test_session_start_normalizes_snake_case_hook_name(self, adapter: QwenAdapter) -> None:
        """session_start should format like SessionStart for response routing."""
        banner = "Gobby Session ID: #42 (uuid-123)"
        response = HookResponse(decision="allow", system_message=banner)

        result = adapter.translate_from_hook_response(response, hook_type="session_start")

        assert "systemMessage" not in result
        assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert result["hookSpecificOutput"]["additionalContext"].count(banner) == 1

    def test_session_start_banner_and_metadata_include_session_id_once(
        self, adapter: QwenAdapter
    ) -> None:
        """SessionStart does not duplicate the session ID between banner and metadata."""
        banner = "Gobby Session ID: #42 (uuid-123)"
        response = HookResponse(
            decision="allow",
            system_message=banner,
            metadata={
                "session_id": "uuid-123",
                "session_ref": "#42",
                "external_id": "ext-id-456",
                "_first_hook_for_session": True,
                "project_id": "proj-xyz",
            },
        )

        result = adapter.translate_from_hook_response(response, hook_type="SessionStart")

        assert "systemMessage" not in result
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert ctx.count(banner) == 1
        assert "ext-id-456" in ctx
        assert "proj-xyz" in ctx

    def test_before_model_modify_args(self, adapter: QwenAdapter) -> None:
        """Translates modify_args for BeforeModel hook."""
        response = HookResponse(
            decision="allow",
            modify_args={"temperature": 0.5, "max_tokens": 1000},
        )

        result = adapter.translate_from_hook_response(response, hook_type="BeforeModel")

        assert result["hookSpecificOutput"]["llm_request"] == {
            "temperature": 0.5,
            "max_tokens": 1000,
        }

    def test_before_tool_selection_modify_args(self, adapter: QwenAdapter) -> None:
        """Translates modify_args for BeforeToolSelection hook."""
        response = HookResponse(
            decision="allow",
            modify_args={"allowed_tools": ["read_file", "write_file"]},
        )

        result = adapter.translate_from_hook_response(response, hook_type="BeforeToolSelection")

        assert result["hookSpecificOutput"]["toolConfig"] == {
            "allowed_tools": ["read_file", "write_file"]
        }

    def test_modify_args_ignored_for_other_hooks(self, adapter: QwenAdapter) -> None:
        """modify_args is ignored for non-BeforeModel/BeforeToolSelection hooks."""
        response = HookResponse(
            decision="allow",
            modify_args={"some_arg": "value"},
        )

        result = adapter.translate_from_hook_response(response, hook_type="SessionStart")

        assert "hookSpecificOutput" not in result

    def test_no_hook_specific_output_when_empty(self, adapter: QwenAdapter) -> None:
        """hookSpecificOutput is not included when empty."""
        response = HookResponse(decision="allow")

        result = adapter.translate_from_hook_response(response)

        assert "hookSpecificOutput" not in result

    def test_combined_context_and_modify_args(self, adapter: QwenAdapter) -> None:
        """Routes BeforeModel modify_args and drops unsupported context."""
        response = HookResponse(
            decision="allow",
            context="Use JSON format",
            modify_args={"temperature": 0.7},
        )

        result = adapter.translate_from_hook_response(response, hook_type="BeforeModel")

        assert result["hookSpecificOutput"]["llm_request"]["temperature"] == 0.7
        assert "additionalContext" not in result["hookSpecificOutput"]

    def test_all_fields_combined(self, adapter: QwenAdapter) -> None:
        """Translates response with all fields populated."""
        response = HookResponse(
            decision="allow",
            context="Context text",
            system_message="System message",
            reason="Some reason",
            modify_args={"key": "value"},
        )

        result = adapter.translate_from_hook_response(response, hook_type="BeforeModel")

        assert result["decision"] == "allow"
        assert result["reason"] == "Some reason"
        assert result["systemMessage"] == "System message"
        assert result["hookSpecificOutput"]["llm_request"] == {"key": "value"}
        assert "additionalContext" not in result["hookSpecificOutput"]

    def test_none_hook_type(self, adapter: QwenAdapter) -> None:
        """Handles None hook_type gracefully."""
        response = HookResponse(
            decision="allow",
            modify_args={"key": "value"},
        )

        result = adapter.translate_from_hook_response(response, hook_type=None)

        # modify_args should be ignored without proper hook_type
        assert "hookSpecificOutput" not in result

    def test_no_metadata_on_subsequent_hooks(self, adapter: QwenAdapter) -> None:
        """Subsequent hooks do not inject session ref."""
        response = HookResponse(
            decision="allow",
            metadata={
                "session_id": "uuid-123",
                "session_ref": "#42",
                "_first_hook_for_session": False,
            },
        )
        result = adapter.translate_from_hook_response(response, hook_type="BeforeTool")
        assert "hookSpecificOutput" not in result


class TestBlockToDenyMapping:
    """Verify that ``block`` decisions are mapped to ``deny`` for ACP providers.

    Grok's hook runner only accepts ``"allow"`` and ``"deny"``; emitting
    ``"block"`` causes a fail-open error that silently disables rule enforcement.
    """

    def test_block_maps_to_deny_recoverable(self) -> None:
        """Block on a tool hook maps to deny with continue=True (recoverable)."""
        adapter = GrokAdapter()
        response = HookResponse(decision="block", reason="Rule enforced")
        result = adapter.translate_from_hook_response(response, hook_type="pre_tool_use")
        assert result["decision"] == "deny"
        assert result["continue"] is True
        assert result["reason"] == "Rule enforced"

    def test_block_maps_to_deny_hard_stop(self) -> None:
        """Only Grok Stop maps a top-level block to recoverable feedback."""
        adapter = GrokAdapter()
        response = HookResponse(decision="block", reason="Hard stop gate")
        result = adapter.translate_from_hook_response(response, hook_type="stop")
        assert result["decision"] == "block"
        assert result["continue"] is True
        assert result["reason"] == "Hard stop gate"


class TestGrokCurrentHookContract:
    """Grok 1.0.0 hook payloads retain native data and canonical aliases."""

    def test_permission_denied_preserves_common_and_tool_fields(self) -> None:
        payload = {
            "sessionId": "grok-session",
            "workspaceRoot": "/tmp/project",
            "transcriptPath": "/tmp/transcript.jsonl",
            "clientIdentifier": "grok-build",
            "promptId": "prompt-7",
            "permissionMode": "default",
            "toolName": "Bash",
            "toolUseId": "tool-7",
            "toolInput": {"command": "rm file"},
            "toolInputTruncated": True,
            "toolResult": "denied",
            "toolResultTruncated": False,
        }

        event = GrokAdapter().translate_to_hook_event(
            {"source": "grok", "hook_type": "PermissionDenied", "input_data": payload}
        )

        assert event.event_type == HookEventType.PERMISSION_DENIED
        assert event.session_id == "grok-session"
        assert event.data["toolInput"] == {"command": "rm file"}
        assert event.data["tool_input"]["command"] == "rm file"
        assert event.data["tool_input_truncated"] is True
        assert event.data["tool_output"] == "denied"
        assert event.data["workspace_root"] == "/tmp/project"
        assert event.data["transcript_path"] == "/tmp/transcript.jsonl"
        assert event.data["prompt_id"] == "prompt-7"
        assert event.data["permission_mode"] == "default"

    def test_stop_failure_preserves_error_and_stop_state(self) -> None:
        event = GrokAdapter().translate_to_hook_event(
            {
                "hook_type": "StopFailure",
                "input_data": {
                    "sessionId": "grok-session",
                    "errorDetails": {"code": "rate_limit", "retryable": True},
                    "lastAssistantMessage": "Working",
                    "phase": "streaming",
                    "stopHookActive": True,
                },
            }
        )

        assert event.event_type == HookEventType.STOP_FAILURE
        assert event.data["error_details"] == {"code": "rate_limit", "retryable": True}
        assert event.data["last_assistant_message"] == "Working"
        assert event.data["phase"] == "streaming"
        assert event.data["stop_hook_active"] is True

    @pytest.mark.parametrize(
        ("hook_type", "event_type"),
        [
            ("SubagentStart", HookEventType.SUBAGENT_START),
            ("SubagentStop", HookEventType.SUBAGENT_STOP),
            ("SubagentEnd", HookEventType.SUBAGENT_STOP),
        ],
    )
    def test_subagent_hooks_expose_both_identifier_vocabularies(
        self, hook_type: str, event_type: HookEventType
    ) -> None:
        event = GrokAdapter().translate_to_hook_event(
            {
                "hook_type": hook_type,
                "input_data": {
                    "sessionId": "grok-session",
                    "subagentId": "agent-9",
                    "subagentType": "Explore",
                    "promptId": "prompt-9",
                },
            }
        )

        assert event.event_type == event_type
        assert event.metadata["_native_hook_type"] == hook_type
        assert event.data["subagent_id"] == "agent-9"
        assert event.data["agent_id"] == "agent-9"
        assert event.data["subagent_type"] == "Explore"
        assert event.data["agent_type"] == "Explore"
        assert event.data["prompt_id"] == "prompt-9"

    @pytest.mark.parametrize("hook_type", ["PermissionDenied", "StopFailure", "SubagentStart"])
    def test_observe_only_hooks_always_continue_neutrally(self, hook_type: str) -> None:
        result = GrokAdapter().translate_from_hook_response(
            HookResponse(decision="block", reason="Workflow still ran"),
            hook_type=hook_type,
        )

        assert result == {"decision": "allow", "continue": True}

    def test_subagent_stop_block_is_recoverable_with_feedback(self) -> None:
        result = GrokAdapter().translate_from_hook_response(
            HookResponse(decision="block", reason="Finish the task", context="Resolve tests"),
            hook_type="SubagentEnd",
        )

        assert result == {
            "continue": True,
            "decision": "block",
            "reason": "Finish the task",
            "hookSpecificOutput": {
                "hookEventName": "SubagentStop",
                "additionalContext": "Resolve tests",
            },
        }

    def test_stop_block_is_recoverable_with_additional_context(self) -> None:
        result = GrokAdapter().translate_from_hook_response(
            HookResponse(decision="block", reason="Keep working", context="Run tests"),
            hook_type="stop",
        )

        assert result == {
            "continue": True,
            "decision": "block",
            "reason": "Keep working",
            "hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": "Run tests",
            },
        }

    @pytest.mark.parametrize("hook_type", ["user_prompt_submit", "post_tool_use"])
    def test_observe_hooks_ignore_response_context(self, hook_type: str) -> None:
        result = GrokAdapter().translate_from_hook_response(
            HookResponse(decision="allow", context="ignored"), hook_type=hook_type
        )

        assert "hookSpecificOutput" not in result

    def test_allowing_stop_has_no_force_stop_fields(self) -> None:
        result = GrokAdapter().translate_from_hook_response(
            HookResponse(decision="allow"), hook_type="stop"
        )

        assert result.get("decision") == "allow"
        assert result.get("continue") is True
        assert "stopReason" not in result

    def test_pre_tool_use_omits_context_fields(self) -> None:
        result = GrokAdapter().translate_from_hook_response(
            HookResponse(
                decision="allow",
                context="ignored",
                modified_input={"command": "git status"},
            ),
            hook_type="pre_tool_use",
        )

        assert result["hookSpecificOutput"] == {
            "hookEventName": "pre_tool_use",
            "updatedInput": {"command": "git status"},
        }
        assert "additionalContext" not in result
        assert "systemMessage" not in result

    def test_subagent_stop_block_has_non_empty_fallback_reason(self) -> None:
        result = GrokAdapter().translate_from_hook_response(
            HookResponse(decision="block"), hook_type="subagent_stop"
        )

        assert result["continue"] is True
        assert result["decision"] == "block"
        assert result["reason"]

    @pytest.mark.parametrize(
        "hook_type",
        [
            "session_start",
            "user_prompt_submit",
            "post_tool_use",
            "post_tool_use_failure",
            "pre_compact",
            "post_compact",
            "notification",
            "permission_denied",
            "stop_failure",
            "subagent_start",
            "session_end",
        ],
    )
    def test_passive_hooks_ignore_block_responses(self, hook_type: str) -> None:
        result = GrokAdapter().translate_from_hook_response(
            HookResponse(decision="block", reason="ignored", context="ignored"),
            hook_type=hook_type,
        )

        assert result == {"decision": "allow", "continue": True}

    def test_pre_tool_use_deny_omits_context_and_updated_input(self) -> None:
        result = GrokAdapter().translate_from_hook_response(
            HookResponse(
                decision="deny",
                reason="Blocked by policy",
                context="ignored",
                modified_input={"command": "rm -rf build"},
            ),
            hook_type="pre_tool_use",
        )

        assert result["decision"] == "deny"
        assert result["reason"] == "Blocked by policy"
        assert "additionalContext" not in result
        assert "systemMessage" not in result
        assert "updatedInput" not in result.get("hookSpecificOutput", {})

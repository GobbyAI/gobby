"""Tests for Gemini CLI adapter.

Tests cover:
- Event type mapping (Gemini PascalCase -> unified HookEventType)
- Tool name normalization (Gemini tool names -> standard names)
- translate_to_hook_event() for various event types
- translate_from_hook_response() for various response configurations
- handle_native() integration
- Machine ID generation/caching
- Error handling and edge cases
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from gobby.adapters.gemini import GeminiAdapter
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource

pytestmark = pytest.mark.unit


class TestGeminiAdapterInit:
    """Tests for GeminiAdapter initialization."""

    def test_init_without_hook_manager(self) -> None:
        """GeminiAdapter initializes without hook_manager."""
        adapter = GeminiAdapter()
        assert adapter._hook_manager is None

    def test_init_with_hook_manager(self) -> None:
        """GeminiAdapter stores hook_manager reference."""
        mock_manager = MagicMock()
        adapter = GeminiAdapter(hook_manager=mock_manager)
        assert adapter._hook_manager is mock_manager

    def test_source_is_gemini(self) -> None:
        """GeminiAdapter reports GEMINI as source."""
        adapter = GeminiAdapter()
        assert adapter.source == SessionSource.GEMINI


class TestEventTypeMapping:
    """Tests for Gemini event type mapping."""

    @pytest.fixture
    def adapter(self):
        """Create a GeminiAdapter instance."""
        return GeminiAdapter()

    @pytest.mark.parametrize(
        "gemini_type,expected_type",
        [
            ("SessionStart", HookEventType.SESSION_START),
            ("SessionEnd", HookEventType.SESSION_END),
            ("BeforeAgent", HookEventType.BEFORE_AGENT),
            ("AfterAgent", HookEventType.AFTER_AGENT),
            ("BeforeTool", HookEventType.BEFORE_TOOL),
            ("AfterTool", HookEventType.AFTER_TOOL),
            ("BeforeToolSelection", HookEventType.BEFORE_TOOL_SELECTION),
            ("BeforeModel", HookEventType.BEFORE_MODEL),
            ("AfterModel", HookEventType.AFTER_MODEL),
            ("PreCompress", HookEventType.PRE_COMPACT),
            ("Notification", HookEventType.NOTIFICATION),
        ],
    )
    def test_event_map_coverage(self, adapter, gemini_type, expected_type) -> None:
        """EVENT_MAP maps all Gemini hook types correctly."""
        assert adapter.EVENT_MAP[gemini_type] == expected_type

    def test_event_map_has_all_gemini_types(self, adapter) -> None:
        """EVENT_MAP contains exactly 11 Gemini hook types."""
        assert len(adapter.EVENT_MAP) == 11

    @pytest.mark.parametrize(
        "event_type_value,expected_gemini_name",
        [
            ("session_start", "SessionStart"),
            ("session_end", "SessionEnd"),
            ("before_agent", "BeforeAgent"),
            ("after_agent", "AfterAgent"),
            ("before_tool", "BeforeTool"),
            ("after_tool", "AfterTool"),
            ("before_tool_selection", "BeforeToolSelection"),
            ("before_model", "BeforeModel"),
            ("after_model", "AfterModel"),
            ("pre_compact", "PreCompress"),
            ("notification", "Notification"),
        ],
    )
    def test_hook_event_name_map_coverage(
        self, adapter, event_type_value, expected_gemini_name
    ) -> None:
        """HOOK_EVENT_NAME_MAP reverse maps all event types correctly."""
        assert adapter.HOOK_EVENT_NAME_MAP[event_type_value] == expected_gemini_name


@pytest.mark.unit
class TestToolNameNormalization:
    """Tests for Gemini tool name normalization."""

    @pytest.fixture
    def adapter(self):
        """Create a GeminiAdapter instance."""
        return GeminiAdapter()

    @pytest.mark.parametrize(
        "gemini_tool,expected_tool",
        [
            # Shell/Bash
            ("run_shell_command", "Bash"),
            ("RunShellCommand", "Bash"),
            ("ShellTool", "Bash"),
            # File read
            ("read_file", "Read"),
            ("ReadFile", "Read"),
            ("ReadFileTool", "Read"),
            # File write
            ("write_file", "Write"),
            ("WriteFile", "Write"),
            ("WriteFileTool", "Write"),
            # File edit
            ("edit_file", "Edit"),
            ("EditFile", "Edit"),
            ("EditFileTool", "Edit"),
            # Search/Glob/Grep
            ("GlobTool", "Glob"),
            ("GrepTool", "Grep"),
            ("search_file_content", "Grep"),
            ("SearchText", "Grep"),
            # MCP tools (Gobby MCP server)
            ("call_tool", "mcp__gobby__call_tool"),
            ("list_mcp_servers", "mcp__gobby__list_mcp_servers"),
            ("list_tools", "mcp__gobby__list_tools"),
            ("get_tool_schema", "mcp__gobby__get_tool_schema"),
            ("search_tools", "mcp__gobby__search_tools"),
            ("recommend_tools", "mcp__gobby__recommend_tools"),
            # Skill and agent tools
            ("activate_skill", "Skill"),
            ("delegate_to_agent", "Task"),
        ],
    )
    def test_tool_map_coverage(self, adapter, gemini_tool, expected_tool) -> None:
        """TOOL_MAP normalizes all known Gemini tool names."""
        assert adapter.normalize_tool_name(gemini_tool) == expected_tool

    def test_unknown_tool_passes_through(self, adapter) -> None:
        """Unknown tool names pass through unchanged."""
        assert adapter.normalize_tool_name("CustomTool") == "CustomTool"
        assert adapter.normalize_tool_name("mcp_server_tool") == "mcp_server_tool"

    def test_empty_tool_name(self, adapter) -> None:
        """Empty tool name passes through unchanged."""
        assert adapter.normalize_tool_name("") == ""


class TestTranslateToHookEvent:
    """Tests for translate_to_hook_event() method."""

    @pytest.fixture
    def adapter(self):
        """Create a GeminiAdapter instance."""
        return GeminiAdapter()

    def test_session_start_with_dispatcher_wrapper(self, adapter) -> None:
        """Translates SessionStart event with dispatcher wrapper format."""
        native_event = {
            "source": "gemini",
            "hook_type": "SessionStart",
            "input_data": {
                "hook_event_name": "SessionStart",
                "session_id": "gemini-sess-123",
                "cwd": "/home/user/project",
                "timestamp": "2025-01-15T10:30:00Z",
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.event_type == HookEventType.SESSION_START
        assert event.session_id == "gemini-sess-123"
        assert event.source == SessionSource.GEMINI
        assert event.cwd == "/home/user/project"
        assert event.data == native_event["input_data"]

    def test_session_start_without_wrapper(self, adapter) -> None:
        """Translates SessionStart event without dispatcher wrapper."""
        native_event = {
            "hook_event_name": "SessionStart",
            "session_id": "gemini-sess-456",
            "cwd": "/tmp/project",
            "timestamp": "2025-01-15T11:00:00+00:00",
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.event_type == HookEventType.SESSION_START
        assert event.session_id == "gemini-sess-456"
        assert event.cwd == "/tmp/project"

    def test_before_tool_with_tool_name(self, adapter) -> None:
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

    def test_after_tool_with_tool_name(self, adapter) -> None:
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

    def test_before_model_event(self, adapter) -> None:
        """Translates BeforeModel event (Gemini-specific)."""
        native_event = {
            "hook_type": "BeforeModel",
            "input_data": {
                "hook_event_name": "BeforeModel",
                "session_id": "sess-model",
                "model": "gemini-2.0-flash-exp",
                "prompt": "Hello, world!",
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.event_type == HookEventType.BEFORE_MODEL
        assert event.data["model"] == "gemini-2.0-flash-exp"

    def test_after_model_event(self, adapter) -> None:
        """Translates AfterModel event (Gemini-specific)."""
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

    def test_before_tool_selection_event(self, adapter) -> None:
        """Translates BeforeToolSelection event (Gemini-specific)."""
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

    def test_pre_compress_event(self, adapter) -> None:
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

    def test_notification_event(self, adapter) -> None:
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

    def test_unknown_event_type_defaults_to_notification(self, adapter) -> None:
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

    def test_timestamp_parsing_iso_with_z(self, adapter) -> None:
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

    def test_timestamp_parsing_iso_with_offset(self, adapter) -> None:
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

    def test_timestamp_missing_uses_current_time(self, adapter) -> None:
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

    def test_timestamp_invalid_uses_current_time(self, adapter) -> None:
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

    def test_machine_id_from_payload(self, adapter) -> None:
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

    def test_machine_id_none_when_missing(self, adapter) -> None:
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

    def test_empty_session_id(self, adapter) -> None:
        """Handles empty session_id."""
        native_event = {
            "hook_type": "SessionStart",
            "input_data": {},
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.session_id == ""

    def test_cwd_extracted_from_input_data(self, adapter) -> None:
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

    def test_cwd_none_when_missing(self, adapter) -> None:
        """cwd is None when not in payload."""
        native_event = {
            "hook_type": "SessionStart",
            "input_data": {
                "session_id": "sess-no-cwd",
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.cwd is None

    def test_no_metadata_when_no_tool_name(self, adapter) -> None:
        """Metadata is empty when no tool_name in event."""
        native_event = {
            "hook_type": "SessionStart",
            "input_data": {
                "session_id": "sess-no-tool",
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.metadata == {}


class TestTranslateFromHookResponse:
    """Tests for translate_from_hook_response() method."""

    @pytest.fixture
    def adapter(self):
        """Create a GeminiAdapter instance."""
        return GeminiAdapter()

    def test_allow_decision(self, adapter) -> None:
        """Translates allow decision."""
        response = HookResponse(decision="allow")

        result = adapter.translate_from_hook_response(response)

        assert result["decision"] == "allow"
        assert "reason" not in result
        assert "hookSpecificOutput" not in result

    def test_deny_decision_with_reason(self, adapter) -> None:
        """Translates deny decision with reason."""
        response = HookResponse(decision="deny", reason="Policy violation")

        result = adapter.translate_from_hook_response(response)

        assert result["decision"] == "deny"
        assert result["reason"] == "Policy violation"

    def test_block_decision(self, adapter) -> None:
        """Translates block decision."""
        response = HookResponse(decision="block", reason="Blocked by workflow")

        result = adapter.translate_from_hook_response(response)

        assert result["decision"] == "block"
        assert result["reason"] == "Blocked by workflow"

    def test_context_injection(self, adapter) -> None:
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

    def test_system_message(self, adapter) -> None:
        """Translates system_message to systemMessage."""
        response = HookResponse(
            decision="allow",
            system_message="Session handoff in progress",
        )

        result = adapter.translate_from_hook_response(response)

        assert result["systemMessage"] == "Session handoff in progress"

    def test_before_model_modify_args(self, adapter) -> None:
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

    def test_before_tool_selection_modify_args(self, adapter) -> None:
        """Translates modify_args for BeforeToolSelection hook."""
        response = HookResponse(
            decision="allow",
            modify_args={"allowed_tools": ["read_file", "write_file"]},
        )

        result = adapter.translate_from_hook_response(response, hook_type="BeforeToolSelection")

        assert result["hookSpecificOutput"]["toolConfig"] == {
            "allowed_tools": ["read_file", "write_file"]
        }

    def test_modify_args_ignored_for_other_hooks(self, adapter) -> None:
        """modify_args is ignored for non-BeforeModel/BeforeToolSelection hooks."""
        response = HookResponse(
            decision="allow",
            modify_args={"some_arg": "value"},
        )

        result = adapter.translate_from_hook_response(response, hook_type="SessionStart")

        assert "hookSpecificOutput" not in result

    def test_no_hook_specific_output_when_empty(self, adapter) -> None:
        """hookSpecificOutput is not included when empty."""
        response = HookResponse(decision="allow")

        result = adapter.translate_from_hook_response(response)

        assert "hookSpecificOutput" not in result

    def test_combined_context_and_modify_args(self, adapter) -> None:
        """Translates both context and modify_args together."""
        response = HookResponse(
            decision="allow",
            context="Use JSON format",
            modify_args={"temperature": 0.7},
        )

        result = adapter.translate_from_hook_response(response, hook_type="BeforeModel")

        assert result["hookSpecificOutput"]["additionalContext"] == "Use JSON format"
        assert result["hookSpecificOutput"]["llm_request"]["temperature"] == 0.7

    def test_all_fields_combined(self, adapter) -> None:
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
        assert result["hookSpecificOutput"]["additionalContext"] == "Context text"
        assert result["hookSpecificOutput"]["llm_request"] == {"key": "value"}

    def test_none_hook_type(self, adapter) -> None:
        """Handles None hook_type gracefully."""
        response = HookResponse(
            decision="allow",
            modify_args={"key": "value"},
        )

        result = adapter.translate_from_hook_response(response, hook_type=None)

        # modify_args should be ignored without proper hook_type
        assert "hookSpecificOutput" not in result

    def test_no_metadata_on_subsequent_hooks(self, adapter) -> None:
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


class TestHandleNative:
    """Tests for handle_native() method."""

    @pytest.fixture
    def adapter(self):
        """Create a GeminiAdapter instance."""
        return GeminiAdapter()

    @pytest.fixture
    def mock_hook_manager(self):
        """Create a mock HookManager."""
        manager = MagicMock()
        manager.handle.return_value = HookResponse(decision="allow")
        return manager

    def test_handle_native_translates_and_processes(self, adapter, mock_hook_manager) -> None:
        """handle_native() translates event, processes, and returns response."""
        native_event = {
            "hook_type": "SessionStart",
            "input_data": {
                "session_id": "sess-handle",
                "cwd": "/project",
            },
        }

        result = adapter.handle_native(native_event, mock_hook_manager)

        # Verify HookManager.handle was called with HookEvent
        mock_hook_manager.handle.assert_called_once()
        call_args = mock_hook_manager.handle.call_args[0]
        assert isinstance(call_args[0], HookEvent)
        assert call_args[0].event_type == HookEventType.SESSION_START

        # Verify response format
        assert result["decision"] == "allow"

    def test_handle_native_preserves_hook_type_for_response(
        self, adapter, mock_hook_manager
    ) -> None:
        """handle_native() uses original hook_type for response formatting."""
        mock_hook_manager.handle.return_value = HookResponse(
            decision="allow",
            modify_args={"temperature": 0.5},
        )

        native_event = {
            "hook_type": "BeforeModel",
            "input_data": {
                "session_id": "sess-model",
            },
        }

        result = adapter.handle_native(native_event, mock_hook_manager)

        # BeforeModel-specific formatting should apply
        assert result["hookSpecificOutput"]["llm_request"]["temperature"] == 0.5

    def test_handle_native_extracts_hook_type_from_input_data(
        self, adapter, mock_hook_manager
    ) -> None:
        """handle_native() extracts hook_type from input_data if not in wrapper."""
        mock_hook_manager.handle.return_value = HookResponse(
            decision="allow",
            modify_args={"tool_filter": ["read"]},
        )

        native_event = {
            "input_data": {
                "hook_event_name": "BeforeToolSelection",
                "session_id": "sess-tools",
            },
        }

        result = adapter.handle_native(native_event, mock_hook_manager)

        # BeforeToolSelection-specific formatting should apply
        assert result["hookSpecificOutput"]["toolConfig"]["tool_filter"] == ["read"]

    def test_handle_native_deny_response(self, adapter, mock_hook_manager) -> None:
        """handle_native() correctly formats deny responses."""
        mock_hook_manager.handle.return_value = HookResponse(
            decision="deny",
            reason="Task not claimed",
        )

        native_event = {
            "hook_type": "BeforeTool",
            "input_data": {
                "session_id": "sess-deny",
                "tool_name": "WriteFileTool",
            },
        }

        result = adapter.handle_native(native_event, mock_hook_manager)

        assert result["decision"] == "deny"
        assert result["reason"] == "Task not claimed"

    def test_handle_native_with_context_injection(self, adapter, mock_hook_manager) -> None:
        """handle_native() includes context injection in response."""
        mock_hook_manager.handle.return_value = HookResponse(
            decision="allow",
            context="## Continuation Context\nPrevious session ended at step 5.",
        )

        native_event = {
            "hook_type": "SessionStart",
            "input_data": {
                "session_id": "sess-context",
            },
        }

        result = adapter.handle_native(native_event, mock_hook_manager)

        assert "hookSpecificOutput" in result
        assert "## Continuation Context" in result["hookSpecificOutput"]["additionalContext"]

    def test_handle_native_empty_hook_type(self, adapter, mock_hook_manager) -> None:
        """handle_native() handles empty hook_type gracefully."""
        native_event = {
            "hook_type": "",
            "input_data": {
                "session_id": "sess-empty",
            },
        }

        result = adapter.handle_native(native_event, mock_hook_manager)

        # Should still process and return a response
        assert result["decision"] == "allow"


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    @pytest.fixture
    def adapter(self):
        """Create a GeminiAdapter instance."""
        return GeminiAdapter()

    def test_translate_empty_event(self, adapter) -> None:
        """Handles empty event gracefully."""
        native_event = {}

        event = adapter.translate_to_hook_event(native_event)

        assert event.event_type == HookEventType.NOTIFICATION  # Default
        assert event.session_id == ""
        assert event.data == {}

    def test_translate_none_values_in_event(self, adapter) -> None:
        """Handles None values in event data."""
        native_event = {
            "hook_type": "SessionStart",
            "input_data": {
                "session_id": None,
                "cwd": None,
                "timestamp": None,
            },
        }

        # Should not raise
        event = adapter.translate_to_hook_event(native_event)

        # Translation succeeded - event was created
        assert event is not None
        # None session_id becomes empty string via .get() default
        # This test documents current behavior - session_id would be None
        # since dict.get returns None for existing key with None value

    def test_translate_nested_data_preserved(self, adapter) -> None:
        """Complex nested data in input_data is preserved."""
        nested_data = {
            "tool_input": {
                "nested": {
                    "deeply": {
                        "value": 42,
                    },
                },
            },
        }
        native_event = {
            "hook_type": "BeforeTool",
            "input_data": {
                "session_id": "sess-nested",
                "tool_name": "custom_tool",
                **nested_data,
            },
        }

        event = adapter.translate_to_hook_event(native_event)

        assert event.data["tool_input"]["nested"]["deeply"]["value"] == 42

    def test_response_with_empty_reason(self, adapter) -> None:
        """Empty reason string is not included in response."""
        response = HookResponse(decision="allow", reason="")

        result = adapter.translate_from_hook_response(response)

        # Empty string is falsy, so reason should not be included
        assert "reason" not in result

    def test_response_with_empty_context(self, adapter) -> None:
        """Empty context string does not create hookSpecificOutput."""
        response = HookResponse(decision="allow", context="")

        result = adapter.translate_from_hook_response(response)

        assert "hookSpecificOutput" not in result

    def test_response_with_empty_system_message(self, adapter) -> None:
        """Empty system_message is not included in response."""
        response = HookResponse(decision="allow", system_message="")

        result = adapter.translate_from_hook_response(response)

        assert "systemMessage" not in result

    def test_timestamp_with_none_replace_attribute(self, adapter) -> None:
        """Handles timestamp that can't be processed (non-string)."""
        native_event = {
            "hook_type": "SessionStart",
            "input_data": {
                "session_id": "sess-bad-ts",
                "timestamp": 12345,  # Not a string
            },
        }

        before = datetime.now(UTC)
        event = adapter.translate_to_hook_event(native_event)
        after = datetime.now(UTC)

        # Should fall back to current time
        assert before <= event.timestamp <= after


class TestIntegration:
    """Integration tests for full round-trip scenarios."""

    @pytest.fixture
    def adapter(self):
        """Create a GeminiAdapter instance."""
        return GeminiAdapter()

    @pytest.fixture
    def mock_hook_manager(self):
        """Create a mock HookManager."""
        return MagicMock()

    def test_session_lifecycle_roundtrip(self, adapter, mock_hook_manager) -> None:
        """Tests full session start/end lifecycle."""
        # Session start
        mock_hook_manager.handle.return_value = HookResponse(
            decision="allow",
            context="Welcome! You have 3 pending tasks.",
        )

        start_event = {
            "hook_type": "SessionStart",
            "input_data": {
                "session_id": "gemini-lifecycle-123",
                "cwd": "/home/user/project",
                "timestamp": "2025-01-15T10:00:00Z",
            },
        }

        start_result = adapter.handle_native(start_event, mock_hook_manager)

        assert start_result["decision"] == "allow"
        assert "pending tasks" in start_result["hookSpecificOutput"]["additionalContext"]

        # Session end
        mock_hook_manager.handle.return_value = HookResponse(decision="allow")

        end_event = {
            "hook_type": "SessionEnd",
            "input_data": {
                "session_id": "gemini-lifecycle-123",
                "timestamp": "2025-01-15T11:00:00Z",
            },
        }

        end_result = adapter.handle_native(end_event, mock_hook_manager)

        assert end_result["decision"] == "allow"

    def test_tool_execution_roundtrip(self, adapter, mock_hook_manager) -> None:
        """Tests full tool execution lifecycle."""
        # Before tool
        mock_hook_manager.handle.return_value = HookResponse(decision="allow")

        before_event = {
            "hook_type": "BeforeTool",
            "input_data": {
                "session_id": "gemini-tool-456",
                "tool_name": "WriteFileTool",
                "tool_input": {
                    "path": "/tmp/test.txt",
                    "content": "Hello, World!",
                },
            },
        }

        before_result = adapter.handle_native(before_event, mock_hook_manager)

        assert before_result["decision"] == "allow"

        # Verify the tool name was normalized in the HookEvent
        call_args = mock_hook_manager.handle.call_args[0][0]
        assert call_args.metadata["normalized_tool_name"] == "Write"

        # After tool
        mock_hook_manager.handle.return_value = HookResponse(decision="allow")

        after_event = {
            "hook_type": "AfterTool",
            "input_data": {
                "session_id": "gemini-tool-456",
                "tool_name": "WriteFileTool",
                "tool_output": {"success": True, "bytes_written": 13},
            },
        }

        after_result = adapter.handle_native(after_event, mock_hook_manager)

        assert after_result["decision"] == "allow"

    def test_tool_denied_by_workflow(self, adapter, mock_hook_manager) -> None:
        """Tests tool denial scenario."""
        mock_hook_manager.handle.return_value = HookResponse(
            decision="deny",
            reason="No task claimed. Use gobby-tasks.create_task() first.",
            system_message="File modifications blocked: claim a task first.",
        )

        native_event = {
            "hook_type": "BeforeTool",
            "input_data": {
                "session_id": "gemini-deny-789",
                "tool_name": "EditFileTool",
                "tool_input": {
                    "path": "/src/main.py",
                    "edit": "...",
                },
            },
        }

        result = adapter.handle_native(native_event, mock_hook_manager)

        assert result["decision"] == "deny"
        assert "No task claimed" in result["reason"]
        assert result["systemMessage"] == "File modifications blocked: claim a task first."

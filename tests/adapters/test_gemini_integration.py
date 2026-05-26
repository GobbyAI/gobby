"""Integration and edge-case tests for Gemini adapter handling."""

from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse

pytestmark = pytest.mark.unit


class TestHandleNative:
    """Tests for handle_native() method."""

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

    @pytest.mark.parametrize("field_name", ["hook_event_name", "hookEventName"])
    def test_handle_native_extracts_hook_type_from_top_level_acp_payload(
        self, adapter, mock_hook_manager, field_name: str
    ) -> None:
        """ACP payloads may carry the hook event name outside input_data."""
        mock_hook_manager.handle.return_value = HookResponse(
            decision="allow",
            modify_args={"tool_filter": ["read"]},
        )

        native_event = {
            field_name: "BeforeToolSelection",
            "input_data": {
                "session_id": "sess-tools",
            },
        }

        result = adapter.handle_native(native_event, mock_hook_manager)

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

    def test_handle_native_after_agent_block_retries_when_response_exists(
        self, adapter, mock_hook_manager
    ) -> None:
        """Normal AfterAgent blocks should retry to enforce stop gates."""
        mock_hook_manager.handle.return_value = HookResponse(
            decision="block",
            reason="Tasks still in_progress",
        )

        native_event = {
            "hook_type": "AfterAgent",
            "input_data": {
                "hook_event_name": "AfterAgent",
                "session_id": "sess-after-agent",
                "prompt_response": "hello",
            },
        }

        result = adapter.handle_native(native_event, mock_hook_manager)

        assert result["decision"] == "block"
        assert result["continue"] is True

    def test_handle_native_after_agent_block_stops_when_cancelled(
        self, adapter, mock_hook_manager
    ) -> None:
        """Cancelled AfterAgent turn should not get trapped in a retry loop."""
        mock_hook_manager.handle.return_value = HookResponse(
            decision="block",
            reason="Tasks still in_progress",
        )

        native_event = {
            "hook_type": "AfterAgent",
            "input_data": {
                "hook_event_name": "AfterAgent",
                "session_id": "sess-after-agent",
            },
        }

        result = adapter.handle_native(native_event, mock_hook_manager)

        assert result["decision"] == "block"
        assert result["continue"] is False


class TestEdgeCases:
    """Tests for edge cases and error handling."""

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

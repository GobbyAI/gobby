"""Tests for the context injector utilities."""

import pytest

from gobby.utils.context_injector import (
    build_restored_context,
    build_session_context,
    inject_context_into_response,
)


class TestBuildSessionContext:
    """Tests for build_session_context function."""

    def test_basic_session_context(self):
        """Test building basic session context without parent."""
        result = build_session_context(
            session_id="sess-123",
            user_id="user-456",
            machine_id="machine-789",
        )

        assert "## Gobby Session Context" in result
        assert "Session ID: `sess-123`" in result
        assert "User ID: `user-456`" in result
        assert "Machine ID: `machine-789`" in result
        assert "Parent Session" not in result

    def test_session_context_with_parent(self):
        """Test building session context with parent session (handoff)."""
        result = build_session_context(
            session_id="sess-new",
            user_id="user-456",
            machine_id="machine-789",
            parent_session_id="sess-old",
        )

        assert "Parent Session: `sess-old`" in result
        assert "handoff completed" in result

    def test_session_context_with_none_parent(self):
        """Test building session context with explicit None parent."""
        result = build_session_context(
            session_id="sess-123",
            user_id="user-456",
            machine_id="machine-789",
            parent_session_id=None,
        )

        assert "Parent Session" not in result


class TestBuildRestoredContext:
    """Tests for build_restored_context function."""

    def test_basic_restored_context(self):
        """Test building restored context for handoff."""
        result = build_restored_context(
            session_id="new-sess",
            parent_session_id="old-sess",
            external_id="claude-abc123",
            summary_markdown="# Session Summary\n\nUser worked on tests.",
        )

        assert "system_message" in result
        assert "additional_context" in result

        # Check system message
        system_msg = result["system_message"]
        assert "Context restored" in system_msg
        assert "new-sess" in system_msg
        assert "old-sess" in system_msg
        assert "claude-abc123" in system_msg

        # Check additional context
        add_ctx = result["additional_context"]
        assert "## Previous Session Context" in add_ctx
        assert "Session Summary" in add_ctx
        assert "User worked on tests" in add_ctx

    def test_restored_context_with_multiline_summary(self):
        """Test restored context with multi-line summary."""
        summary = """# Session Summary

## Overview
This was a productive session.

## Key Changes
- Added new feature
- Fixed bug

## Next Steps
1. Run tests
2. Deploy
"""
        result = build_restored_context(
            session_id="sess-1",
            parent_session_id="sess-0",
            external_id="key-123",
            summary_markdown=summary,
        )

        assert "Overview" in result["additional_context"]
        assert "Key Changes" in result["additional_context"]
        assert "Next Steps" in result["additional_context"]


class TestInjectContextIntoResponse:
    """Tests for inject_context_into_response function."""

    def test_inject_basic_context(self):
        """Test injecting basic session context into empty response."""
        response = {}

        result = inject_context_into_response(
            response=response,
            session_id="sess-123",
            user_id="user-456",
            machine_id="machine-789",
        )

        assert "hookSpecificOutput" in result
        assert "additionalContext" in result["hookSpecificOutput"]
        assert "sess-123" in result["hookSpecificOutput"]["additionalContext"]

    def test_inject_context_preserves_existing_response(self):
        """Test that existing response fields are preserved."""
        response = {
            "existingField": "value",
            "anotherField": 123,
        }

        result = inject_context_into_response(
            response=response,
            session_id="sess-123",
            user_id="user-456",
            machine_id="machine-789",
        )

        assert result["existingField"] == "value"
        assert result["anotherField"] == 123
        assert "hookSpecificOutput" in result

    def test_inject_context_with_existing_hook_output(self):
        """Test injecting context when hookSpecificOutput already exists."""
        response = {
            "hookSpecificOutput": {
                "existingKey": "existingValue",
            },
        }

        result = inject_context_into_response(
            response=response,
            session_id="sess-123",
            user_id="user-456",
            machine_id="machine-789",
        )

        # Should preserve existing keys and add additionalContext
        assert result["hookSpecificOutput"]["existingKey"] == "existingValue"
        assert "additionalContext" in result["hookSpecificOutput"]

    def test_inject_context_with_restored_summary(self):
        """Test injecting context with restored session summary."""
        response = {}

        result = inject_context_into_response(
            response=response,
            session_id="new-sess",
            user_id="user-456",
            machine_id="machine-789",
            parent_session_id="old-sess",
            restored_summary="# Previous Summary\nImportant context.",
            external_id="claude-key",
        )

        # Should have system message for handoff
        assert "systemMessage" in result
        assert "Context restored" in result["systemMessage"]

        # Should have combined context
        context = result["hookSpecificOutput"]["additionalContext"]
        assert "Gobby Session Context" in context
        assert "Previous Session Context" in context
        assert "Important context" in context

    def test_inject_context_handoff_without_external_id(self):
        """Test that handoff requires external_id."""
        response = {}

        result = inject_context_into_response(
            response=response,
            session_id="new-sess",
            user_id="user-456",
            machine_id="machine-789",
            parent_session_id="old-sess",
            restored_summary="# Summary",
            external_id=None,  # Missing external_id
        )

        # Should not add restored context or system message without external_id
        assert "systemMessage" not in result
        assert "Previous Session Context" not in result["hookSpecificOutput"]["additionalContext"]

    def test_inject_context_handoff_without_parent_session(self):
        """Test that handoff requires parent_session_id."""
        response = {}

        result = inject_context_into_response(
            response=response,
            session_id="sess-123",
            user_id="user-456",
            machine_id="machine-789",
            parent_session_id=None,  # Missing parent
            restored_summary="# Summary",
            external_id="key-123",
        )

        # Should not add restored context without parent
        assert "systemMessage" not in result

    def test_inject_context_modifies_in_place(self):
        """Test that the function modifies response in place and returns it."""
        response = {"key": "value"}

        result = inject_context_into_response(
            response=response,
            session_id="sess-123",
            user_id="user-456",
            machine_id="machine-789",
        )

        # Should be the same object
        assert result is response

    def test_inject_context_with_all_params(self):
        """Test injecting context with all parameters provided."""
        response = {"status": "ok"}

        result = inject_context_into_response(
            response=response,
            session_id="new-session-uuid",
            user_id="user-uuid-456",
            machine_id="machine-id-789",
            parent_session_id="old-session-uuid",
            restored_summary="# Complete Summary\n\n## Decisions\n- Used pytest\n- Added mocks",
            external_id="claude-code-session-key",
        )

        # Verify all context is present
        assert result["status"] == "ok"
        assert "systemMessage" in result
        context = result["hookSpecificOutput"]["additionalContext"]

        # Session metadata
        assert "new-session-uuid" in context
        assert "user-uuid-456" in context
        assert "machine-id-789" in context
        assert "old-session-uuid" in context

        # Restored summary
        assert "Complete Summary" in context
        assert "Decisions" in context
        assert "pytest" in context


class TestIntegration:
    """Integration tests for context injector utilities."""

    def test_full_handoff_workflow(self):
        """Test a full handoff workflow using all functions."""
        # Build session context
        session_ctx = build_session_context(
            session_id="sess-new",
            user_id="user-1",
            machine_id="machine-1",
            parent_session_id="sess-old",
        )

        assert "handoff completed" in session_ctx

        # Build restored context
        restored = build_restored_context(
            session_id="sess-new",
            parent_session_id="sess-old",
            external_id="claude-123",
            summary_markdown="# Summary\nPrevious work done.",
        )

        assert "Context restored" in restored["system_message"]
        assert "Previous work done" in restored["additional_context"]

        # Inject into response
        response = {"continue": True}
        final = inject_context_into_response(
            response=response,
            session_id="sess-new",
            user_id="user-1",
            machine_id="machine-1",
            parent_session_id="sess-old",
            restored_summary="# Summary\nPrevious work done.",
            external_id="claude-123",
        )

        # Verify complete response
        assert final["continue"] is True
        assert "systemMessage" in final
        assert "hookSpecificOutput" in final
        assert "additionalContext" in final["hookSpecificOutput"]

    def test_new_session_workflow(self):
        """Test a new session workflow (no handoff)."""
        response = {}

        final = inject_context_into_response(
            response=response,
            session_id="brand-new-session",
            user_id="new-user",
            machine_id="new-machine",
        )

        # Should have context but no handoff-specific fields
        assert "hookSpecificOutput" in final
        assert "additionalContext" in final["hookSpecificOutput"]
        assert "systemMessage" not in final

        context = final["hookSpecificOutput"]["additionalContext"]
        assert "brand-new-session" in context
        assert "Parent Session" not in context

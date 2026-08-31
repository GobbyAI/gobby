"""Tests for session summary formatting."""

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.hooks.tool_error_tracker import load_open_tool_errors
from gobby.sessions.analyzer import HandoffContext
from gobby.sessions.summary_formatting import (
    _format_structured_context,
    format_turns_for_llm,
    format_unresolved_errors,
)

pytestmark = pytest.mark.unit


def test_seeded_open_tool_errors_are_lossless_with_bounded_retrieval_preview() -> None:
    full_error = "failed\n## Current State\n```\n" + ("x" * 1_000)
    seeded = [
        {
            "tool": "Bash\n## Current State\n```\n" + ("t" * 1_000),
            "target_key": "/tmp/file\n## Next Steps\n~~~\n" + ("k" * 1_000),
            "error": full_error,
            "first_at": "2026-07-23T00:00:00.000000000000+00:00",
            "last_at": "2026-07-23T00:00:01.000000000000+00:00",
            "count": 10**30,
        },
        {"malformed": True},
    ]
    db = MagicMock()
    db.fetchone.return_value = {"variables": json.dumps({"open_tool_errors": seeded})}
    db.fetchall.return_value = []

    records = load_open_tool_errors(db, "session-1")
    rendered = format_unresolved_errors(records)

    assert len(records) == 1
    assert len(records[0]["tool"]) == 130
    assert len(records[0]["target_key"]) == 130
    assert records[0]["count"] == 999_999
    assert records[0]["error"] == full_error.replace("\n", " ")
    assert records[0]["error_id"].startswith("error-")
    assert rendered.startswith("Unresolved Tool Errors:\n")
    assert len(rendered.splitlines()) == 2
    assert records[0]["error"] not in rendered
    assert 'get_variable(name="open_tool_errors", session_id=<current>)' in rendered
    assert f'error_id="{records[0]["error_id"]}"' in rendered
    assert "\n##" not in rendered
    assert "\n```" not in rendered
    assert "\n~~~" not in rendered
    assert load_open_tool_errors(None, "session-1") == []
    assert format_unresolved_errors([]) == ""


def test_handoff_context_defaults_unresolved_errors_to_an_independent_list() -> None:
    first = HandoffContext()
    second = HandoffContext()

    first.unresolved_errors.append({"tool": "Bash"})

    assert second.unresolved_errors == []


class TestFormatTurnsForLlm:
    """Tests for the format_turns_for_llm helper function."""

    def test_format_empty_turns(self) -> None:
        """Test formatting with empty turns list."""
        result = format_turns_for_llm([])
        assert result == ""

    def test_format_user_turn_string_content(self) -> None:
        """Test formatting a user turn with string content."""
        turns = [{"message": {"role": "user", "content": "Hello world"}}]
        result = format_turns_for_llm(turns)
        assert "[Turn 1 - user]: Hello world" in result

    def test_format_assistant_turn_text_block(self) -> None:
        """Test formatting an assistant turn with text block content."""
        turns: list[dict[str, Any]] = [
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hi there!"}],
                }
            }
        ]
        result = format_turns_for_llm(turns)
        assert "[Turn 1 - assistant]: Hi there!" in result

    def test_format_assistant_turn_thinking_block(self) -> None:
        """Test formatting an assistant turn with thinking block."""
        turns = [
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "thinking", "thinking": "Let me consider..."}],
                }
            }
        ]
        result = format_turns_for_llm(turns)
        assert "[Turn 1 - assistant]: [Thinking: Let me consider...]" in result

    def test_format_assistant_turn_tool_use_block(self) -> None:
        """Test formatting an assistant turn with tool_use block."""
        turns = [
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "read_file"}],
                }
            }
        ]
        result = format_turns_for_llm(turns)
        assert "[Turn 1 - assistant]: [Tool: read_file]" in result

    def test_format_assistant_turn_mixed_blocks(self) -> None:
        """Test formatting assistant turn with multiple block types."""
        turns = [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Let me help."},
                        {"type": "thinking", "thinking": "Analyzing request"},
                        {"type": "tool_use", "name": "search"},
                    ],
                }
            }
        ]
        result = format_turns_for_llm(turns)
        assert "Let me help." in result
        assert "[Thinking: Analyzing request]" in result
        assert "[Tool: search]" in result

    def test_format_multiple_turns(self) -> None:
        """Test formatting multiple turns."""
        turns: list[dict[str, Any]] = [
            {"message": {"role": "user", "content": "First message"}},
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Response"}],
                }
            },
            {"message": {"role": "user", "content": "Second message"}},
        ]
        result = format_turns_for_llm(turns)
        assert "[Turn 1 - user]: First message" in result
        assert "[Turn 2 - assistant]: Response" in result
        assert "[Turn 3 - user]: Second message" in result
        # Check turns are separated by double newlines
        assert "\n\n" in result

    def test_format_turn_missing_message(self) -> None:
        """Test formatting turns with missing message key."""
        turns = [{"other_key": "value"}]
        result = format_turns_for_llm(turns)
        assert "[Turn 1 - unknown]:" in result

    def test_format_turn_missing_role(self) -> None:
        """Test formatting turns with missing role."""
        turns = [{"message": {"content": "No role here"}}]
        result = format_turns_for_llm(turns)
        assert "[Turn 1 - unknown]: No role here" in result

    def test_format_turn_missing_content(self) -> None:
        """Test formatting turns with missing content."""
        turns = [{"message": {"role": "user"}}]
        result = format_turns_for_llm(turns)
        assert "[Turn 1 - user]:" in result

    def test_format_turn_unknown_block_type(self) -> None:
        """Test formatting turns with unknown block type (should be skipped)."""
        turns = [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "unknown_type", "data": "something"},
                        {"type": "text", "text": "Known text"},
                    ],
                }
            }
        ]
        result = format_turns_for_llm(turns)
        # Unknown type should be skipped, only text should appear
        assert "Known text" in result
        assert "unknown_type" not in result

    def test_format_turn_tool_use_missing_name(self) -> None:
        """Test formatting tool_use block with missing name."""
        turns = [
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use"}],  # Missing 'name'
                }
            }
        ]
        result = format_turns_for_llm(turns)
        assert "[Tool: unknown]" in result

    def test_format_turn_non_dict_block(self) -> None:
        """Test formatting with non-dict items in content list."""
        turns = [
            {
                "message": {
                    "role": "assistant",
                    "content": ["string item", {"type": "text", "text": "dict item"}],
                }
            }
        ]
        result = format_turns_for_llm(turns)
        # Non-dict items should be skipped
        assert "dict item" in result
        assert "string item" not in result

    def test_format_assistant_turn_tool_result_block(self) -> None:
        """Test formatting an assistant turn with tool_result block."""
        turns = [
            {
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_123",
                            "content": "File contents here",
                        }
                    ],
                }
            }
        ]
        result = format_turns_for_llm(turns)
        assert "[Turn 1 - user]: [Result: File contents here]" in result

    def test_format_tool_result_references_long_content(self) -> None:
        """Long tool results keep a bounded head and a truncation marker."""
        long_content = "x" * 500  # 500 characters
        turns = [
            {
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_123",
                            "content": long_content,
                        }
                    ],
                }
            }
        ]
        result = format_turns_for_llm(turns)
        assert "[Result: " + "x" * 200 in result
        assert "... [truncated]" in result
        assert "x" * 500 not in result


class TestFormatStructuredContext:
    """Tests for the _format_structured_context helper function."""

    def test_format_structured_context_with_task_progress(self) -> None:
        """Test that task_progress is formatted with task IDs and actions."""
        ctx = HandoffContext(
            task_progress=[
                {"id": "gt-001", "action": "create_task", "title": "Fix login bug"},
                {"id": "gt-001", "action": "claim_task", "title": "Task gt-001"},
                {"id": "gt-001", "action": "close_task", "title": "Task gt-001"},
            ]
        )
        result = _format_structured_context(ctx)

        assert "Task Progress:" in result
        assert "create_task: Fix login bug (gt-001)" in result
        assert "claim_task: Task gt-001 (gt-001)" in result
        assert "close_task: Task gt-001 (gt-001)" in result

    def test_format_structured_context_empty(self) -> None:
        """Test that empty HandoffContext returns empty string."""
        ctx = HandoffContext()
        result = _format_structured_context(ctx)
        assert result == ""

    def test_format_structured_context_caps_task_progress(self) -> None:
        """Test that task_progress is capped at 15 entries."""
        ctx = HandoffContext(
            task_progress=[
                {"id": f"gt-{i:03d}", "action": "update_task", "title": f"Task {i}"}
                for i in range(20)
            ]
        )
        result = _format_structured_context(ctx)

        assert "Task Progress:" in result
        # Should only show the last 15 (indices 5-19)
        assert "Task 5" in result
        assert "Task 19" in result
        # First 5 should be excluded
        assert "gt-000" not in result
        assert "gt-004" not in result
        # Count the lines
        progress_section = result.split("Task Progress:\n")[1]
        lines = [line for line in progress_section.split("\n") if line.strip().startswith("- ")]
        assert len(lines) == 15

    def test_format_structured_context_task_progress_with_other_fields(self) -> None:
        """Test that task_progress coexists with other context fields."""
        ctx = HandoffContext(
            active_gobby_task={"id": "gt-001", "title": "Active task", "status": "in_progress"},
            task_progress=[
                {"id": "gt-001", "action": "claim_task", "title": "Active task"},
            ],
            initial_goal="Fix all the bugs",
        )
        result = _format_structured_context(ctx)

        assert "Active Task:" in result
        assert "Task Progress:" in result
        assert "Original Goal:" in result

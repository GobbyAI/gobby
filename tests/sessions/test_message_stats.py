"""Tests for the shared transcript message-stats predicate.

``compute_message_stats`` is the single source of truth for the session stat
counts used by both the live ``SessionMessageProcessor`` poll loop and the batch
``SessionLifecycleManager`` expiry path.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.sessions.message_stats import compute_message_stats

pytestmark = pytest.mark.unit


def _msg(
    role: str,
    content_type: str,
    content: str = "",
    tool_name: str | None = None,
) -> SimpleNamespace:
    """Build a parsed-message double exposing the attributes the predicate reads."""
    return SimpleNamespace(
        role=role,
        content_type=content_type,
        content=content,
        tool_name=tool_name,
    )


def test_empty_input_returns_zeros() -> None:
    assert compute_message_stats([]) == {
        "message_count": 0,
        "turn_count": 0,
        "tool_call_count": 0,
        "last_assistant_content": None,
    }


def test_counts_messages_turns_and_tools() -> None:
    messages = [
        _msg("user", "text", "Add pagination to the search endpoint"),
        _msg("assistant", "thinking", "Let me think"),
        _msg("assistant", "text", "Here is the plan"),
        _msg("assistant", "tool_use", "", tool_name="Read"),
        _msg("user", "tool_result", "file contents"),
        _msg("assistant", "text", "Done"),
    ]

    stats = compute_message_stats(messages)

    assert stats["message_count"] == 6
    # Only assistant messages whose content_type is "text" count as turns.
    assert stats["turn_count"] == 2
    # Only the tool_use message carries a tool_name.
    assert stats["tool_call_count"] == 1
    assert stats["last_assistant_content"] == "Done"


def test_last_assistant_content_tracks_last_nonempty_text() -> None:
    messages = [
        _msg("assistant", "text", "first"),
        _msg("assistant", "text", "   "),  # whitespace-only: still a turn, not stored
        _msg("assistant", "text", "second"),
        _msg("user", "text", "later user prompt"),  # not assistant: ignored
    ]

    stats = compute_message_stats(messages)

    assert stats["turn_count"] == 3  # all three assistant text messages
    assert stats["last_assistant_content"] == "second"


def test_last_assistant_content_clamped_to_trailing_500_chars() -> None:
    messages = [_msg("assistant", "text", "x" * 600 + "TAIL")]

    content = compute_message_stats(messages)["last_assistant_content"]

    assert content is not None
    assert len(content) == 500
    assert content.endswith("TAIL")


def test_tool_name_counts_regardless_of_role() -> None:
    messages = [
        _msg("assistant", "tool_use", "", tool_name="Bash"),
        _msg("user", "tool_result", "out", tool_name="Bash"),
    ]

    stats = compute_message_stats(messages)

    assert stats["tool_call_count"] == 2
    assert stats["turn_count"] == 0


def test_accepts_sequence_of_message_protocol() -> None:
    stats = compute_message_stats(
        (
            _msg("assistant", "text", "tuple message"),
            _msg("user", "tool_result", "", tool_name="Bash"),
        )
    )

    assert stats == {
        "message_count": 2,
        "turn_count": 1,
        "tool_call_count": 1,
        "last_assistant_content": "tuple message",
    }

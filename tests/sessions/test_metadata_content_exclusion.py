"""Metadata content types (``NON_MESSAGE_CONTENT_TYPES``) are excluded from
message/flat counts everywhere.

These lock in the unified "not a conversation message" rule for the three
metadata content types — ``session_title``, ``hook_prompt``, and the
``unmodeled_record`` sentinel — at the pure-function choke points shared by the
live processor, the lifecycle path, and the index.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.sessions.message_stats import compute_message_stats
from gobby.sessions.transcript_parsing import _parsed_to_dicts
from gobby.sessions.transcripts.base import (
    NON_MESSAGE_CONTENT_TYPES,
    UNMODELED_RECORD_CONTENT_TYPE,
    ParsedMessage,
)

pytestmark = pytest.mark.unit


def _msg(
    *,
    index: int,
    role: str,
    content_type: str,
    content: str = "",
    tool_name: str | None = None,
    tool_use_id: str | None = None,
) -> ParsedMessage:
    return ParsedMessage(
        index=index,
        role=role,
        content=content,
        content_type=content_type,
        tool_name=tool_name,
        tool_input=None,
        tool_result=None,
        timestamp=datetime(2026, 6, 27, tzinfo=UTC),
        raw_json={"type": content_type, "i": index},
        tool_use_id=tool_use_id,
        source="claude",
        source_ref=str(index),
        source_line=index,
    )


def test_non_message_content_types_membership() -> None:
    assert NON_MESSAGE_CONTENT_TYPES == frozenset(
        {
            "session_title",
            "hook_prompt",
            "usage",
            "turn_completed",
            UNMODELED_RECORD_CONTENT_TYPE,
        }
    )


class TestComputeMessageStatsExclusion:
    def test_metadata_does_not_bump_message_count(self) -> None:
        messages = [
            _msg(index=0, role="user", content_type="text", content="hi"),
            _msg(index=1, role="system", content_type="session_title", content="A title"),
            _msg(index=2, role="system", content_type="hook_prompt", content="hook"),
            _msg(index=3, role="system", content_type=UNMODELED_RECORD_CONTENT_TYPE, content="x"),
            _msg(index=4, role="assistant", content_type="usage"),
            _msg(index=5, role="assistant", content_type="text", content="answer"),
        ]
        stats = compute_message_stats(messages)
        # Only the two real conversation messages count.
        assert stats["message_count"] == 2
        # One assistant text turn; metadata never matches the turn predicate.
        assert stats["turn_count"] == 1
        assert stats["last_assistant_content"] == "answer"

    def test_metadata_does_not_affect_tool_count(self) -> None:
        messages = [
            _msg(
                index=0,
                role="assistant",
                content_type="tool_use",
                tool_name="Read",
                tool_use_id="t1",
            ),
            _msg(index=1, role="system", content_type="session_title", content="A title"),
        ]
        stats = compute_message_stats(messages)
        assert stats["message_count"] == 1
        assert stats["tool_call_count"] == 1


class TestParsedToDictsExclusion:
    def test_metadata_rows_are_dropped(self) -> None:
        messages = [
            _msg(index=0, role="user", content_type="text", content="hi"),
            _msg(index=1, role="system", content_type="session_title", content="title"),
            _msg(index=2, role="system", content_type=UNMODELED_RECORD_CONTENT_TYPE, content="x"),
            _msg(index=3, role="assistant", content_type="text", content="bye"),
        ]
        rows = _parsed_to_dicts(messages)
        assert [r["content_type"] for r in rows] == ["text", "text"]
        assert [r["message_index"] for r in rows] == [0, 3]

    def test_single_metadata_record_flattens_to_empty(self) -> None:
        # This is the precondition the streaming flat collectors rely on: a
        # single-metadata batch returns [] (collectors must guard the [0] index).
        title = _msg(index=0, role="system", content_type="session_title", content="title")
        sentinel = _msg(
            index=1, role="system", content_type=UNMODELED_RECORD_CONTENT_TYPE, content="x"
        )
        assert _parsed_to_dicts([title]) == []
        assert _parsed_to_dicts([sentinel]) == []

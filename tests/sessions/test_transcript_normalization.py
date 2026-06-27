"""Transcript normalization regression tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.sessions.transcript_normalization import normalize_transcript_records
from gobby.sessions.transcripts.base import ParsedMessage

pytestmark = pytest.mark.unit


def _todo_snapshot_pair(
    start_index: int, tool_use_id: str, todos: list[dict[str, str]]
) -> list[ParsedMessage]:
    timestamp = datetime.now(UTC)
    return [
        ParsedMessage(
            index=start_index,
            role="assistant",
            content="",
            content_type="tool_use",
            tool_name="TodoWrite",
            tool_input={"todos": todos},
            tool_result=None,
            timestamp=timestamp,
            raw_json={"type": "todo_state", "todos": todos},
            tool_use_id=tool_use_id,
        ),
        ParsedMessage(
            index=start_index + 1,
            role="tool",
            content="",
            content_type="tool_result",
            tool_name=None,
            tool_input=None,
            tool_result={"todos": todos, "source": "todo_state"},
            timestamp=timestamp,
            raw_json={"type": "todo_state", "todos": todos},
            tool_use_id=tool_use_id,
        ),
    ]


def _text_message(index: int, content: str) -> ParsedMessage:
    return ParsedMessage(
        index=index,
        role="assistant",
        content=content,
        content_type="text",
        tool_name=None,
        tool_input=None,
        tool_result=None,
        timestamp=datetime.now(UTC),
        raw_json={},
    )


def test_normalize_transcript_records_ignores_non_string_grok_update_type() -> None:
    message = ParsedMessage(
        index=0,
        role="tool",
        content="",
        content_type="tool_result",
        tool_name="PostToolUse",
        tool_input=None,
        tool_result=None,
        timestamp=datetime.now(UTC),
        raw_json={"sessionUpdate": 123, "type": {"name": "bad"}},
    )

    assert normalize_transcript_records([message], "grok") == [message]


def test_normalize_transcript_records_collapses_consecutive_droid_todo_snapshots() -> None:
    todos: list[dict[str, str]] = [{"content": "check", "status": "completed"}]
    first_snapshot = _todo_snapshot_pair(0, "droid-todo-state-same", todos)
    duplicate_snapshot = _todo_snapshot_pair(2, "droid-todo-state-same", todos)
    separator = _text_message(4, "keep the surrounding message")
    non_consecutive_repeat = _todo_snapshot_pair(5, "droid-todo-state-same", todos)
    different_snapshot = _todo_snapshot_pair(
        7,
        "droid-todo-state-different",
        [{"content": "ship", "status": "pending"}],
    )
    records = (
        first_snapshot
        + duplicate_snapshot
        + [separator]
        + non_consecutive_repeat
        + different_snapshot
    )

    normalized = normalize_transcript_records(records, "droid")

    assert normalized == first_snapshot + [separator] + non_consecutive_repeat + different_snapshot
    assert [record.index for record in normalized] == [0, 1, 4, 5, 6, 7, 8]
    assert normalize_transcript_records(records, "codex") == records

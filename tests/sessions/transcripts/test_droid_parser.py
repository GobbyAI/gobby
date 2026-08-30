"""Tests for Factory Droid transcript parsing."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.sessions.processor import SessionMessageProcessor
from gobby.sessions.transcript_parsing import _parse_lines
from gobby.sessions.transcript_renderer import render_transcript
from gobby.sessions.transcript_source import (
    _detect_source_from_jsonl_lines,
    _detect_source_from_path,
)
from gobby.sessions.transcripts import get_parser
from gobby.sessions.transcripts.base import ParsedMessage
from gobby.sessions.transcripts.droid import DroidTranscriptParser

pytestmark = pytest.mark.unit

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "droid"
FIXTURE_JSONL = FIXTURE_DIR / "dbf95187-5fa4-43a0-b207-8c24f412baf7.jsonl"


def _fixture_lines() -> list[str]:
    return FIXTURE_JSONL.read_text(encoding="utf-8").splitlines(keepends=True)


def _fixture_turns() -> list[dict[str, Any]]:
    return [data for line in _fixture_lines() if isinstance((data := json.loads(line)), dict)]


def test_parse_fixture_expands_blocks_and_loads_sidecar_usage() -> None:
    parser = DroidTranscriptParser(
        session_id="gobby-session-id",
        transcript_path=FIXTURE_JSONL,
    )

    records = parser.parse_lines(_fixture_lines(), start_index=0)

    assert len(records) == 7
    assert all(isinstance(record, ParsedMessage) for record in records)
    messages = [record for record in records if isinstance(record, ParsedMessage)]
    assert [message.index for message in messages] == list(range(7))
    assert [message.content_type for message in messages] == [
        "text",
        "thinking",
        "text",
        "tool_use",
        "tool_use",
        "tool_result",
        "tool_result",
    ]

    assert messages[0].role == "user"
    assert messages[0].content == "List the available MCP servers."
    assert messages[3].tool_name == "gobby___list_mcp_servers"
    assert messages[3].tool_input == {}
    assert messages[4].tool_name == "Read"
    assert messages[4].tool_input == {"file_path": "/redacted/project/AGENTS.md"}
    assert messages[5].tool_result == {
        "content": "servers: gobby-tasks, gobby-memory",
        "is_error": False,
    }
    assert messages[6].tool_use_id == "toolu_02"

    assert messages[1].usage is None
    assert messages[4].usage is not None
    assert messages[4].usage.input_tokens == 22571
    assert messages[4].usage.output_tokens == 512
    assert messages[4].usage.cache_creation_tokens == 0
    assert messages[4].usage.cache_read_tokens == 26112
    assert not hasattr(messages[4].usage, "thinking_tokens")
    assert {message.model for message in messages} == {"claude-3-7-sonnet-latest"}


def test_sidecar_lookup_uses_transcript_path_not_session_id() -> None:
    parser = DroidTranscriptParser(
        session_id="different-gobby-session-id",
        transcript_path=FIXTURE_JSONL,
    )

    messages = parser.parse_lines(_fixture_lines(), start_index=0)

    assistant_messages = [
        record
        for record in messages
        if isinstance(record, ParsedMessage) and record.role == "assistant"
    ]
    assert assistant_messages[-1].usage is not None
    assert assistant_messages[-1].usage.input_tokens == 22571


def test_missing_transcript_path_or_sidecar_degrades_without_usage(tmp_path: Path) -> None:
    missing_sidecar_path = tmp_path / "session.jsonl"

    no_path_records = DroidTranscriptParser().parse_lines(_fixture_lines(), start_index=0)
    missing_sidecar_records = DroidTranscriptParser(
        transcript_path=missing_sidecar_path,
    ).parse_lines(_fixture_lines(), start_index=0)

    assert all(
        not isinstance(record, ParsedMessage) or record.usage is None for record in no_path_records
    )
    assert all(
        not isinstance(record, ParsedMessage) or record.usage is None
        for record in missing_sidecar_records
    )


def test_missing_sidecar_is_retried_when_created_later(tmp_path: Path) -> None:
    transcript_path = tmp_path / "session.jsonl"
    parser = DroidTranscriptParser(transcript_path=transcript_path)

    initial_records = parser.parse_lines(_fixture_lines(), start_index=0)
    transcript_path.with_suffix(".settings.json").write_text(
        json.dumps(
            {
                "model": "claude-3-7-sonnet-latest",
                "tokenUsage": {
                    "inputTokens": 10,
                    "outputTokens": 2,
                    "cacheCreationTokens": 0,
                    "cacheReadTokens": 7,
                },
            }
        ),
        encoding="utf-8",
    )
    reloaded_records = parser.parse_lines(_fixture_lines(), start_index=0)

    assert all(
        not isinstance(record, ParsedMessage) or record.usage is None for record in initial_records
    )
    assistant_records = [
        record
        for record in reloaded_records
        if isinstance(record, ParsedMessage) and record.role == "assistant"
    ]
    assert assistant_records[-1].usage is not None
    assert assistant_records[-1].usage.input_tokens == 10
    assert assistant_records[-1].usage.cache_read_tokens == 7


def test_null_sidecar_usage_is_retried_and_emits_deltas(tmp_path: Path) -> None:
    transcript_path = tmp_path / "session.jsonl"
    sidecar_path = transcript_path.with_suffix(".settings.json")
    parser = DroidTranscriptParser(transcript_path=transcript_path)
    sidecar_path.write_text(
        json.dumps({"model": "claude-3-7-sonnet-latest", "tokenUsage": None}),
        encoding="utf-8",
    )

    initial_records = parser.parse_lines(_fixture_lines(), start_index=0)
    sidecar_path.write_text(
        json.dumps(
            {
                "model": "claude-3-7-sonnet-latest",
                "tokenUsage": {
                    "inputTokens": 10,
                    "outputTokens": 2,
                    "thinkingTokens": 3,
                    "cacheCreationTokens": 1,
                    "cacheReadTokens": 7,
                },
            }
        ),
        encoding="utf-8",
    )
    first_usage_records = parser.parse_lines(_fixture_lines(), start_index=7)
    sidecar_path.write_text(
        json.dumps(
            {
                "model": "claude-3-7-sonnet-latest",
                "tokenUsage": {
                    "inputTokens": 14,
                    "outputTokens": 5,
                    "thinkingTokens": 5,
                    "cacheCreationTokens": 1,
                    "cacheReadTokens": 9,
                },
            }
        ),
        encoding="utf-8",
    )
    delta_records = parser.parse_lines(_fixture_lines(), start_index=14)

    assert all(
        record.usage is None for record in initial_records if isinstance(record, ParsedMessage)
    )
    first_usage = next(
        record.usage
        for record in reversed(first_usage_records)
        if isinstance(record, ParsedMessage) and record.usage is not None
    )
    assert first_usage.input_tokens == 10
    assert first_usage.output_tokens == 5
    delta = next(
        record.usage
        for record in reversed(delta_records)
        if isinstance(record, ParsedMessage) and record.usage is not None
    )
    assert delta.input_tokens == 4
    assert delta.output_tokens == 5
    assert delta.cache_creation_tokens == 0
    assert delta.cache_read_tokens == 2


def test_parse_line_returns_first_expanded_block() -> None:
    parser = DroidTranscriptParser()

    record = parser.parse_line(_fixture_lines()[2], index=12)

    assert isinstance(record, ParsedMessage)
    assert record.index == 12
    assert record.role == "assistant"
    assert record.content_type == "thinking"
    assert record.content == "I should inspect the MCP server registry."


def test_todo_state_metadata_is_ignored_without_unknown_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = DroidTranscriptParser(session_id="droid-session")
    calls: list[dict[str, Any]] = []

    def _log_unknown_block(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(parser.error_log, "log_unknown_block", _log_unknown_block)

    todos = [{"content": "check", "status": "completed"}]
    line = json.dumps({"type": "todo_state", "id": "snapshot-1", "todos": todos})

    records = parser.parse_lines([line])

    assert calls == []
    messages = [record for record in records if isinstance(record, ParsedMessage)]
    assert len(messages) == 2

    tool_use, tool_result = messages
    assert tool_use.role == "assistant"
    assert tool_use.content_type == "tool_use"
    assert tool_use.tool_name == "TodoWrite"
    assert tool_use.tool_input == {"todos": todos}
    assert tool_use.tool_use_id is not None
    assert tool_use.tool_use_id.startswith("droid-todo-state-")

    assert tool_result.role == "tool"
    assert tool_result.content_type == "tool_result"
    assert tool_result.tool_result == {"todos": todos, "source": "todo_state"}
    assert tool_result.tool_use_id == tool_use.tool_use_id

    same_session_records = DroidTranscriptParser(session_id="droid-session").parse_lines([line])
    same_session_messages = [
        record for record in same_session_records if isinstance(record, ParsedMessage)
    ]
    assert same_session_messages[0].tool_use_id == tool_use.tool_use_id

    other_session_records = DroidTranscriptParser(session_id="other-session").parse_lines([line])
    other_session_messages = [
        record for record in other_session_records if isinstance(record, ParsedMessage)
    ]
    assert other_session_messages[0].tool_use_id != tool_use.tool_use_id

    rendered = render_transcript(messages, cli_name="droid", source="droid")
    assert len(rendered) == 1
    assert rendered[0].role == "assistant"
    block = rendered[0].content_blocks[0]
    assert block.type == "tool_chain"
    tool_call = block.tool_calls[0]
    assert tool_call.tool_name == "TodoWrite"
    assert tool_call.status == "completed"
    assert tool_call.result is not None
    assert tool_call.result.content == {"todos": todos, "source": "todo_state"}


def test_extract_last_messages_strips_injected_user_blocks() -> None:
    parser = DroidTranscriptParser()

    messages = parser.extract_last_messages(_fixture_turns(), num_pairs=2)

    assert messages == [
        {"role": "user", "content": "List the available MCP servers."},
        {"role": "assistant", "content": "I will check the registered MCP servers."},
    ]


def test_droid_has_no_clear_boundary() -> None:
    parser = DroidTranscriptParser()
    turns = _fixture_turns()

    assert parser.extract_turns_since_clear(turns) == turns
    assert parser.extract_turns_since_clear(turns, max_turns=2) == turns[-2:]
    assert parser.is_session_boundary(turns[1]) is False


def test_transcript_reader_detects_and_parses_droid_with_transcript_path() -> None:
    lines = _fixture_lines()
    parser = get_parser(
        "droid",
        session_id="gobby-session-id",
        transcript_path=FIXTURE_JSONL,
    )

    assert isinstance(parser, DroidTranscriptParser)
    assert parser._transcript_path == FIXTURE_JSONL
    assert (
        _detect_source_from_path("~/.factory/sessions/-Users-josh-Projects-gobby/abc.jsonl")
        == "droid"
    )
    assert _detect_source_from_jsonl_lines(lines) == "droid"

    messages = _parse_lines(
        lines,
        "droid",
        session_id="gobby-session-id",
        transcript_path=FIXTURE_JSONL,
    )

    assert messages[-3].tool_name == "Read"
    assert messages[-3].usage is not None
    assert messages[-3].usage.cache_read_tokens == 26112


def test_all_droid_parser_constructions_pass_transcript_path() -> None:
    source_root = Path("src/gobby/sessions")
    construction_pattern = re.compile(r"\bDroidTranscriptParser\(")

    for path in source_root.rglob("*.py"):
        if path == Path("src/gobby/sessions/transcripts/droid.py"):
            continue
        text = path.read_text(encoding="utf-8")
        for match in construction_pattern.finditer(text):
            call_window = text[match.start() : match.start() + 300]
            assert "transcript_path=" in call_window, (
                f"{path} omits transcript_path near {call_window}"
            )


def test_session_processor_registers_droid_parser_with_transcript_path() -> None:
    processor = SessionMessageProcessor(db=MagicMock())

    processor.register_session("session-id", str(FIXTURE_JSONL), source="droid")

    parser = processor._parsers["session-id"]
    assert isinstance(parser, DroidTranscriptParser)
    assert parser._transcript_path == FIXTURE_JSONL


def test_session_start_with_title_emits_session_title() -> None:
    """session_start with a non-placeholder title emits a session_title ParsedMessage."""
    parser = DroidTranscriptParser(session_id="test-session")
    line = json.dumps(
        {
            "type": "session_start",
            "title": "Investigate tmux title issues",
            "timestamp": "2026-04-22T10:00:00Z",
        }
    )
    msgs = parser._expand_line(line, 0)
    assert len(msgs) == 1
    assert msgs[0].content_type == "session_title"
    assert msgs[0].role == "system"
    assert msgs[0].content == "Investigate tmux title issues"

    single = parser.parse_line(line, 0)
    assert single is not None
    assert single.content_type == "session_title"


def test_session_start_placeholder_title_still_emits_message() -> None:
    """session_start with 'New Session' still emits a session_title ParsedMessage;
    the processor's normalize_native_title handles rejecting the placeholder."""
    parser = DroidTranscriptParser(session_id="test-session")
    line = json.dumps(
        {
            "type": "session_start",
            "title": "New Session",
            "timestamp": "2026-04-22T10:00:00Z",
        }
    )
    msgs = parser._expand_line(line, 0)
    assert len(msgs) == 1
    assert msgs[0].content_type == "session_title"
    assert msgs[0].content == "New Session"


def test_session_start_without_title_is_skipped() -> None:
    """session_start with no title field produces no message."""
    parser = DroidTranscriptParser(session_id="test-session")
    line = json.dumps(
        {
            "type": "session_start",
            "timestamp": "2026-04-22T10:00:00Z",
        }
    )
    assert parser._expand_line(line, 0) == []
    assert parser.parse_line(line, 0) is None

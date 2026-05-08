"""Tests for Factory Droid transcript parsing."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.sessions.processor import SessionMessageProcessor
from gobby.sessions.transcript_reader import (
    _detect_source_from_jsonl_lines,
    _detect_source_from_path,
    _get_parser,
    _parse_lines,
)
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
    assert messages[4].usage.output_tokens == 384
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


def test_parse_line_returns_first_expanded_block() -> None:
    parser = DroidTranscriptParser()

    record = parser.parse_line(_fixture_lines()[2], index=12)

    assert isinstance(record, ParsedMessage)
    assert record.index == 12
    assert record.role == "assistant"
    assert record.content_type == "thinking"
    assert record.content == "I should inspect the MCP server registry."


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
    parser = _get_parser(
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

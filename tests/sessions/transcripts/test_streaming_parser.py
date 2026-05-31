"""Streaming-parser equivalence + resume tests.

The windowed index/render build on ``iter_parse_events`` + ``finalize``. These
tests assert that:

1. Streaming (flattened events + applied finalize) reproduces batch ``parse_lines``
   exactly, for every CLI parser.
2. ``ParseEvent`` echoes the correct ``RawLine`` byte offset / raw line number.
3. **Resume equivalence** — resuming the stream from any emitted event boundary
   (feeding the raw lines from that boundary with ``start_index=parsed_index``)
   reproduces the corresponding tail of the full parse with identical global
   ``ParsedMessage.index`` values. This is the property ``render_window`` relies on
   when it seeks to a ``resume_safe`` boundary.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from gobby.sessions.transcripts.base import (
    BaseTranscriptParser,
    ParsedMessage,
    ParseEvent,
    RawLine,
    apply_adjustment,
)
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.sessions.transcripts.gemini import GeminiTranscriptParser


def _raw_lines_with_offsets(lines: list[str]) -> list[RawLine]:
    """Build RawLines with cumulative byte offsets (as a file streamer would)."""
    out: list[RawLine] = []
    offset = 0
    for i, text in enumerate(lines):
        out.append(RawLine(byte_offset=offset, raw_line_no=i, text=text))
        offset += len((text + "\n").encode("utf-8"))
    return out


def _batch(parser: BaseTranscriptParser, lines: list[str]) -> list[ParsedMessage]:
    records = list(parser.parse_lines(lines, start_index=0))
    return [r for r in records if isinstance(r, ParsedMessage)]


def _stream_messages(
    parser: BaseTranscriptParser,
    raws: list[RawLine],
    start_index: int,
) -> tuple[list[ParseEvent], list[ParsedMessage]]:
    events = list(parser.iter_parse_events(raws, start_index=start_index))
    records: list = []
    for ev in events:
        records.extend(ev.records)
    for adj in parser.finalize():
        apply_adjustment(records, adj)
    msgs = [r for r in records if isinstance(r, ParsedMessage)]
    return events, msgs


def _codex_lines() -> list[str]:
    def msg(role: str, text: str) -> str:
        block = "output_text" if role == "assistant" else "input_text"
        return json.dumps(
            {
                "timestamp": "2024-06-15T10:30:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": role,
                    "content": [{"type": block, "text": text}],
                },
            }
        )

    def fn_call(name: str, call_id: str) -> str:
        return json.dumps(
            {
                "timestamp": "2024-06-15T10:30:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": name,
                    "arguments": "{}",
                    "call_id": call_id,
                },
            }
        )

    def fn_out(call_id: str, output: str) -> str:
        return json.dumps(
            {
                "timestamp": "2024-06-15T10:30:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            }
        )

    return [
        msg("user", "hello"),
        msg("assistant", "let me look"),
        fn_call("read", "call_1"),
        fn_out("call_1", "file contents"),
        msg("assistant", "done"),
        json.dumps({"timestamp": "x", "type": "event_msg", "payload": {"type": "token_count"}}),
        msg("user", "thanks"),
        msg("assistant", "you are welcome"),
    ]


def _claude_lines() -> list[str]:
    def user(text: str) -> str:
        return json.dumps(
            {
                "type": "user",
                "message": {"content": text},
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )

    def assistant_multi() -> str:
        return json.dumps(
            {
                "type": "agent",
                "message": {
                    "content": [
                        {"type": "text", "text": "Let me read that file."},
                        {
                            "type": "tool_use",
                            "id": "toolu_read1",
                            "name": "Read",
                            "input": {"file_path": "/tmp/test.txt"},
                        },
                        {
                            "type": "tool_use",
                            "id": "toolu_read2",
                            "name": "Grep",
                            "input": {"pattern": "foo"},
                        },
                    ]
                },
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )

    def user_results() -> str:
        return json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_read1",
                            "content": "file contents here",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_read2",
                            "content": "second result",
                        },
                    ]
                },
                "timestamp": "2024-01-01T12:00:00Z",
            }
        )

    return [user("hi"), assistant_multi(), user_results(), user("bye")]


def _gemini_lines() -> list[str]:
    def line(role: str, text: str) -> str:
        return json.dumps({"type": role, "content": text, "timestamp": "2024-01-01T12:00:00Z"})

    return [line("user", "hi"), line("assistant", "hello"), line("user", "more")]


PARSERS = {
    "codex": (CodexTranscriptParser, _codex_lines),
    "claude": (ClaudeTranscriptParser, _claude_lines),
    "gemini": (GeminiTranscriptParser, _gemini_lines),
}


def _msg_key(m: ParsedMessage) -> tuple:
    return (m.index, m.role, m.content_type, m.content, m.tool_use_id)


@pytest.mark.unit
@pytest.mark.parametrize("name", list(PARSERS))
def test_streaming_matches_batch(name: str) -> None:
    parser_cls, lines_fn = PARSERS[name]
    lines = lines_fn()

    batch = _batch(parser_cls(), lines)
    raws = _raw_lines_with_offsets(lines)
    _events, streamed = _stream_messages(parser_cls(), raws, start_index=0)

    assert [_msg_key(m) for m in streamed] == [_msg_key(m) for m in batch]


@pytest.mark.unit
@pytest.mark.parametrize("name", list(PARSERS))
def test_event_offsets_echoed(name: str) -> None:
    parser_cls, lines_fn = PARSERS[name]
    raws = _raw_lines_with_offsets(lines_fn())
    by_line = {r.raw_line_no: r for r in raws}

    events = list(parser_cls().iter_parse_events(raws, start_index=0))
    assert events, "expected at least one event"
    for ev in events:
        assert ev.byte_offset == by_line[ev.raw_line_no].byte_offset
        assert ev.records, "emitted events should carry records"
        assert ev.records[0].index == ev.parsed_index


@pytest.mark.unit
def test_claude_buffered_lookahead_marks_event_not_parser_safe() -> None:
    events = list(
        ClaudeTranscriptParser().iter_parse_events(
            _raw_lines_with_offsets(_claude_lines()),
            start_index=0,
        )
    )

    assert any(not ev.parser_safe for ev in events[:-1])
    assert events[-1].parser_safe is True


@pytest.mark.unit
@pytest.mark.parametrize("name", list(PARSERS))
def test_resume_from_every_event_matches_tail(name: str) -> None:
    """Resuming from any event boundary reproduces the full-parse tail exactly."""
    parser_cls, lines_fn = PARSERS[name]
    lines = lines_fn()
    raws = _raw_lines_with_offsets(lines)

    full_events, full_msgs = _stream_messages(parser_cls(), raws, start_index=0)

    for ev in full_events:
        if not ev.parser_safe:
            continue
        resume_raws = [r for r in raws if r.raw_line_no >= ev.raw_line_no]
        _ev2, resumed = _stream_messages(parser_cls(), resume_raws, start_index=ev.parsed_index)
        expected_tail = [m for m in full_msgs if m.index >= ev.parsed_index]
        assert [_msg_key(m) for m in resumed] == [_msg_key(m) for m in expected_tail], (
            f"resume at line {ev.raw_line_no}/idx {ev.parsed_index} diverged for {name}"
        )


def test_base_default_indexing_preserves_blank_gaps() -> None:
    """Base default iter_parse_events keeps per-line index gaps for blank lines."""

    class _OneToOne(BaseTranscriptParser):
        def __init__(self) -> None:
            super().__init__("test")

        def parse_line(self, line, index):
            return ParsedMessage(
                index=index,
                role="user",
                content=line.strip(),
                content_type="text",
                tool_name=None,
                tool_input=None,
                tool_result=None,
                timestamp=datetime.now(UTC),
                raw_json={},
            )

    parser = _OneToOne()
    lines = ["a", "", "b"]
    msgs = [r for r in parser.parse_lines(lines) if isinstance(r, ParsedMessage)]
    # "a" -> index 0, blank consumes slot 1, "b" -> index 2 (gap preserved).
    assert [m.index for m in msgs] == [0, 2]
    assert [m.content for m in msgs] == ["a", "b"]

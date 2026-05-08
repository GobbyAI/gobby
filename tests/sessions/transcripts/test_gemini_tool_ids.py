"""Tests for deterministic Gemini/Qwen synthetic tool_use_id generation."""

from __future__ import annotations

import json

import pytest

from gobby.sessions.transcripts.gemini import GeminiTranscriptParser
from gobby.sessions.transcripts.qwen import QwenTranscriptParser

pytestmark = pytest.mark.unit


def _jsonl_tool_use_lines() -> list[str]:
    """A canonical JSONL stream with two synthetic-id tool uses."""
    return [
        json.dumps({"type": "tool_use", "tool_name": "Bash", "parameters": {"cmd": "ls"}}),
        json.dumps({"type": "tool_result", "tool_id": None, "output": "a\nb", "status": "success"}),
        json.dumps({"type": "tool_use", "tool_name": "Read", "parameters": {"path": "/x"}}),
        json.dumps({"type": "tool_result", "output": "contents", "status": "success"}),
    ]


def _session_json_with_tool_calls() -> dict[str, object]:
    """A canonical Gemini JSON session message with two tool calls and no upstream IDs."""
    return {
        "type": "gemini",
        "content": "",
        "toolCalls": [
            {
                "name": "Bash",
                "args": {"cmd": "ls"},
                "result": [{"functionResponse": {"output": "a\nb"}}],
            },
            {
                "name": "Read",
                "args": {"path": "/x"},
                "result": [{"functionResponse": {"output": "contents"}}],
            },
        ],
    }


def _parse_jsonl_twice(parser_cls: type[GeminiTranscriptParser]) -> tuple[list[str], list[str]]:
    lines = _jsonl_tool_use_lines()
    first = parser_cls(session_id="sess-deadbeef")
    second = parser_cls(session_id="sess-deadbeef")
    first_ids = [
        msg.tool_use_id
        for line_index, raw in enumerate(lines)
        if (msg := first.parse_line(raw, line_index)) is not None and msg.tool_use_id is not None
    ]
    second_ids = [
        msg.tool_use_id
        for line_index, raw in enumerate(lines)
        if (msg := second.parse_line(raw, line_index)) is not None and msg.tool_use_id is not None
    ]
    return first_ids, second_ids


def _parse_session_twice(parser_cls: type[GeminiTranscriptParser]) -> tuple[list[str], list[str]]:
    msg = _session_json_with_tool_calls()
    first = parser_cls(session_id="sess-deadbeef")
    second = parser_cls(session_id="sess-deadbeef")
    first_ids = [
        m.tool_use_id for m in first._parse_session_message(msg, 0) if m.tool_use_id is not None
    ]
    second_ids = [
        m.tool_use_id for m in second._parse_session_message(msg, 0) if m.tool_use_id is not None
    ]
    return first_ids, second_ids


@pytest.mark.parametrize("parser_cls", [GeminiTranscriptParser, QwenTranscriptParser])
def test_jsonl_synthetic_ids_are_stable_across_reparses(
    parser_cls: type[GeminiTranscriptParser],
) -> None:
    first, second = _parse_jsonl_twice(parser_cls)
    assert first == second
    assert len(first) >= 2  # tool_use + tool_result × 2 — pairing reuses last id for results


@pytest.mark.parametrize("parser_cls", [GeminiTranscriptParser, QwenTranscriptParser])
def test_session_json_synthetic_ids_are_stable_across_reparses(
    parser_cls: type[GeminiTranscriptParser],
) -> None:
    first, second = _parse_session_twice(parser_cls)
    assert first == second


def test_synthetic_ids_carry_provider_specific_prefix() -> None:
    gemini_ids, _ = _parse_jsonl_twice(GeminiTranscriptParser)
    qwen_ids, _ = _parse_jsonl_twice(QwenTranscriptParser)
    assert all(tid.startswith("gemini-tu-") for tid in gemini_ids)
    assert all(tid.startswith("qwen-tu-") for tid in qwen_ids)
    # Same logical input should produce different IDs across providers (cli_name in hash).
    assert set(gemini_ids).isdisjoint(set(qwen_ids))


def test_session_id_changes_synthetic_ids() -> None:
    a = GeminiTranscriptParser(session_id="sess-a")
    b = GeminiTranscriptParser(session_id="sess-b")
    msg = _session_json_with_tool_calls()
    a_ids = [m.tool_use_id for m in a._parse_session_message(msg, 0) if m.tool_use_id]
    b_ids = [m.tool_use_id for m in b._parse_session_message(msg, 0) if m.tool_use_id]
    assert set(a_ids).isdisjoint(set(b_ids))


def test_explicit_upstream_id_is_preserved_unchanged() -> None:
    parser = GeminiTranscriptParser(session_id="sess-x")
    line = json.dumps(
        {
            "type": "tool_use",
            "tool_name": "Bash",
            "parameters": {"cmd": "ls"},
            "id": "upstream-abc-123",
        }
    )
    msg = parser.parse_line(line, 0)
    assert msg is not None
    assert msg.tool_use_id == "upstream-abc-123"


def test_two_synthetic_calls_in_one_message_get_distinct_ids() -> None:
    parser = GeminiTranscriptParser(session_id="sess-y")
    msg = _session_json_with_tool_calls()
    ids = [m.tool_use_id for m in parser._parse_session_message(msg, 0) if m.tool_use_id]
    # Two tool calls × (use + result) = 4 entries, but pairing means 2 distinct ids.
    assert len(set(ids)) == 2

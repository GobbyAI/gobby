"""Qwen transcript parser coverage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.sessions.transcripts.base import ParsedMessage, TokenUsage
from gobby.sessions.transcripts.qwen import QwenTranscriptParser
from gobby.sessions.transcripts.typed_json import TypedJsonTranscriptParser

pytestmark = pytest.mark.unit

FIXTURE_DIR = Path(__file__).parents[2] / "fixtures" / "transcripts" / "qwen"


def _load_json(path: str) -> Any:
    return json.loads((FIXTURE_DIR / path).read_text())


def _usage_dict(usage: TokenUsage | None) -> dict[str, int] | None:
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
    }


def _normalized(message: ParsedMessage) -> dict[str, Any]:
    return {
        "index": message.index,
        "role": message.role,
        "content": message.content,
        "content_type": message.content_type,
        "tool_name": message.tool_name,
        "tool_input": message.tool_input,
        "tool_result": message.tool_result,
        "usage": _usage_dict(message.usage),
        "tool_use_id": message.tool_use_id,
        "message_id": message.message_id,
        "timestamp": message.timestamp.isoformat(),
    }


def test_qwen_parser_uses_neutral_typed_json_base() -> None:
    assert issubclass(QwenTranscriptParser, TypedJsonTranscriptParser)


def test_qwen_jsonl_matches_golden_fixture() -> None:
    parser = QwenTranscriptParser(session_id="qwen-session")
    lines = (FIXTURE_DIR / "jsonl_transcript.jsonl").read_text().splitlines()

    actual = [_normalized(message) for message in parser.parse_lines(lines)]

    assert actual == _load_json("jsonl_expected.json")


def test_qwen_native_session_matches_golden_fixture() -> None:
    parser = QwenTranscriptParser(session_id="qwen-session")
    session = _load_json("session.json")

    actual = [_normalized(message) for message in parser.parse_session_json(session)]

    assert actual == _load_json("session_expected.json")


def test_qwen_synthetic_tool_ids_are_stable_and_qwen_prefixed() -> None:
    lines = [
        json.dumps({"type": "tool_use", "tool_name": "Bash", "parameters": {"cmd": "pwd"}}),
        json.dumps({"type": "tool_result", "output": "/tmp", "status": "success"}),
    ]
    first = QwenTranscriptParser(session_id="session-a").parse_lines(lines)
    second = QwenTranscriptParser(session_id="session-a").parse_lines(lines)

    first_ids = [msg.tool_use_id for msg in first if msg.tool_use_id is not None]
    second_ids = [msg.tool_use_id for msg in second if msg.tool_use_id is not None]

    assert first_ids == second_ids
    assert first_ids
    assert all(tool_use_id.startswith("qwen-tu-") for tool_use_id in first_ids)


def test_qwen_uses_gemini_compatible_usage_mapping() -> None:
    parser = QwenTranscriptParser()
    line = json.dumps(
        {
            "type": "message",
            "role": "model",
            "content": "Qwen response",
            "usageMetadata": {
                "promptTokenCount": 800,
                "cachedContentTokenCount": 300,
                "candidatesTokenCount": 60,
            },
        }
    )

    msg = parser.parse_line(line, 0)

    assert msg is not None
    assert msg.usage is not None
    assert msg.usage.input_tokens == 500
    assert msg.usage.cache_read_tokens == 300
    assert msg.usage.output_tokens == 60

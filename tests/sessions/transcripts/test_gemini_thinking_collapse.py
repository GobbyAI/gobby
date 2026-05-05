"""Tests for Gemini thoughts[] → single thinking block collapse (#14236)."""

from __future__ import annotations

import pytest

from gobby.sessions.transcripts.gemini import GeminiTranscriptParser
from gobby.sessions.transcripts.qwen import QwenTranscriptParser

pytestmark = pytest.mark.unit


@pytest.fixture(params=[GeminiTranscriptParser, QwenTranscriptParser])
def parser(request: pytest.FixtureRequest) -> GeminiTranscriptParser:
    cls = request.param
    return cls(session_id="s1")


def _msg(thoughts: list[dict[str, str]], content: str = "") -> dict[str, object]:
    return {"type": "gemini", "content": content, "thoughts": thoughts}


def test_multiple_thoughts_collapse_into_single_thinking_block(
    parser: GeminiTranscriptParser,
) -> None:
    msg = _msg(
        [
            {"subject": "Reading file", "description": "Need to inspect contents."},
            {"subject": "Considering plan", "description": "Two viable approaches."},
        ]
    )

    parsed = parser._parse_session_message(msg, start_index=0)

    thinking = [p for p in parsed if p.content_type == "thinking"]
    assert len(thinking) == 1
    assert "**Reading file**" in thinking[0].content
    assert "Need to inspect contents." in thinking[0].content
    assert "**Considering plan**" in thinking[0].content
    assert "Two viable approaches." in thinking[0].content


def test_subject_only_thought_renders_as_bold_heading(
    parser: GeminiTranscriptParser,
) -> None:
    msg = _msg([{"subject": "Quick check", "description": ""}])

    parsed = parser._parse_session_message(msg, start_index=0)

    thinking = [p for p in parsed if p.content_type == "thinking"]
    assert len(thinking) == 1
    assert thinking[0].content == "**Quick check**"


def test_description_only_thought_renders_as_body(
    parser: GeminiTranscriptParser,
) -> None:
    msg = _msg([{"subject": "", "description": "Pure reasoning."}])

    parsed = parser._parse_session_message(msg, start_index=0)

    thinking = [p for p in parsed if p.content_type == "thinking"]
    assert len(thinking) == 1
    assert thinking[0].content == "Pure reasoning."


def test_no_subject_text_block_emitted_for_thoughts(
    parser: GeminiTranscriptParser,
) -> None:
    msg = _msg(
        [{"subject": "Reading file", "description": "Inspecting."}],
        content="",
    )

    parsed = parser._parse_session_message(msg, start_index=0)

    assert all(p.content != "Reading file" for p in parsed)
    assert all(p.content_type != "text" or p.content == "" for p in parsed)


def test_empty_thoughts_emits_nothing(parser: GeminiTranscriptParser) -> None:
    msg = _msg([{"subject": "", "description": ""}])

    parsed = parser._parse_session_message(msg, start_index=0)

    assert [p for p in parsed if p.content_type == "thinking"] == []


def test_thoughts_then_text_response_preserves_main_text(
    parser: GeminiTranscriptParser,
) -> None:
    msg = _msg(
        [{"subject": "Plan", "description": "Will summarize."}],
        content="Here's the answer.",
    )

    parsed = parser._parse_session_message(msg, start_index=0)

    types = [p.content_type for p in parsed]
    assert types.count("thinking") == 1
    assert any(p.content_type == "text" and p.content == "Here's the answer." for p in parsed)


def test_thoughts_with_tool_calls_emits_one_thinking_then_tool_use(
    parser: GeminiTranscriptParser,
) -> None:
    msg: dict[str, object] = {
        "type": "gemini",
        "content": "",
        "thoughts": [
            {"subject": "Decide tool", "description": "Bash fits."},
            {"subject": "Form command", "description": "Use ls."},
        ],
        "toolCalls": [
            {"name": "Bash", "args": {"cmd": "ls"}, "result": []},
        ],
    }

    parsed = parser._parse_session_message(msg, start_index=0)

    types = [p.content_type for p in parsed]
    assert types.count("thinking") == 1
    assert types.index("thinking") < types.index("tool_use")

import json

import pytest

from gobby.sessions.transcripts.typed_json import TypedJsonTranscriptParser

pytestmark = pytest.mark.unit


def test_parse_session_json_skips_malformed_items_and_preserves_empty_tool_result() -> None:
    parser = TypedJsonTranscriptParser(cli_name="test")

    messages = parser.parse_session_json(
        {
            "messages": [
                None,
                {
                    "type": "gemini",
                    "timestamp": "2026-06-16T12:00:00Z",
                    "thoughts": [None, {"description": "nice plan"}],
                    "content": "done",
                    "toolCalls": [
                        None,
                        {
                            "id": "call-1",
                            "name": "empty_tool",
                            "args": {},
                            "result": [{"functionResponse": {}}],
                        },
                    ],
                },
            ],
        }
    )

    assert [message.content_type for message in messages] == [
        "thinking",
        "text",
        "tool_use",
        "tool_result",
    ]
    assert messages[0].content == "nice plan"
    assert messages[3].tool_result == {"output": {}, "status": "success"}
    assert messages[3].tool_use_id == messages[2].tool_use_id


def test_parse_line_inline_function_call_assigns_tool_use_id() -> None:
    parser = TypedJsonTranscriptParser(cli_name="test")
    line = json.dumps(
        {
            "type": "message",
            "role": "model",
            "content": [
                {"text": "checking"},
                {"functionCall": {"id": "inline-1", "name": "lookup", "args": {"q": "x"}}},
            ],
        }
    )

    message = parser.parse_line(line, 7)

    assert message is not None
    assert message.content_type == "tool_use"
    assert message.tool_name == "lookup"
    assert message.tool_input == {"q": "x"}
    assert message.tool_use_id == "inline-1"

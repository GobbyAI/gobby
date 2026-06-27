import json

import pytest

from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.sessions.transcripts.droid import DroidTranscriptParser
from gobby.sessions.transcripts.grok import GrokTranscriptParser
from gobby.sessions.transcripts.typed_json import TypedJsonTranscriptParser

pytestmark = pytest.mark.unit


def _line(data: dict[str, object]) -> str:
    return json.dumps(data)


def test_codex_unknown_response_item_is_preserved():
    raw = {
        "timestamp": "2026-06-27T15:00:00Z",
        "type": "response_item",
        "payload": {"type": "new_block", "id": "payload-1", "meta": True},
    }

    msg = CodexTranscriptParser(session_id="session-1").parse_line(_line(raw), 3)

    assert msg is not None
    assert msg.content_type == "response_item/new_block"
    assert msg.raw_json == raw["payload"]
    assert msg.message_id == "payload-1"


def test_droid_unknown_blocks_keep_source_order():
    raw = {
        "type": "message",
        "id": "msg-1",
        "timestamp": "2026-06-27T15:00:00Z",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "before"},
                {"type": "mystery", "content": "middle"},
                {"type": "tool_use", "id": "tool-1", "name": "lookup", "input": {}},
            ],
        },
    }

    messages = DroidTranscriptParser(session_id="session-1").parse_lines([_line(raw)])

    assert [msg.content_type for msg in messages] == ["text", "mystery", "tool_use"]
    assert [msg.content for msg in messages] == ["before", "middle", ""]
    assert messages[1].raw_json == {"type": "mystery", "content": "middle"}


def test_unknown_records_are_preserved_but_protocol_records_stay_skipped():
    unknown = {"type": "agent_event", "timestamp": "2026-06-27T15:00:00Z", "content": "kept"}
    session_start = {"type": "session_start", "timestamp": "2026-06-27T15:00:00Z"}
    todo_state = {"type": "todo_state", "timestamp": "2026-06-27T15:00:00Z"}
    droid_parser = DroidTranscriptParser(session_id="session-1")
    codex_parser = CodexTranscriptParser(session_id="session-1")
    typed_parser = TypedJsonTranscriptParser(cli_name="typed", session_id="session-1")

    messages = droid_parser.parse_lines([_line(unknown), _line(session_start), _line(todo_state)])

    assert [msg.content_type for msg in messages] == ["agent_event", "tool_use", "tool_result"]
    assert messages[0].content == "kept"
    assert messages[1].tool_name == "TodoWrite"
    assert messages[2].tool_result == {"todos": [], "source": "todo_state"}
    assert (
        codex_parser.parse_line(
            _line({"type": "response_item", "payload": {"type": "reasoning"}}), 0
        )
        is None
    )
    assert typed_parser.parse_line(_line({"type": "init"}), 0) is None
    assert typed_parser.parse_line(_line({"type": "result"}), 1) is None


def test_typed_json_unknown_jsonl_and_session_messages_are_preserved():
    parser = TypedJsonTranscriptParser(cli_name="typed", session_id="session-1")

    jsonl_msg = parser.parse_line(
        _line(
            {
                "type": "provider_extension",
                "id": "evt-1",
                "timestamp": "2026-06-27T15:00:00Z",
                "content": "extension body",
            }
        ),
        4,
    )
    session_messages = parser.parse_session_json(
        {
            "messages": [
                {"type": "info", "content": "skip me"},
                {
                    "type": "provider_session_extension",
                    "id": "msg-1",
                    "timestamp": "2026-06-27T15:00:00Z",
                    "content": "session body",
                },
            ]
        }
    )

    assert jsonl_msg is not None
    assert jsonl_msg.content_type == "provider_extension"
    assert jsonl_msg.content == "extension body"
    assert [msg.content_type for msg in session_messages] == ["provider_session_extension"]
    assert session_messages[0].content == "session body"


def test_grok_unknown_session_update_is_preserved():
    raw = {
        "timestamp": "2026-06-27T15:00:00Z",
        "update": {
            "sessionUpdate": "new_update",
            "messageId": "msg-1",
            "content": "new update body",
        },
    }

    msg = GrokTranscriptParser(session_id="session-1").parse_line(_line(raw), 5)

    assert msg is not None
    assert msg.content_type == "new_update"
    assert msg.content == "new update body"
    assert msg.raw_json == raw["update"]


def test_claude_unknown_block_keeps_source_order():
    raw = {
        "type": "assistant",
        "timestamp": "2026-06-27T15:00:00Z",
        "message": {
            "content": [
                {"type": "text", "text": "before"},
                {"type": "mystery", "content": "middle"},
                {"type": "tool_use", "id": "tool-1", "name": "lookup", "input": {}},
            ]
        },
    }

    messages = ClaudeTranscriptParser(session_id="session-1").parse_lines([_line(raw)])

    assert [msg.content_type for msg in messages] == ["text", "mystery", "tool_use"]
    assert [msg.content for msg in messages] == ["before", "middle", ""]
    assert messages[1].raw_json == {"type": "mystery", "content": "middle"}

import json
from pathlib import Path

import pytest

from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.sessions.transcripts.droid import DroidTranscriptParser
from gobby.sessions.transcripts.grok import GrokTranscriptParser

pytestmark = pytest.mark.unit


def _line(data: dict[str, object]) -> str:
    return json.dumps(data)


def test_codex_unknown_response_item_is_preserved() -> None:
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


def test_droid_unknown_blocks_keep_source_order() -> None:
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


def test_droid_unknown_assistant_block_receives_sidecar_usage(tmp_path: Path) -> None:
    transcript_path = tmp_path / "droid.jsonl"
    transcript_path.with_suffix(".settings.json").write_text(
        json.dumps(
            {
                "model": "droid-model",
                "tokenUsage": {
                    "inputTokens": 11,
                    "outputTokens": 7,
                    "cacheCreationTokens": 3,
                    "cacheReadTokens": 5,
                },
            }
        ),
        encoding="utf-8",
    )
    raw = {
        "type": "message",
        "id": "msg-unknown",
        "timestamp": "2026-06-27T15:00:00Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "mystery", "content": "only unknown"}],
        },
    }

    messages = DroidTranscriptParser(
        session_id="session-1",
        transcript_path=transcript_path,
    ).parse_lines([_line(raw)])

    assert len(messages) == 1
    msg = messages[0]
    assert msg.content_type == "mystery"
    assert msg.model == "droid-model"
    assert msg.usage is not None
    assert msg.usage.input_tokens == 11
    assert msg.usage.output_tokens == 7
    assert msg.usage.cache_creation_tokens == 3
    assert msg.usage.cache_read_tokens == 5


def test_unknown_records_are_preserved_but_protocol_records_stay_skipped() -> None:
    unknown = {"type": "agent_event", "timestamp": "2026-06-27T15:00:00Z", "content": "kept"}
    session_start = {"type": "session_start", "timestamp": "2026-06-27T15:00:00Z"}
    todo_state = {"type": "todo_state", "timestamp": "2026-06-27T15:00:00Z"}
    droid_parser = DroidTranscriptParser(session_id="session-1")
    codex_parser = CodexTranscriptParser(session_id="session-1")

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


def test_grok_unknown_session_update_is_preserved() -> None:
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


def test_claude_unknown_block_keeps_source_order() -> None:
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


def test_claude_unknown_block_flushes_text_before_thinking() -> None:
    raw = {
        "type": "assistant",
        "timestamp": "2026-06-27T15:00:00Z",
        "message": {
            "content": [
                {"type": "text", "text": "visible"},
                {"type": "thinking", "thinking": "hidden"},
                {"type": "mystery", "content": "middle"},
            ]
        },
    }

    messages = ClaudeTranscriptParser(session_id="session-1").parse_lines([_line(raw)])

    assert [msg.content_type for msg in messages] == ["text", "thinking", "mystery"]
    assert [msg.content for msg in messages] == ["visible", "hidden", "middle"]

"""Tests for Grok updates.jsonl transcript parsing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.memory.digest import _extract_digest_pairs
from gobby.sessions.transcript_normalization import normalize_transcript_records
from gobby.sessions.transcripts.base import (
    NON_MESSAGE_CONTENT_TYPES,
    RENDER_SKIP_CONTENT_TYPES,
    ParsedMessage,
)
from gobby.sessions.transcripts.grok import GrokTranscriptParser

pytestmark = pytest.mark.unit


def _event(update: dict[str, object]) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": "grok-session", "update": update},
            "timestamp": "2026-05-22T22:22:00Z",
        }
    )


def _record(update: dict[str, object]) -> dict[str, object]:
    parsed = json.loads(_event(update))
    assert isinstance(parsed, dict)
    return parsed


def _user_chunk(text: str) -> dict[str, object]:
    return _record(
        {
            "sessionUpdate": "user_message_chunk",
            "content": {"type": "text", "text": text},
        }
    )


def _agent_chunk(text: str) -> dict[str, object]:
    return _record(
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text},
        }
    )


def _thought_chunk(text: str) -> dict[str, object]:
    return _record(
        {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": text},
        }
    )


@pytest.mark.parametrize(
    "update_type",
    [
        "retry_state",
        "compaction_checkpoint",
        "auto_compact_completed",
        "task_backgrounded",
        "task_completed",
        "current_mode_update",
        "hook_annotation",
    ],
)
def test_grok_protocol_metadata_records_are_suppressed(update_type: str) -> None:
    parser = GrokTranscriptParser(session_id="grok-session")
    line = _event({"sessionUpdate": update_type})

    parsed = parser.parse_line(line, 0)
    assert parsed is None
    assert parser.parse_lines([line]) == []


def _parsed_messages(parser: GrokTranscriptParser, lines: list[str]) -> list[ParsedMessage]:
    messages: list[ParsedMessage] = []
    for item in parser.parse_lines(lines):
        assert isinstance(item, ParsedMessage)
        messages.append(item)
    return messages


def _turn_completed_record(
    *,
    prompt_id: str | None = "prompt-1",
    usage: dict[str, object] | None = None,
    include_usage: bool = True,
) -> dict[str, object]:
    update: dict[str, object] = {
        "sessionUpdate": "turn_completed",
        "stop_reason": "end_turn",
        "modelCalls": 2,
        "reasoningTokens": 7,
        "modelUsage": {"grok-4": {"inputTokens": 100}},
    }
    if prompt_id is not None:
        update["prompt_id"] = prompt_id
    if include_usage:
        update["usage"] = (
            {
                "inputTokens": 100,
                "outputTokens": 20,
                "cachedReadTokens": 30,
                "cacheCreationTokens": 10,
                "totalTokens": 120,
                "reasoningTokens": 7,
                "modelCalls": 2,
            }
            if usage is None
            else usage
        )
    return {
        "timestamp": 1784250127,
        "method": "_x.ai/session/update",
        "params": {
            "sessionId": "grok-session",
            "update": update,
            "_meta": {"eventId": "grok-session-216", "agentTimestampMs": 1784250127054},
        },
    }


def test_grok_turn_completed_maps_turn_aggregate_usage() -> None:
    parser = GrokTranscriptParser(session_id="grok-session")
    record = _turn_completed_record()
    parsed = parser.parse_line(json.dumps(record), 4)

    assert isinstance(parsed, ParsedMessage)
    assert parsed.role == "assistant"
    assert parsed.content == ""
    assert parsed.content_type == "turn_completed"
    assert parsed.message_id is not None
    assert "prompt-1" in parsed.message_id
    assert parsed.raw_json == record
    assert parsed.usage is not None
    # inputTokens 100 minus cachedReadTokens 30 minus cacheCreationTokens 10.
    assert parsed.usage.input_tokens == 60
    assert parsed.usage.output_tokens == 20
    assert parsed.usage.cache_read_tokens == 30
    assert parsed.usage.cache_creation_tokens == 10


def test_grok_turn_completed_without_usage_still_emits_boundary() -> None:
    parser = GrokTranscriptParser(session_id="grok-session")
    record = _turn_completed_record(include_usage=False)
    parsed = parser.parse_line(json.dumps(record), 0)

    assert isinstance(parsed, ParsedMessage)
    assert parsed.content_type == "turn_completed"
    assert parsed.content == ""
    assert parsed.usage is None
    assert parsed.raw_json == record


def test_grok_turn_completed_missing_prompt_id_uses_index_message_id() -> None:
    parser = GrokTranscriptParser(session_id="grok-session")
    record = _turn_completed_record(prompt_id=None, include_usage=False)
    parsed = parser.parse_line(json.dumps(record), 9)

    assert isinstance(parsed, ParsedMessage)
    assert parsed.message_id == "grok-session:grok:9"


def test_grok_turn_completed_is_render_skip_excluded_from_message_count() -> None:
    assert "turn_completed" in RENDER_SKIP_CONTENT_TYPES
    assert "turn_completed" in NON_MESSAGE_CONTENT_TYPES


def test_grok_updates_jsonl_parser_renders_message_and_tool_records() -> None:
    parser = GrokTranscriptParser(session_id="grok-session")
    lines = [
        _event(
            {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": "Run pwd"},
            }
        ),
        _event(
            {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "Need current directory."},
            }
        ),
        _event(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "Checking."},
            }
        ),
        _event(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "call-grok-tool-0",
                "title": "run_terminal_command",
                "rawInput": {"command": "pwd", "timeout": 30000},
            }
        ),
        _event(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call-grok-tool-0",
                "status": "in_progress",
                "content": [{"type": "content", "content": {"type": "text", "text": "Running"}}],
            }
        ),
        _event(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call-grok-tool-0",
                "status": "completed",
                "content": [{"type": "content", "content": {"type": "text", "text": "/repo"}}],
            }
        ),
    ]

    messages = _parsed_messages(parser, lines)

    assert [message.role for message in messages] == [
        "user",
        "assistant",
        "assistant",
        "assistant",
        "tool",
    ]
    assert [message.content_type for message in messages] == [
        "text",
        "thinking",
        "text",
        "tool_use",
        "tool_result",
    ]
    assert messages[0].content == "Run pwd"
    assert messages[1].content == "Need current directory."
    assert messages[3].tool_name == "run_terminal_command"
    assert messages[3].tool_input == {"command": "pwd", "timeout": 30000}
    assert messages[4].tool_use_id == "call-grok-tool-0"
    assert messages[4].tool_result == {
        "output": "/repo",
        "error": None,
        "raw": {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call-grok-tool-0",
            "status": "completed",
            "content": [{"type": "content", "content": {"type": "text", "text": "/repo"}}],
        },
    }


def test_grok_successful_hook_execution_without_output_is_suppressed() -> None:
    parser = GrokTranscriptParser(session_id="grok-session")
    records = normalize_transcript_records(
        parser.parse_lines(
            [
                _event(
                    {
                        "sessionUpdate": "hook_execution",
                        "hookName": "PostToolUse",
                        "status": "success",
                    }
                )
            ]
        ),
        "grok",
    )

    assert records == []


def test_grok_failed_hook_execution_renders_system_text_not_unknown() -> None:
    parser = GrokTranscriptParser(session_id="grok-session")
    records = normalize_transcript_records(
        parser.parse_lines(
            [
                _event(
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "Checking."},
                    }
                ),
                _event(
                    {
                        "sessionUpdate": "hook_execution",
                        "hookName": "PostToolUse",
                        "status": "failed",
                        "message": "Policy blocked the command.",
                    }
                ),
            ]
        ),
        "grok",
    )
    messages = [record for record in records if isinstance(record, ParsedMessage)]

    assert messages[-1].role == "system"
    assert messages[-1].content_type == "text"
    assert messages[-1].content == "Policy blocked the command."


def test_grok_hook_execution_ignores_content_beyond_collect_depth() -> None:
    parser = GrokTranscriptParser(session_id="grok-session")
    content: object = "deep output"
    for _ in range(60):
        content = {"content": content}
    records = normalize_transcript_records(
        parser.parse_lines(
            [
                _event(
                    {
                        "sessionUpdate": "hook_execution",
                        "hookName": "PostToolUse",
                        "status": "failed",
                        "content": content,
                    }
                ),
            ]
        ),
        "grok",
    )
    messages = [record for record in records if isinstance(record, ParsedMessage)]

    assert messages[-1].content == "PostToolUse hook execution: failed\n[truncated]"
    assert "deep output" not in messages[-1].content


def test_grok_usage_aggregates_nested_cache_details() -> None:
    parser = GrokTranscriptParser(session_id="grok-session")
    message = parser.parse_line(
        _event(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "Done"},
                "usage": {
                    "inputTokens": 100,
                    "outputTokens": 7,
                    "inputTokenDetails": {
                        "cachedTokens": 11,
                        "cacheCreationTokens": 3,
                    },
                    "prompt_tokens_details": {
                        "cached_tokens": 5,
                        "cache_creation_input_tokens": 2,
                    },
                    "cacheCreationInputTokensDetails": {
                        "ephemeral5mInputTokens": 4,
                        "ephemeral_1h_input_tokens": 6,
                    },
                },
            }
        ),
        0,
    )

    assert message is not None
    assert message.usage is not None
    # input = 100 - (11 + 5 cached reads) - (3 + 2 + 4 + 6 cache creations);
    # cache_read = 11 + 5, cache_creation = 3 + 2 + 4 + 6.
    assert message.usage.input_tokens == 69
    assert message.usage.output_tokens == 7
    assert message.usage.cache_read_tokens == 16
    assert message.usage.cache_creation_tokens == 15


def test_grok_extract_last_messages_turn_keyed_pairs() -> None:
    parser = GrokTranscriptParser(session_id="grok-session")
    tool_call = _record(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "call-1",
            "title": "run_terminal_command",
            "rawInput": {"command": "pwd"},
        }
    )
    turns = [
        _user_chunk("first prompt"),
        _thought_chunk("planning"),
        _agent_chunk("Hello"),
        tool_call,
        _agent_chunk(" world"),
        _turn_completed_record(),
        _user_chunk("second prompt"),
        _agent_chunk("Do"),
        _agent_chunk("ne."),
        _turn_completed_record(),
    ]

    messages = parser.extract_last_messages(turns, num_pairs=len(turns))

    assert messages == [
        {"role": "user", "content": "first prompt"},
        {"role": "assistant", "content": "Hello world"},
        {"role": "user", "content": "second prompt"},
        {"role": "assistant", "content": "Done."},
    ]
    assert parser.extract_last_messages(turns, num_pairs=1) == [
        {"role": "user", "content": "second prompt"},
        {"role": "assistant", "content": "Done."},
    ]
    assert _extract_digest_pairs(parser, turns) == [
        ("first prompt", "Hello world"),
        ("second prompt", "Done."),
    ]


def test_grok_marathon_turn_sub_segmentation() -> None:
    parser = GrokTranscriptParser(session_id="grok-session")
    seg1_a = "a" * 2000
    seg1_b = "b" * 2000
    oversized = "c" * 5000
    tail = "d" * 2079
    turns = [
        _user_chunk("marathon prompt"),
        _agent_chunk(seg1_a),
        _agent_chunk(seg1_b),
        _agent_chunk(oversized),
        _agent_chunk(tail),
        _turn_completed_record(),
    ]

    messages = parser.extract_last_messages(turns, num_pairs=len(turns))

    assert messages == [
        {"role": "user", "content": "marathon prompt"},
        {"role": "assistant", "content": seg1_a + seg1_b},
    ]
    assert [len(msg["content"]) for msg in messages[1:]] == [4000]
    assert _extract_digest_pairs(parser, turns) == [
        ("marathon prompt", seg1_a + seg1_b),
    ]


def test_grok_turn_segments_split_on_type_field_turn_completed() -> None:
    parser = GrokTranscriptParser(session_id="grok-session")
    turns = [
        _user_chunk("first"),
        _agent_chunk("one"),
        {
            "timestamp": 1784250127,
            "method": "_x.ai/session/update",
            "params": {
                "sessionId": "grok-session",
                "update": {"type": "turn_completed", "stop_reason": "end_turn"},
            },
        },
        _user_chunk("second"),
        _agent_chunk("two"),
    ]

    assert parser.extract_last_messages(turns, num_pairs=1) == [
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "two"},
    ]


def test_grok_pair_budget_truncates_overflowing_chunk() -> None:
    parser = GrokTranscriptParser(session_id="grok-session")
    first = "a" * 3000
    overflow = "b" * 2000
    turns = [
        _user_chunk("prompt"),
        _agent_chunk(first),
        _agent_chunk(overflow),
    ]
    messages = parser.extract_last_messages(turns, num_pairs=2)
    assert messages == [
        {"role": "user", "content": "prompt"},
        {"role": "assistant", "content": first + ("b" * 1000)},
    ]


def test_grok_mid_turn_injection_anchoring() -> None:
    parser = GrokTranscriptParser(session_id="grok-session")
    injection = "The user sent a message while you were working"
    turns = [
        _user_chunk("do the work"),
        _agent_chunk("started "),
        _agent_chunk("more"),
        _user_chunk(injection),
        _agent_chunk("acknowledged"),
        _turn_completed_record(),
    ]

    messages = parser.extract_last_messages(turns, num_pairs=len(turns))

    assert messages == [
        {"role": "user", "content": "do the work"},
        {"role": "assistant", "content": "started more"},
        {"role": "user", "content": injection},
        {"role": "assistant", "content": "acknowledged"},
    ]
    assert _extract_digest_pairs(parser, turns) == [
        ("do the work", "started more"),
        (injection, "acknowledged"),
    ]


def test_grok_open_and_cancelled_turn_pairs() -> None:
    parser = GrokTranscriptParser(session_id="grok-session")
    turns = [
        _user_chunk("cancelled prompt"),
        _turn_completed_record(),
        _user_chunk("finished prompt"),
        _agent_chunk("all done"),
        _turn_completed_record(),
        _user_chunk("in flight"),
        _agent_chunk("part"),
        _agent_chunk("ial"),
    ]

    messages = parser.extract_last_messages(turns, num_pairs=len(turns))

    assert messages == [
        {"role": "user", "content": "cancelled prompt"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "finished prompt"},
        {"role": "assistant", "content": "all done"},
        {"role": "user", "content": "in flight"},
        {"role": "assistant", "content": "partial"},
    ]
    assert parser.extract_last_messages(turns, num_pairs=1) == [
        {"role": "user", "content": "in flight"},
        {"role": "assistant", "content": "partial"},
    ]
    assert parser.extract_last_messages(turns[:2], num_pairs=1) == [
        {"role": "user", "content": "cancelled prompt"},
        {"role": "assistant", "content": ""},
    ]
    assert _extract_digest_pairs(parser, turns) == [
        ("cancelled prompt", ""),
        ("finished prompt", "all done"),
        ("in flight", "partial"),
    ]


def test_extract_last_messages_tool_activity_ledger() -> None:
    fixture = Path(__file__).parent / "fixtures" / "grok_audit" / "10711" / "updates.jsonl"
    turns = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines()]

    messages = GrokTranscriptParser().extract_last_messages(
        turns,
        num_pairs=len(turns),
        include_tool_activity=True,
    )
    ledgers = "\n".join(
        str(message["tool_activity"]) for message in messages if "tool_activity" in message
    )

    assert "- search_replace /repo/widget.py" in ledgers
    assert "- search_replace" in ledgers
    assert "- mcp gobby-tasks:claim_task task_id=#20728" in ledgers
    assert "- Bash uv run pytest -k widget ! failed: exit 1" in ledgers
    assert "run_terminal_command" not in ledgers

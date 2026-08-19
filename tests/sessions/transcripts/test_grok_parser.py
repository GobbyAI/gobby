"""Tests for Grok updates.jsonl transcript parsing."""

from __future__ import annotations

import json

import pytest

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

    messages = parser.parse_lines(lines)

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

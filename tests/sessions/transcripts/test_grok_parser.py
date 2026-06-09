"""Tests for Grok updates.jsonl transcript parsing."""

from __future__ import annotations

import json

import pytest

from gobby.sessions.transcript_normalization import normalize_transcript_records
from gobby.sessions.transcript_renderer import render_transcript
from gobby.sessions.transcripts.base import ParsedMessage
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
    rendered = render_transcript([], session_id="grok-session", error_log=parser.error_log)
    assert rendered == []


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

    rendered = render_transcript(messages, session_id="grok-session", error_log=parser.error_log)
    block_types = [block.type for message in rendered for block in message.content_blocks]

    assert "unknown" not in block_types
    assert rendered[-1].role == "system"
    assert rendered[-1].content_blocks[0].type == "text"
    assert rendered[-1].content_blocks[0].content == "Policy blocked the command."


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

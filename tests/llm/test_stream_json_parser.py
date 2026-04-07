"""Tests for stream JSON parser — Claude CLI stream-json NDJSON output.

Covers: init, text delta, thinking delta, tool_use, rate_limit, result,
error, malformed input, empty lines.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from gobby.llm.stream_json_parser import (
    ContentBlockDelta,
    ErrorEvent,
    InitEvent,
    MessageComplete,
    RateLimitEvent,
    ResultEvent,
    StreamEvent,
    _classify_event,
    parse_stream,
)

pytestmark = pytest.mark.unit


# ─── Fixtures ──────────────────────────────────────────────────────────


def _make_reader(lines: list[str]) -> asyncio.StreamReader:
    """Create a StreamReader pre-loaded with NDJSON lines."""
    reader = asyncio.StreamReader()
    for line in lines:
        reader.feed_data((line + "\n").encode())
    reader.feed_eof()
    return reader


# ─── _classify_event tests ─────────────────────────────────────────────


class TestClassifyEvent:
    def test_init_event(self) -> None:
        raw: dict[str, Any] = {
            "type": "system",
            "subtype": "init",
            "session_id": "sess-abc",
            "model": "claude-sonnet-4-6",
        }
        event = _classify_event(raw)
        assert isinstance(event, InitEvent)
        assert event.session_id == "sess-abc"
        assert event.model == "claude-sonnet-4-6"
        assert event.raw is raw

    def test_text_delta(self) -> None:
        raw: dict[str, Any] = {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Hello world"}],
            },
            "delta": True,
        }
        event = _classify_event(raw)
        assert isinstance(event, ContentBlockDelta)
        assert event.block_type == "text"
        assert event.text == "Hello world"

    def test_thinking_delta(self) -> None:
        raw: dict[str, Any] = {
            "type": "assistant",
            "message": {
                "content": [{"type": "thinking", "text": "Let me think..."}],
            },
            "delta": True,
        }
        event = _classify_event(raw)
        assert isinstance(event, ContentBlockDelta)
        assert event.block_type == "thinking"
        assert event.text == "Let me think..."

    def test_tool_use_delta(self) -> None:
        raw: dict[str, Any] = {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/tmp/test.py"},
                    }
                ],
            },
            "delta": True,
        }
        event = _classify_event(raw)
        assert isinstance(event, ContentBlockDelta)
        assert event.block_type == "tool_use"
        assert event.tool_name == "Read"
        assert event.tool_input == {"file_path": "/tmp/test.py"}

    def test_rate_limit_event(self) -> None:
        raw: dict[str, Any] = {
            "type": "rate_limit_event",
            "retry_after": 2.5,
        }
        event = _classify_event(raw)
        assert isinstance(event, RateLimitEvent)
        assert event.retry_after == 2.5

    def test_result_event(self) -> None:
        raw: dict[str, Any] = {
            "type": "result",
            "cost_usd": 0.003,
            "input_tokens": 100,
            "output_tokens": 50,
            "session_id": "sess-xyz",
        }
        event = _classify_event(raw)
        assert isinstance(event, ResultEvent)
        assert event.cost_usd == 0.003
        assert event.input_tokens == 100
        assert event.output_tokens == 50
        assert event.session_id == "sess-xyz"

    def test_error_event(self) -> None:
        raw: dict[str, Any] = {
            "type": "error",
            "error": {"message": "Rate limited", "is_fatal": False},
        }
        event = _classify_event(raw)
        assert isinstance(event, ErrorEvent)
        assert event.message == "Rate limited"
        assert event.is_fatal is False

    def test_fatal_error_event(self) -> None:
        raw: dict[str, Any] = {
            "type": "error",
            "error": {"message": "Auth failed", "is_fatal": True},
        }
        event = _classify_event(raw)
        assert isinstance(event, ErrorEvent)
        assert event.is_fatal is True

    def test_message_complete(self) -> None:
        raw: dict[str, Any] = {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Done."}],
                "stop_reason": "end_turn",
            },
        }
        event = _classify_event(raw)
        assert isinstance(event, MessageComplete)
        assert event.stop_reason == "end_turn"
        assert len(event.content) == 1

    def test_unknown_type_returns_base_stream_event(self) -> None:
        raw: dict[str, Any] = {"type": "system", "subtype": "hook_start"}
        event = _classify_event(raw)
        assert type(event) is StreamEvent


# ─── parse_stream tests ───────────────────────────────────────────────


class TestParseStream:
    @pytest.mark.asyncio
    async def test_parses_init_event(self) -> None:
        lines = [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "s1",
                    "model": "claude-sonnet-4-6",
                }
            ),
        ]
        reader = _make_reader(lines)
        events = [event async for event in parse_stream(reader)]
        assert len(events) == 1
        assert isinstance(events[0], InitEvent)

    @pytest.mark.asyncio
    async def test_skips_empty_lines(self) -> None:
        lines = [
            "",
            json.dumps(
                {
                    "type": "result",
                    "cost_usd": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "session_id": "s1",
                }
            ),
            "",
            "",
        ]
        reader = _make_reader(lines)
        events = [event async for event in parse_stream(reader)]
        assert len(events) == 1
        assert isinstance(events[0], ResultEvent)

    @pytest.mark.asyncio
    async def test_skips_malformed_json(self) -> None:
        lines = [
            "not valid json {{{",
            json.dumps(
                {
                    "type": "result",
                    "cost_usd": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "session_id": "s1",
                }
            ),
        ]
        reader = _make_reader(lines)
        events = [event async for event in parse_stream(reader)]
        assert len(events) == 1
        assert isinstance(events[0], ResultEvent)

    @pytest.mark.asyncio
    async def test_full_stream_sequence(self) -> None:
        """Parse a realistic init → delta → result sequence."""
        lines = [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "s1",
                    "model": "claude-sonnet-4-6",
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "Hi"}]},
                    "delta": True,
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "cost_usd": 0.001,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "session_id": "s1",
                }
            ),
        ]
        reader = _make_reader(lines)
        events = [event async for event in parse_stream(reader)]
        assert len(events) == 3
        assert isinstance(events[0], InitEvent)
        assert isinstance(events[1], ContentBlockDelta)
        assert isinstance(events[2], ResultEvent)

    @pytest.mark.asyncio
    async def test_empty_stream(self) -> None:
        reader = _make_reader([])
        events = [event async for event in parse_stream(reader)]
        assert events == []

"""Stream JSON parser for Claude CLI --output-format stream-json output.

Parses NDJSON lines from Claude CLI stdout into typed StreamEvent objects.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# StreamEvent hierarchy
# ---------------------------------------------------------------------------


@dataclass
class StreamEvent:
    """Base class for all stream events."""

    raw: dict[str, Any] = field(repr=False)


@dataclass
class InitEvent(StreamEvent):
    """system/init -- session initialized."""

    session_id: str = ""
    model: str = ""


@dataclass
class ContentBlockDelta(StreamEvent):
    """Content block delta -- text, thinking, or tool_use."""

    block_type: str = ""  # "text", "thinking", "tool_use"
    text: str = ""
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None


@dataclass
class MessageComplete(StreamEvent):
    """Full assistant message complete."""

    content: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""


@dataclass
class RateLimitEvent(StreamEvent):
    """Rate limit info from the stream."""

    retry_after: float = 0.0


@dataclass
class ResultEvent(StreamEvent):
    """Final result with usage stats."""

    input_tokens: int = 0
    output_tokens: int = 0
    session_id: str = ""


@dataclass
class ErrorEvent(StreamEvent):
    """Error during streaming."""

    message: str = ""
    is_fatal: bool = False


# ---------------------------------------------------------------------------
# Event classification
# ---------------------------------------------------------------------------


def _classify_event(data: dict[str, Any]) -> StreamEvent:
    """Classify a parsed JSON dict into the appropriate StreamEvent subclass."""
    event_type = data.get("type", "")

    if event_type == "system" and data.get("subtype") == "init":
        return InitEvent(
            raw=data,
            session_id=data.get("session_id", ""),
            model=data.get("model", ""),
        )

    if event_type == "assistant":
        message = data.get("message", {})
        content_blocks = message.get("content", data.get("content", []))

        if data.get("delta"):
            # Delta event -- extract first content block info
            block_type = ""
            text = ""
            tool_name: str | None = None
            tool_input: dict[str, Any] | None = None

            if content_blocks:
                block = content_blocks[0]
                block_type = block.get("type", "")
                text = block.get("text", "")
                tool_name = block.get("tool_name") or block.get("name")
                raw_input = block.get("tool_input") or block.get("input")
                if raw_input is not None:
                    tool_input = raw_input if isinstance(raw_input, dict) else None

            return ContentBlockDelta(
                raw=data,
                block_type=block_type,
                text=text,
                tool_name=tool_name,
                tool_input=tool_input,
            )

        # Full message complete
        return MessageComplete(
            raw=data,
            content=content_blocks,
            stop_reason=message.get("stop_reason", data.get("stop_reason", "")),
        )

    if event_type == "rate_limit_event":
        return RateLimitEvent(
            raw=data,
            retry_after=float(data.get("retry_after", 0.0)),
        )

    if event_type == "result":
        # Tokens may be at top level or nested under usage
        usage = data.get("usage", {})
        return ResultEvent(
            raw=data,
            input_tokens=int(data.get("input_tokens", usage.get("input_tokens", 0))),
            output_tokens=int(data.get("output_tokens", usage.get("output_tokens", 0))),
            session_id=data.get("session_id", ""),
        )

    if event_type == "error":
        error = data.get("error", {})
        return ErrorEvent(
            raw=data,
            message=error.get("message", data.get("message", "")),
            is_fatal=bool(error.get("is_fatal", False)),
        )

    # Unknown event type -- return base StreamEvent
    return StreamEvent(raw=data)


# ---------------------------------------------------------------------------
# Async stream iterator
# ---------------------------------------------------------------------------


async def parse_stream(reader: asyncio.StreamReader) -> AsyncIterator[StreamEvent]:
    """Parse NDJSON lines from Claude CLI stdout into StreamEvents.

    Args:
        reader: An asyncio.StreamReader (typically from a subprocess stdout).

    Yields:
        Typed StreamEvent instances for each valid JSON line.
    """
    while True:
        raw_line = await reader.readline()
        if not raw_line:
            break
        line = raw_line.decode().strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.debug("Skipping malformed stream JSON line: %s (%s)", line[:200], exc)
            continue
        yield _classify_event(data)

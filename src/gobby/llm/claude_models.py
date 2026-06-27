"""Claude LLM data models.

Dataclasses and type aliases for Claude tool calls and streaming events.
Extracted from src/gobby/llm/claude.py as part of the Strangler Fig
decomposition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class TextChunk:
    """A chunk of text from the streaming response."""

    content: str
    """The text content."""


@dataclass
class ToolCallEvent:
    """Event when a tool is being called."""

    tool_call_id: str
    """Unique ID for this tool call."""

    tool_name: str
    """Full tool name (e.g., mcp__gobby-tasks__create_task)."""

    server_name: str
    """Extracted server name (e.g., gobby-tasks)."""

    arguments: dict[str, Any]
    """Arguments passed to the tool."""


@dataclass
class ToolResultEvent:
    """Event when a tool call completes."""

    tool_call_id: str
    """ID matching the original ToolCallEvent."""

    success: bool
    """Whether the tool call succeeded."""

    result: Any = None
    """Result data if successful."""

    error: str | None = None
    """Error message if failed."""


@dataclass
class SessionConfigOptionsEvent:
    """Event when ACP session config options change."""

    config_options: list[dict[str, Any]]
    """Complete current ACP config option state."""


@dataclass
class SessionInfoUpdateEvent:
    """Event when ACP session metadata changes."""

    session_info: dict[str, Any]
    """Complete ACP sessionInfo update payload."""


@dataclass
class SessionModeUpdateEvent:
    """Event when ACP reports a current session mode change."""

    current_mode_id: str
    """Provider-reported ACP currentModeId."""

    chat_mode: str | None = None
    """Mapped Gobby chat mode when the ACP mode is a known Gobby posture."""


@dataclass
class SessionUsageUpdateEvent:
    """Event when ACP reports usage/context-window data."""

    usage: dict[str, Any]
    """Normalized usage payload for websocket consumers."""


@dataclass
class DoneEvent:
    """Event when streaming is complete."""

    tool_calls_count: int
    """Total number of tool calls made."""

    duration_ms: float | None = None
    """Duration in milliseconds if available."""

    input_tokens: int | None = None
    """Non-cached input tokens (often very small with prompt caching)."""

    output_tokens: int | None = None
    """Output tokens generated in this turn."""

    cache_read_input_tokens: int | None = None
    """Tokens read from cache."""

    cache_creation_input_tokens: int | None = None
    """Tokens written to cache."""

    total_input_tokens: int | None = None
    """Sum of input_tokens + cache_read + cache_creation.

    This is the real context size consumed this turn. With Claude Code's
    aggressive prompt caching, ``input_tokens`` alone is often only 3-23
    tokens — the bulk lives in cache_read/cache_creation.
    """

    context_window: int | None = None
    """Max context window size for the model."""

    sdk_session_id: str | None = None
    """SDK session_id from ResultMessage (used to re-key web chat sessions)."""


@dataclass
class ThinkingEvent:
    """Event when the model is using extended thinking."""

    content: str = ""


# Union type for all streaming events
ChatEvent = (
    TextChunk
    | ToolCallEvent
    | ToolResultEvent
    | DoneEvent
    | ThinkingEvent
    | SessionConfigOptionsEvent
    | SessionInfoUpdateEvent
    | SessionModeUpdateEvent
    | SessionUsageUpdateEvent
)

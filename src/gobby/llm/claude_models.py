"""Claude LLM data models.

Dataclasses and type aliases for Claude tool calls and streaming events.
Extracted from src/gobby/llm/claude.py as part of the Strangler Fig
decomposition.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Default context window for all Claude models (200K tokens).
# litellm incorrectly returns 1M for some Claude models (the beta
# context-1m-2025-08-07 window), so we never trust litellm for Claude.
CLAUDE_DEFAULT_CONTEXT_WINDOW = 200_000

# Substrings that identify a model as Claude.
# This list is used to identify Claude model variants and should be extended
# when new naming conventions appear (e.g., "claude" as a fallback).
# A more robust detection strategy (prefix/suffix check for "claude") could be implemented later.
_CLAUDE_IDENTIFIERS = ("opus", "sonnet", "haiku", "claude")


def resolve_context_window(
    model: str | None,
    model_usage: dict[str, Any] | None,
) -> int | None:
    """Resolve the context window size for a model.

    Priority order:
    1. SDK-reported ``contextWindow`` from ``model_usage`` (authoritative from CLI)
    2. Static 200K for Claude models — never trust litellm for these
    3. litellm lookup for non-Claude models only

    Args:
        model: Model name (e.g. "claude-opus-4-6", "gemini-2.0-flash").
        model_usage: The ``_model_usage`` dict stashed by sdk_compat, or None.

    Returns:
        Context window size in tokens, or None if unknown.
    """
    # 1. SDK-reported contextWindow (most authoritative)
    if isinstance(model_usage, dict):
        ctx = model_usage.get("contextWindow")
        if ctx is not None:
            return int(ctx)

    if not model:
        return None

    model_lower = model.lower()

    # 2. Static 200K for Claude models
    if any(k in model_lower for k in _CLAUDE_IDENTIFIERS):
        return CLAUDE_DEFAULT_CONTEXT_WINDOW

    # 3. litellm for non-Claude models only
    try:
        import litellm

        model_info = litellm.get_model_info(model=model)
        val = model_info.get("max_input_tokens")
        if val is not None:
            return int(val)
    except Exception as e:
        logger.debug("Could not derive context window for %s: %s", model, e)

    return None


@dataclass
class ToolCall:
    """Represents a tool call made during generation."""

    tool_name: str
    """Full tool name (e.g., mcp__gobby-tasks__create_task)."""

    server_name: str
    """Extracted server name from the tool (e.g., gobby-tasks)."""

    arguments: dict[str, Any]
    """Arguments passed to the tool."""

    result: str | None = None
    """Result returned by the tool, if available."""


@dataclass
class MCPToolResult:
    """Result of generate_with_mcp_tools."""

    text: str
    """Final text output from the generation."""

    tool_calls: list[ToolCall] = field(default_factory=list)
    """List of tool calls made during generation."""


# Streaming event types for stream_with_mcp_tools
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
class DoneEvent:
    """Event when streaming is complete."""

    tool_calls_count: int
    """Total number of tool calls made."""

    cost_usd: float | None = None
    """Cost in USD if available."""

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
ChatEvent = TextChunk | ToolCallEvent | ToolResultEvent | DoneEvent | ThinkingEvent

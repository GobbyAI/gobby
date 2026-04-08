"""Claude LLM data models.

Dataclasses and type aliases for Claude tool calls and streaming events.
Extracted from src/gobby/llm/claude.py as part of the Strangler Fig
decomposition.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

CLAUDE_DEFAULT_CONTEXT_WINDOW = 200_000  # fallback for unknown Claude models


def resolve_context_window(
    model: str | None,
    _unused: Any = None,
    overrides: dict[str, int] | None = None,
) -> int | None:
    """Resolve the context window size for a model.

    Priority order:
    1. Config overrides (model substring match)
    2. OpenRouter registry data (via cost_table cache)
    3. CLAUDE_DEFAULT_CONTEXT_WINDOW for Claude models

    Args:
        model: Model name (e.g. "claude-opus-4-6", "gpt-4o").
        _unused: Deprecated, kept for call-site compat. Ignored.
        overrides: Optional config-driven overrides mapping model substring to
            context window size (e.g. ``{"opus": 1_000_000}``).

    Returns:
        Context window size in tokens, or None if unknown.
    """
    if not model:
        return None

    model_lower = model.lower()

    # 1. Config overrides
    for substr, window in (overrides or {}).items():
        if substr in model_lower:
            return window

    # 2. Registry lookup (OpenRouter data cached in model_costs table)
    from gobby.llm.model_registry import lookup_context_window

    registry_val = lookup_context_window(model)
    if registry_val is not None:
        return registry_val

    # 3. Fallback for Claude models when registry has no data
    if any(k in model_lower for k in ("opus", "sonnet", "haiku", "claude")):
        return CLAUDE_DEFAULT_CONTEXT_WINDOW

    return None


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

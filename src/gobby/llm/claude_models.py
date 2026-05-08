"""Claude LLM data models.

Dataclasses and type aliases for Claude tool calls and streaming events.
Extracted from src/gobby/llm/claude.py as part of the Strangler Fig
decomposition.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.servers.provider_models import ProviderModelCatalog

logger = logging.getLogger(__name__)


_QWEN_AUTH_SUFFIXES = frozenset({"qwen-oauth", "openai", "anthropic", "gemini", "vertex-ai"})


def _strip_qwen_auth_suffix(model: str) -> str:
    trimmed = model.strip()
    close_idx = trimmed.rfind(")")
    open_idx = trimmed.rfind("(")
    if open_idx >= 0 and close_idx == len(trimmed) - 1 and open_idx < close_idx:
        model_id = trimmed[:open_idx].strip()
        auth_type = trimmed[open_idx + 1 : close_idx].strip()
        if model_id and auth_type in _QWEN_AUTH_SUFFIXES:
            return model_id
    return trimmed


def _registry_lookup_candidates(provider: str | None, model: str) -> list[str]:
    candidates = [model]
    if provider == "qwen":
        candidates.append(_strip_qwen_auth_suffix(model))
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def _get_provider_model_catalog() -> ProviderModelCatalog | None:
    try:
        from gobby.app_context import get_app_context

        ctx = get_app_context()
    except (ImportError, AttributeError):
        return None
    return getattr(ctx, "provider_model_catalog", None) if ctx else None


def resolve_context_window(
    model: str | None,
    _unused: Any = None,
    overrides: dict[str, int] | None = None,
    *,
    provider: str | None = None,
    catalog: Any | None = None,
) -> int | None:
    """Resolve the context window size for a model.

    Priority order:
    1. Config overrides (model substring match)
    2. Provider model catalog metadata
    3. OpenRouter registry data (via model_costs cache)

    Args:
        model: Model name (e.g. "claude-opus-4-6", "gpt-4o").
        _unused: Deprecated, kept for call-site compat. Ignored.
        overrides: Optional config-driven overrides mapping model substring to
            context window size (e.g. ``{"opus": 1_000_000}``).
        provider: Optional Gobby provider name for catalog lookups.
        catalog: Optional ProviderModelCatalog-like object.

    Returns:
        Context window size in tokens, or None if unknown.
    """
    if not model:
        return None

    model_lower = model.lower()

    # 1. Config overrides
    for substr, window in (overrides or {}).items():
        if substr.lower() in model_lower:
            return window

    # 2. Provider catalog lookup
    provider_name = provider.strip().lower() if isinstance(provider, str) else None
    provider_catalog = catalog or _get_provider_model_catalog()
    if provider_catalog is not None and hasattr(provider_catalog, "get_context_window"):
        catalog_val = provider_catalog.get_context_window(provider_name, model)
        if isinstance(catalog_val, int) and not isinstance(catalog_val, bool):
            return catalog_val

    # 3. Registry lookup (OpenRouter data cached in model_costs table)
    from gobby.llm.model_registry import lookup_context_window

    for candidate in _registry_lookup_candidates(provider_name, model):
        registry_val = lookup_context_window(candidate)
        if registry_val is not None:
            return registry_val

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

"""MCP tool description summarization."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.config.features import ToolSummarizerConfig
    from gobby.llm.service import LLMService
from gobby.prompts import PromptLoader
from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

# Maximum description length for tool summaries
MAX_DESCRIPTION_LENGTH = 200

# Module-level resolvers are bound once; each tool call resolves its captured runtime epoch.
_config_resolver: Callable[[], ToolSummarizerConfig | None] | None = None
_loader: PromptLoader | None = None
_llm_service_resolver: Callable[[], LLMService | None] | None = None


def init_summarizer_config(
    config_resolver: Callable[[], ToolSummarizerConfig | None],
    db: HubDatabase,
    project_dir: str | None = None,
    llm_service_resolver: Callable[[], LLMService | None] | None = None,
) -> None:
    """Initialize the summarizer with per-operation dependency resolvers."""
    global _config_resolver, _loader, _llm_service_resolver
    _config_resolver = config_resolver
    _loader = PromptLoader(db=db)
    _llm_service_resolver = llm_service_resolver


def reset_summarizer_config() -> None:
    """Clear daemon-owned summarizer state after failed construction."""
    global _config_resolver, _loader, _llm_service_resolver
    _config_resolver = None
    _loader = None
    _llm_service_resolver = None


def _get_config() -> ToolSummarizerConfig:
    """Get the current config, with fallback to defaults."""
    config = _config_resolver() if _config_resolver is not None else None
    if config is not None:
        return config
    # Import here to avoid circular imports
    from gobby.config.features import ToolSummarizerConfig

    return ToolSummarizerConfig()


def _get_llm_service() -> LLMService | None:
    return _llm_service_resolver() if _llm_service_resolver is not None else None


async def _summarize_description_with_llm(description: str) -> str:
    """
    Summarize a tool description using the configured LLM provider.

    Args:
        description: Long tool description to summarize

    Returns:
        Summarized description (max 180 chars)
    """
    config = _get_config()
    llm_service = _get_llm_service()

    try:
        if not llm_service:
            raise RuntimeError("LLM service not initialized")

        # Get summary prompt
        prompt_path = config.prompt_path or "features/tool_summary"
        if _loader is None:
            raise RuntimeError("Summarizer not initialized")
        prompt = _loader.render(prompt_path, {"description": description})

        # Get system prompt
        sys_prompt_path = config.system_prompt_path or "features/tool_summary_system"
        try:
            system_prompt = _loader.render(sys_prompt_path, {})
        except (OSError, KeyError, ValueError, RuntimeError):
            system_prompt = "You are a technical summarizer."

        return await llm_service.call_feature(
            config,
            prompt,
            system_prompt=system_prompt,
            caller="tools.tool_summary",
        )

    except Exception as e:
        logger.warning("Failed to summarize description with configured LLM: %s", e)
        # Fallback: truncate to 200 chars with ellipsis
        return description[:197] + "..." if len(description) > 200 else description


async def summarize_tools(tools: list[Any]) -> list[dict[str, Any]]:
    """
    Create lightweight tool summaries with intelligent description shortening.

    Args:
        tools: List of MCP Tool objects with name, description, and input_schema

    Returns:
        List of dicts with name, summarized description, and args:
        [{"name": "tool_name", "description": "Short summary...", "args": {...}}]
    """
    summaries = []

    for tool in tools:
        description = tool.description or ""

        # Summarize if needed
        if len(description) > MAX_DESCRIPTION_LENGTH:
            logger.debug(
                "Summarizing description for tool '%s' (%s chars)", tool.name, len(description)
            )
            description = await _summarize_description_with_llm(description)

        summaries.append(
            {
                "name": tool.name,
                "description": description,
                "args": tool.input_schema,
            }
        )

    return summaries


async def generate_server_description(
    server_name: str, tool_summaries: list[dict[str, Any]]
) -> str:
    """
    Generate a concise server description from tool summaries.

    Uses the configured LLM to synthesize a single-sentence description
    of what the MCP server does based on all its available tools.

    Args:
        server_name: Name of the MCP server
        tool_summaries: List of tool summaries from summarize_tools()

    Returns:
        Single-sentence description (aiming for <100 chars)
    """
    config = _get_config()
    llm_service = _get_llm_service()

    try:
        if not llm_service:
            raise RuntimeError("LLM service not initialized")

        # Build tools list for prompt
        tools_list = "\n".join([f"- {t['name']}: {t['description']}" for t in tool_summaries])

        # Build prompt
        prompt_path = config.server_description_prompt_path or "features/server_description"
        context = {
            "server_name": server_name,
            "tools_list": tools_list,
        }
        if _loader is None:
            raise RuntimeError("Summarizer not initialized")
        prompt = _loader.render(prompt_path, context)

        # Get system prompt
        sys_prompt_path = (
            config.server_description_system_prompt_path or "features/server_description_system"
        )
        try:
            system_prompt = _loader.render(sys_prompt_path, {})
        except (OSError, KeyError, ValueError, RuntimeError):
            system_prompt = "You write concise technical descriptions."

        return await llm_service.call_feature(
            config,
            prompt,
            system_prompt=system_prompt,
            caller="tools.server_description",
        )

    except Exception as e:
        logger.warning("Failed to generate server description for '%s': %s", server_name, e)
        # Fallback: Generate simple description from first few tools
        if tool_summaries:
            first_tools = ", ".join([t["name"] for t in tool_summaries[:3]])
            return f"Provides {first_tools} and more"
        return f"MCP server: {server_name}"

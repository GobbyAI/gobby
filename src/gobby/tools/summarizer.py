"""
Tool description summarization using Claude Agent SDK.

Intelligently summarizes long MCP tool descriptions to fit within
the 200-character limit for config file storage.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.config.features import ToolSummarizerConfig
from gobby.prompts import PromptLoader
from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)

# Maximum description length for tool summaries
MAX_DESCRIPTION_LENGTH = 200

# Module-level config reference (set by init_summarizer_config)
_config: ToolSummarizerConfig | None = None
_loader: PromptLoader | None = None


def init_summarizer_config(
    config: ToolSummarizerConfig,
    project_dir: str | None = None,
    db: DatabaseProtocol | None = None,
) -> None:
    """Initialize the summarizer with configuration."""
    global _config, _loader
    _config = config
    _loader = PromptLoader(db=db)


def _get_config() -> ToolSummarizerConfig:
    """Get the current config, with fallback to defaults."""
    if _config is not None:
        return _config
    # Import here to avoid circular imports
    from gobby.config.features import ToolSummarizerConfig

    return ToolSummarizerConfig()


async def _summarize_description_with_claude(description: str) -> str:
    """
    Summarize a tool description using Claude Agent SDK.

    Args:
        description: Long tool description to summarize

    Returns:
        Summarized description (max 180 chars)
    """
    config = _get_config()

    try:
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

        # Get summary prompt
        prompt_path = config.prompt_path or "features/tool_summary"
        try:
            # We assume _loader is initialized by _get_config() logic or init
            if _loader is None:
                raise RuntimeError("Summarizer not initialized")
            prompt = _loader.render(prompt_path, {"description": description})
        except (OSError, KeyError, ValueError, RuntimeError) as e:
            logger.debug(f"Failed to load prompt from {prompt_path}: {e}")
            raise

        # Get system prompt
        sys_prompt_path = config.system_prompt_path or "features/tool_summary_system"
        try:
            if _loader is None:
                raise RuntimeError("Summarizer not initialized")
            system_prompt = _loader.render(sys_prompt_path, {})
        except (OSError, KeyError, ValueError, RuntimeError) as e:
            logger.debug(f"Failed to load system prompt from {sys_prompt_path}: {e}")
            system_prompt = "You are a technical summarizer."

        # Configure for single-turn completion
        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            max_turns=1,
            model=config.model,
            tools=[],  # Passes --tools "" to CLI, disabling all built-in tools
            allowed_tools=[],
            mcp_servers={},
            permission_mode="default",
        )

        # Run async query
        summary_text = ""
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        summary_text = block.text
        return summary_text.strip()

    except Exception as e:
        logger.warning(f"Failed to summarize description with Claude: {e}")
        # Fallback: truncate to 200 chars with ellipsis
        return description[:197] + "..." if len(description) > 200 else description


async def summarize_tools(tools: list[Any]) -> list[dict[str, Any]]:
    """
    Create lightweight tool summaries with intelligent description shortening.

    Args:
        tools: List of MCP Tool objects with name, description, and inputSchema

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
                f"Summarizing description for tool '{tool.name}' ({len(description)} chars)"
            )
            description = await _summarize_description_with_claude(description)

        summaries.append(
            {
                "name": tool.name,
                "description": description,
                "args": tool.inputSchema if hasattr(tool, "inputSchema") else {},
            }
        )

    return summaries


async def generate_server_description(
    server_name: str, tool_summaries: list[dict[str, Any]]
) -> str:
    """
    Generate a concise server description from tool summaries.

    Uses Claude Haiku to synthesize a single-sentence description of what
    the MCP server does based on all its available tools.

    Args:
        server_name: Name of the MCP server
        tool_summaries: List of tool summaries from summarize_tools()

    Returns:
        Single-sentence description (aiming for <100 chars)
    """
    config = _get_config()

    try:
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

        # Build tools list for prompt
        tools_list = "\n".join([f"- {t['name']}: {t['description']}" for t in tool_summaries])

        # Build prompt
        prompt_path = config.server_description_prompt_path or "features/server_description"
        context = {
            "server_name": server_name,
            "tools_list": tools_list,
        }
        if _loader is None:
            _get_config()  # force init
        if _loader is None:
            # Still None after _get_config, use default
            raise RuntimeError("Summarizer not initialized")
        else:
            prompt = _loader.render(prompt_path, context)

        # Get system prompt
        sys_prompt_path = (
            config.server_description_system_prompt_path or "features/server_description_system"
        )

        if _loader is None:
            system_prompt = "You write concise technical descriptions."
        else:
            system_prompt = _loader.render(sys_prompt_path, {})

        # Configure for single-turn completion
        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            max_turns=1,
            model=config.model,
            tools=[],  # Passes --tools "" to CLI, disabling all built-in tools
            allowed_tools=[],
            mcp_servers={},
            permission_mode="default",
        )

        # Run async query
        description = ""
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        description = block.text

        return description.strip()

    except Exception as e:
        logger.warning(f"Failed to generate server description for '{server_name}': {e}")
        # Fallback: Generate simple description from first few tools
        if tool_summaries:
            first_tools = ", ".join([t["name"] for t in tool_summaries[:3]])
            return f"Provides {first_tools} and more"
        return f"MCP server: {server_name}"

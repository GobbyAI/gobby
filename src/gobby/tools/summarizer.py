"""
Tool description summarization using Claude Agent SDK.

Intelligently summarizes long MCP tool descriptions to fit within
the 200-character limit for config file storage.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Maximum description length for tool summaries
MAX_DESCRIPTION_LENGTH = 200


async def _summarize_description_with_claude(description: str) -> str:
    """
    Summarize a tool description using Claude Agent SDK.

    Args:
        description: Long tool description to summarize

    Returns:
        Summarized description (max 180 chars)
    """
    try:
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

        prompt = f"""Summarize this MCP tool description in 180 characters or less.
Keep it to three sentences or less. Be concise and preserve the key functionality.
Do not add quotes, extra formatting, or code examples.

Description: {description}

Summary:"""

        # Configure for single-turn completion with Haiku
        options = ClaudeAgentOptions(
            system_prompt="You are a technical summarizer. Create concise tool descriptions.",
            max_turns=1,
            model="claude-haiku-4-5",  # Fast, cheap model
            allowed_tools=[],
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
    try:
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

        # Build tools list for prompt
        tools_list = "\n".join([f"- {t['name']}: {t['description']}" for t in tool_summaries])

        prompt = f"""Write a single concise sentence describing what the '{server_name}' MCP server does based on its tools.

Tools:
{tools_list}

Description (1 sentence, try to keep under 100 characters):"""

        # Configure for single-turn completion with Haiku
        options = ClaudeAgentOptions(
            system_prompt="You write concise technical descriptions.",
            max_turns=1,
            model="claude-haiku-4-5",
            allowed_tools=[],
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

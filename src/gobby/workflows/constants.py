"""Shared constants used across the workflow system.

This module centralises constant values (keyword sets, tool exemption lists,
configuration defaults, etc.) that are referenced by multiple workflow modules
such as the rule engine, evaluator, pipeline executor, and enforcement layer.
"""

# Read-only MCP discovery tools that are always allowed regardless of workflow step restrictions.
# These "meta" tools enable progressive disclosure and are required for agents to discover
# what tools are available. They don't execute actions, only return information.
# NOTE: call_tool is intentionally NOT exempt - it executes actual tools and should be restricted.
EXEMPT_TOOLS = frozenset(
    {
        # Gobby MCP discovery tools (both prefixed and unprefixed forms)
        "list_mcp_servers",
        "mcp__gobby__list_mcp_servers",
        "list_tools",
        "mcp__gobby__list_tools",
        "get_tool_schema",
        "mcp__gobby__get_tool_schema",
        "recommend_tools",
        "mcp__gobby__recommend_tools",
        "search_tools",
        "mcp__gobby__search_tools",
    }
)

# Pipeline test constants for coordinator pipeline verification
PIPELINE_TEST_1 = "coordinator-run-1"
PIPELINE_TEST_2 = "coordinator-run-2"
PIPELINE_TEST_3 = "coordinator-run-3"
PIPELINE_TEST_4 = "coordinator-run-4"

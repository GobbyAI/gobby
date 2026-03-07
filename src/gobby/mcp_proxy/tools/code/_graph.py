"""Graph query tools for gobby-code."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.code._context import CodeRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry


def create_graph_registry(ctx: CodeRegistryContext) -> InternalToolRegistry:
    """Create graph query sub-registry."""
    registry = InternalToolRegistry(
        name="gobby-code-graph",
        description="Code graph query tools (callers, usages, imports)",
    )

    @registry.tool(description="Find symbols that call a given function/method. Requires Neo4j.")
    async def find_callers(
        symbol_name: str,
        project_id: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Find callers of a symbol."""
        if ctx.graph is None or not ctx.graph.available:
            return [{"error": "Graph not available (Neo4j not configured)"}]

        pid = project_id or ctx.project_id or "default"
        return await ctx.graph.find_callers(symbol_name, pid, limit)

    @registry.tool(description="Find all usages of a symbol (calls + imports). Requires Neo4j.")
    async def find_usages(
        symbol_name: str,
        project_id: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Find usages of a symbol."""
        if ctx.graph is None or not ctx.graph.available:
            return [{"error": "Graph not available (Neo4j not configured)"}]

        pid = project_id or ctx.project_id or "default"
        return await ctx.graph.find_usages(symbol_name, pid, limit)

    @registry.tool(description="Get import graph for a file showing what it imports.")
    async def get_imports(
        file_path: str,
        project_id: str = "",
    ) -> list[dict[str, Any]]:
        """Get imports for a file."""
        if ctx.graph is None or not ctx.graph.available:
            return [{"error": "Graph not available (Neo4j not configured)"}]

        pid = project_id or ctx.project_id or "default"
        return await ctx.graph.get_imports(file_path, pid)

    return registry

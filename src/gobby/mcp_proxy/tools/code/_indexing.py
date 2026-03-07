"""Indexing tools for gobby-code."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.code._context import CodeRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry


def create_indexing_registry(ctx: CodeRegistryContext) -> InternalToolRegistry:
    """Create indexing sub-registry."""
    registry = InternalToolRegistry(
        name="gobby-code-indexing",
        description="Code indexing operations",
    )

    @registry.tool(description="List all indexed projects with stats.")
    def list_indexed() -> list[dict[str, Any]]:
        """List indexed projects."""
        projects = ctx.storage.list_indexed_projects()
        return [p.to_dict() for p in projects]

    @registry.tool(description="Clear index for a project. Forces full re-index next time.")
    async def invalidate_index(project_id: str = "") -> dict[str, str]:
        """Invalidate (clear) the code index for a project."""
        pid = project_id or ctx.project_id or "default"
        await ctx.indexer.invalidate(pid)
        return {"status": "ok", "project_id": pid}

    return registry

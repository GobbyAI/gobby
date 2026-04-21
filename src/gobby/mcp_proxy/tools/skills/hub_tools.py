"""Handlers for list_hubs and search_hub tools."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.skills._context import SkillsContext


def register(ctx: SkillsContext, registry: InternalToolRegistry) -> None:
    """Register the list_hubs and search_hub tools on the registry."""

    @registry.tool(
        name="list_hubs",
        description="List all configured skill hubs. Returns hub names and types.",
    )
    def list_hubs() -> dict[str, Any]:
        """
        List all configured skill hubs.

        Returns hub name, type, and base_url for each configured hub.

        Returns:
            Dict with success status and list of hub info
        """
        try:
            if ctx.hub_manager is None:
                return {
                    "success": True,
                    "count": 0,
                    "hubs": [],
                }

            hub_names = ctx.hub_manager.list_hubs()
            hubs_list = []

            for name in hub_names:
                config = ctx.hub_manager.get_config(name)
                entry = {
                    "name": name,
                    "type": config.type,
                    "base_url": config.base_url,
                }
                entry.update(ctx.hub_manager.auth_status(name))
                hubs_list.append(entry)

            return {
                "success": True,
                "count": len(hubs_list),
                "hubs": hubs_list,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="search_hub",
        description="Search for skills across configured hubs. Returns ranked results from all or specific hubs.",
    )
    async def search_hub(
        query: str,
        hub_name: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """
        Search for skills across configured hubs.

        Args:
            query: Search query (required, non-empty)
            hub_name: Optional specific hub to search (None for all hubs)
            limit: Maximum results per hub (default 20)

        Returns:
            Dict with success status and search results
        """
        try:
            # Validate query
            if not query or not query.strip():
                return {"success": False, "error": "Query is required and cannot be empty"}

            if ctx.hub_manager is None:
                return {
                    "success": False,
                    "error": "No hub manager configured. Add hubs to config to enable hub search.",
                }

            # Build hub filter
            hub_names_filter = [hub_name] if hub_name else None

            # Perform search
            results, errors = await ctx.hub_manager.search_all(
                query=query.strip(),
                limit=limit,
                hub_names=hub_names_filter,
            )

            response: dict[str, Any] = {
                "success": True,
                "count": len(results),
                "results": results,
                "hub_errors": errors,
            }
            return response
        except Exception as e:
            return {"success": False, "error": str(e)}

"""Summary tools for gobby-code."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.code._context import CodeRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry


def create_summary_registry(ctx: CodeRegistryContext) -> InternalToolRegistry:
    """Create summary sub-registry."""
    registry = InternalToolRegistry(
        name="gobby-code-summary",
        description="Code summary tools",
    )

    @registry.tool(
        description="Get AI-generated summary for a symbol. Cached after first generation."
    )
    def get_summary(symbol_id: str) -> dict[str, Any]:
        """Get or generate summary for a symbol."""
        sym = ctx.storage.get_symbol(symbol_id)
        if sym is None:
            return {"error": f"Symbol not found: {symbol_id}"}

        if sym.summary:
            return {
                "symbol_id": sym.id,
                "name": sym.qualified_name,
                "summary": sym.summary,
                "cached": True,
            }

        return {
            "symbol_id": sym.id,
            "name": sym.qualified_name,
            "summary": None,
            "note": "Summary not yet generated. Background generation will create it.",
        }

    @registry.tool(
        description="High-level project summary showing top-level modules and their symbol counts."
    )
    def get_repo_outline(project_id: str = "") -> dict[str, Any]:
        """Get high-level project outline."""
        pid = project_id or ctx.project_id or "default"
        project = ctx.storage.get_project_stats(pid)
        if project is None:
            return {"error": f"Project not indexed: {pid}"}

        files = ctx.storage.list_files(pid)

        # Group by top-level directory
        dir_stats: dict[str, dict[str, int]] = {}
        for f in files:
            parts = f.file_path.split("/")
            top_dir = parts[0] if len(parts) > 1 else "."
            if top_dir not in dir_stats:
                dir_stats[top_dir] = {"files": 0, "symbols": 0}
            dir_stats[top_dir]["files"] += 1
            dir_stats[top_dir]["symbols"] += f.symbol_count

        # Sort by symbol count descending
        sorted_dirs = sorted(dir_stats.items(), key=lambda x: x[1]["symbols"], reverse=True)

        return {
            "project_id": pid,
            "root_path": project.root_path,
            "total_files": project.total_files,
            "total_symbols": project.total_symbols,
            "last_indexed_at": project.last_indexed_at,
            "directories": [{"path": d, **stats} for d, stats in sorted_dirs],
        }

    return registry

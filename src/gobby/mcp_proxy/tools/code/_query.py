"""Query tools for gobby-code."""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path
from typing import Any

from gobby.mcp_proxy.tools.code._context import CodeRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry

_logger = logging.getLogger(__name__)


def create_query_registry(ctx: CodeRegistryContext) -> InternalToolRegistry:
    """Create query sub-registry."""
    registry = InternalToolRegistry(
        name="gobby-code-query",
        description="Code symbol query tools",
    )

    @registry.tool(description="File tree with symbol counts per file.")
    def get_file_tree(project_id: str = "") -> list[dict[str, Any]]:
        """Get file structure with symbol counts for an indexed project."""
        pid = project_id or ctx.project_id or "default"
        files = ctx.storage.list_files(pid)
        return [
            {
                "file_path": f.file_path,
                "language": f.language,
                "symbol_count": f.symbol_count,
                "byte_size": f.byte_size,
            }
            for f in files
        ]

    @registry.tool(
        description="Hierarchical symbol outline for a single file. Much cheaper than reading the file."
    )
    def get_file_outline(
        file_path: str,
        project_id: str = "",
    ) -> dict[str, Any]:
        """Get symbol tree for a file without reading its contents."""
        pid = project_id or ctx.project_id or "default"
        symbols = ctx.storage.get_symbols_for_file(pid, file_path)

        # If not found, try path variants
        if not symbols:
            for i in range(len(Path(file_path).parts)):
                candidate = str(Path(*Path(file_path).parts[i:]))
                symbols = ctx.storage.get_symbols_for_file(pid, candidate)
                if symbols:
                    break

        outline = []
        for sym in symbols:
            entry: dict[str, Any] = {
                "id": sym.id,
                "name": sym.name,
                "qualified_name": sym.qualified_name,
                "kind": sym.kind,
                "line_start": sym.line_start,
                "line_end": sym.line_end,
                "signature": sym.signature,
            }
            if sym.docstring:
                entry["docstring"] = sym.docstring[:200]
            if sym.summary:
                entry["summary"] = sym.summary
            if sym.parent_symbol_id:
                entry["parent_id"] = sym.parent_symbol_id
            outline.append(entry)

        return {
            "file_path": file_path,
            "symbol_count": len(outline),
            "symbols": outline,
        }

    @registry.tool(
        description="Get full source for a symbol by ID. O(1) retrieval via byte offsets."
    )
    def get_symbol(
        symbol_id: str,
        project_id: str = "",
    ) -> dict[str, Any]:
        """Retrieve a single symbol with its source code."""
        sym = ctx.storage.get_symbol(symbol_id)
        if sym is None:
            return {"error": f"Symbol not found: {symbol_id}"}

        # Read source from file
        source = _read_symbol_source(sym, project_id or ctx.project_id)
        result = sym.to_dict()
        if source:
            result["source"] = source
        return result

    @registry.tool(description="Batch-retrieve multiple symbols by ID.")
    def get_symbols(
        symbol_ids: list[str],
        project_id: str = "",
    ) -> list[dict[str, Any]]:
        """Retrieve multiple symbols."""
        symbols = ctx.storage.get_symbols(symbol_ids)
        results = []
        for sym in symbols:
            d = sym.to_dict()
            source = _read_symbol_source(sym, project_id or ctx.project_id)
            if source:
                d["source"] = source
            results.append(d)
        return results

    @registry.tool(
        description="Hybrid search: name + semantic + graph. Finds symbols by description or name."
    )
    async def search_symbols(
        query: str,
        project_id: str = "",
        kind: str = "",
        file_path: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search for symbols using hybrid search."""
        pid = project_id or ctx.project_id or "default"
        results = await ctx.searcher.search(
            query=query,
            project_id=pid,
            kind=kind or None,
            file_path=file_path or None,
            limit=limit,
        )

        # Check staleness of result files
        file_paths = {r.get("file_path", "") for r in results if r.get("file_path")}
        stale = _check_staleness(pid, file_paths)

        response: dict[str, Any] = {"results": results}
        if stale:
            response["status"] = "stale"
            response["stale_files"] = stale
            response["note"] = (
                f"{len(stale)} file(s) changed since last index. "
                "Results may be outdated. Re-indexing in background."
            )
            _trigger_async_reindex(pid, stale)
        else:
            response["status"] = "current"
        return response

    @registry.tool(description="Full-text search across symbol names and signatures.")
    def search_text(
        query: str,
        project_id: str = "",
        file_path: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search symbols by text matching."""
        pid = project_id or ctx.project_id or "default"
        return ctx.searcher.search_text(
            query=query,
            project_id=pid,
            file_path=file_path or None,
            limit=limit,
        )

    def _trigger_async_reindex(project_id: str, stale_files: list[str]) -> None:
        """Fire-and-forget incremental re-index of stale files."""

        def _do_reindex() -> None:
            try:
                import httpx

                from gobby.config.bootstrap import load_bootstrap

                port = load_bootstrap().daemon_port
                httpx.post(
                    f"http://localhost:{port}/api/code-index/incremental",
                    json={"project_id": project_id, "files": stale_files},
                    timeout=120,
                )
            except Exception as e:
                _logger.debug(f"Async re-index failed: {e}")

        threading.Thread(target=_do_reindex, daemon=True).start()

    def _check_staleness(project_id: str, file_paths: set[str]) -> list[str]:
        """Check which result files have changed since indexing."""
        if not file_paths:
            return []
        project = ctx.storage.get_project_stats(project_id)
        if not project:
            return []
        root = Path(project.root_path)
        current_hashes: dict[str, str] = {}
        for fp in file_paths:
            full = root / fp
            if full.exists():
                try:
                    current_hashes[fp] = hashlib.sha256(full.read_bytes()).hexdigest()
                except OSError:
                    pass
        if not current_hashes:
            return []
        return ctx.storage.get_stale_files(project_id, current_hashes)

    def _read_symbol_source(sym: Any, project_id: str | None) -> str | None:
        """Read symbol source from file using byte offsets."""
        # Try to find the file on disk
        # Look up project stats for root_path
        pid = project_id or sym.project_id
        project = ctx.storage.get_project_stats(pid)
        if project is None:
            return None

        file_path = Path(project.root_path) / sym.file_path
        if not file_path.exists():
            return None

        try:
            data = file_path.read_bytes()
            return str(data[sym.byte_start : sym.byte_end].decode("utf-8", errors="replace"))
        except (OSError, IndexError):
            return None

    return registry

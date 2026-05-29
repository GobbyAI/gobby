"""Thin context object for code index daemon integration.

Holds storage, gcode graph gateway, and vector_store references for the
daemon's background tasks (maintenance, sync worker, HTTP routes). All actual
indexing and graph projection work is handled by gcode (Rust CLI).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from gobby.code_index.gcode_gateway import GcodeGateway, GcodeGatewayError
from gobby.code_index.storage import CodeIndexStorage
from gobby.config.code_index import CodeIndexConfig

logger = logging.getLogger(__name__)


class CodeIndexGraphUnavailable(RuntimeError):
    """Raised when code graph operations are disabled for this context."""


class CodeIndexProjectNotFound(RuntimeError):
    """Raised when a project id cannot be resolved to an indexed project root."""


class CodeIndexContext:
    """Daemon-side context for code index operations.

    Replaces the old CodeIndexer orchestrator — gcode now handles
    parsing, hashing, chunking, and hub writes. This object
    provides access to storage/graph/vectors for the sync worker,
    maintenance loop, and HTTP invalidate endpoint.
    """

    def __init__(
        self,
        storage: CodeIndexStorage,
        vector_store: Any | None = None,
        gcode_gateway: GcodeGateway | None = None,
        config: CodeIndexConfig | None = None,
        run_db: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self._storage = storage
        self._vector_store = vector_store
        self._config = config or CodeIndexConfig()
        self._gcode_gateway: GcodeGateway | None = gcode_gateway
        if self._gcode_gateway is None and self._config.graph_enabled:
            try:
                self._gcode_gateway = GcodeGateway()
            except Exception as e:
                logger.warning("Code graph gateway unavailable during context init: %s", e)
        self._run_db = run_db

    @property
    def storage(self) -> CodeIndexStorage:
        return self._storage

    @property
    def vector_store(self) -> Any | None:
        return self._vector_store

    @property
    def gcode_gateway(self) -> GcodeGateway | None:
        return self._gcode_gateway

    @property
    def config(self) -> CodeIndexConfig:
        return self._config

    async def run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run code-index hub work on the daemon DB executor when available."""
        if self._run_db is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db(func, *args, **kwargs)

    async def invalidate(self, project_id: str) -> None:
        """Clear all index data for a project."""
        await self.run_db(self._storage.delete_project_index, project_id)

        if self._config.graph_enabled and self._gcode_gateway is not None:
            try:
                result = await self._gcode_gateway.graph_clear(project_id)
                if not result.get("success", False):
                    logger.warning(
                        "Code graph clear during invalidate reported failure for %s: %s",
                        project_id,
                        result.get("error", "unknown error"),
                    )
            except GcodeGatewayError as e:
                logger.warning(
                    "Code graph clear during invalidate failed for %s: %s",
                    project_id,
                    e,
                )

        if self._vector_store is not None:
            collection = f"{self._config.qdrant_collection_prefix}{project_id}"
            try:
                await self._vector_store.delete_collection(collection)
            except Exception as e:
                logger.warning(f"Vector collection delete failed for {collection}: {e}")

        logger.info(f"Invalidated code index for project {project_id}")

    async def graph_overview(self, project_id: str, *, limit: int = 200) -> dict[str, Any]:
        """Return a gcode-owned overview graph for an indexed project."""
        gateway = self._require_graph_enabled()
        root = await self._graph_project_root(project_id)
        return await gateway.graph_overview(root, limit=limit)

    async def graph_file(self, project_id: str, file_path: str) -> dict[str, Any]:
        """Return gcode-owned graph context for one indexed file."""
        gateway = self._require_graph_enabled()
        root = await self._graph_project_root(project_id)
        return await gateway.graph_file(root, file_path)

    async def graph_symbol_neighbors(
        self,
        project_id: str,
        symbol_id: str,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return gcode-owned neighbors for one symbol."""
        gateway = self._require_graph_enabled()
        root = await self._graph_project_root(project_id)
        return await gateway.graph_neighbors(root, symbol_id, limit=limit)

    async def graph_blast_radius(
        self,
        project_id: str,
        *,
        symbol_id: str | None = None,
        file_path: str | None = None,
        depth: int = 3,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return gcode-owned transitive impact graph."""
        gateway = self._require_graph_enabled()
        root = await self._graph_project_root(project_id)
        return await gateway.graph_blast_radius(
            root,
            symbol_id=symbol_id,
            file_path=file_path,
            depth=depth,
            limit=limit,
        )

    async def clear_graph(self, project_id: str) -> dict[str, Any]:
        """Clear only the gcode-owned code graph projection for one project id."""
        gateway = self._require_graph_enabled()
        return await gateway.graph_clear(project_id)

    async def rebuild_graph(self, project_id: str, limit: int = 10_000) -> dict[str, Any]:
        """Rebuild the gcode-owned code graph projection for one indexed project.

        ``limit`` is a deprecated compatibility parameter. Rebuild now delegates to
        gcode, which replays the full indexed project.
        """
        if limit != 10_000:
            logger.warning(
                "CodeIndexContext.rebuild_graph(limit=%s) is deprecated and ignored",
                limit,
            )
        gateway = self._require_graph_enabled()
        root = await self._graph_project_root(project_id)
        return await gateway.graph_rebuild(root)

    async def _graph_project_root(self, project_id: str) -> Path:
        self._require_graph_enabled()
        project = await self.run_db(self._storage.get_project_stats, project_id)
        if project is None or not project.root_path:
            raise CodeIndexProjectNotFound(f"Code index project not found: {project_id}")
        return Path(project.root_path).expanduser()

    def _require_graph_enabled(self) -> GcodeGateway:
        if not self._config.graph_enabled or self._gcode_gateway is None:
            raise CodeIndexGraphUnavailable("Code graph not available")
        return self._gcode_gateway

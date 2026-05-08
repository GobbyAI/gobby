"""Thin context object for code index daemon integration.

Holds storage, graph, and vector_store references for the daemon's
background tasks (maintenance, sync worker, HTTP routes). All actual
indexing is handled by gcode (Rust CLI).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gobby.code_index.graph import CodeGraph
from gobby.code_index.storage import CodeIndexStorage
from gobby.config.code_index import CodeIndexConfig

logger = logging.getLogger(__name__)


class CodeIndexContext:
    """Daemon-side context for code index operations.

    Replaces the old CodeIndexer orchestrator — gcode now handles
    parsing, hashing, chunking, and writing to SQLite. This object
    provides access to storage/graph/vectors for the sync worker,
    maintenance loop, and HTTP invalidate endpoint.
    """

    def __init__(
        self,
        storage: CodeIndexStorage,
        vector_store: Any | None = None,
        graph: CodeGraph | None = None,
        config: CodeIndexConfig | None = None,
    ) -> None:
        self._storage = storage
        self._vector_store = vector_store
        self._graph = graph
        self._config = config or CodeIndexConfig()

    @property
    def storage(self) -> CodeIndexStorage:
        return self._storage

    @property
    def vector_store(self) -> Any | None:
        return self._vector_store

    @property
    def graph(self) -> CodeGraph | None:
        return self._graph

    @property
    def config(self) -> CodeIndexConfig:
        return self._config

    async def invalidate(self, project_id: str) -> None:
        """Clear all index data for a project."""
        await asyncio.to_thread(self._storage.delete_project_index, project_id)

        if self._graph is not None:
            await self._graph.clear_project(project_id)

        if self._vector_store is not None:
            collection = f"{self._config.qdrant_collection_prefix}{project_id}"
            try:
                await self._vector_store.delete_collection(collection)
            except Exception as e:
                logger.warning(f"Vector collection delete failed for {collection}: {e}")

        logger.info(f"Invalidated code index for project {project_id}")

    async def clear_graph(self, project_id: str) -> dict[str, Any]:
        """Clear only the Neo4j code-graph projection for one project."""
        if self._graph is None or not self._graph.available:
            return {"success": False, "error": "Code graph not available", "project_id": project_id}

        try:
            files_marked = await asyncio.to_thread(
                self._storage.reset_graph_sync_for_project,
                project_id,
            )
            await self._graph.clear_project(project_id)
            return {
                "success": True,
                "project_id": project_id,
                "files_marked_pending": files_marked,
            }
        except Exception as e:
            logger.warning(f"Failed to clear code graph for {project_id}: {e}")
            return {"success": False, "error": str(e), "project_id": project_id}

    async def rebuild_graph(self, project_id: str, limit: int = 10_000) -> dict[str, Any]:
        """Rebuild the Neo4j code graph for a project from SQLite source data."""
        if self._graph is None or not self._graph.available:
            return {"success": False, "error": "Code graph not available", "project_id": project_id}

        from gobby.code_index.sync_worker import _sync_graph

        files = await asyncio.to_thread(self._storage.list_files, project_id)
        if limit > 0:
            files = files[:limit]

        try:
            await self._graph.clear_project(project_id)
            await asyncio.to_thread(self._storage.reset_graph_sync_for_project, project_id)
        except Exception as e:
            logger.warning(f"Failed to prepare code graph rebuild for {project_id}: {e}")
            return {"success": False, "error": str(e), "project_id": project_id}

        synced = 0
        errors: list[str] = []

        for file in files:
            await asyncio.to_thread(self._storage.mark_graph_sync_attempted, file.id)
            try:
                await _sync_graph(self._storage, self._graph, project_id, file)
                await asyncio.to_thread(self._storage.mark_graph_synced, file.id)
                synced += 1
            except Exception as e:
                logger.warning(f"Code graph rebuild failed for {file.file_path}: {e}")
                errors.append(f"{file.file_path}: {e}")

        return {
            "success": True,
            "project_id": project_id,
            "files_processed": len(files),
            "files_synced": synced,
            "files_failed": len(errors),
            "errors": errors,
        }

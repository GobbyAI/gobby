"""Indexing service for embedding/crossref/KG/FTS5 lifecycle ops."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from gobby.memory.services.crossref import CrossrefRebuildError, CrossrefService
from gobby.storage.memories import LocalMemoryManager, Memory

if TYPE_CHECKING:
    from gobby.memory.fts_search import MemoryFTS5Searcher
    from gobby.memory.services.knowledge_graph import KnowledgeGraphService
    from gobby.memory.vectorstore import VectorStore

logger = logging.getLogger(__name__)

MAX_REINDEX_LIMIT = 100_000


class IndexingService:
    """Orchestrates wipe, rebuild, reconcile across vector/graph/crossref/FTS indices."""

    def __init__(
        self,
        *,
        storage: LocalMemoryManager,
        vector_store: VectorStore | None,
        embed_fn: Callable[..., Any] | None,
        kg_service: KnowledgeGraphService | None,
        fts_searcher_factory: Callable[[], MemoryFTS5Searcher],
        crossref_service: CrossrefService,
        kg_rebuilder: Callable[[str | None], Awaitable[dict[str, Any]]],
    ) -> None:
        self._storage = storage
        self._vector_store = vector_store
        self._embed_fn = embed_fn
        self._kg_service = kg_service
        self._fts_searcher_factory = fts_searcher_factory
        self._crossref_service = crossref_service
        self._kg_rebuilder = kg_rebuilder

    @property
    def kg_service(self) -> KnowledgeGraphService | None:
        return self._kg_service

    @kg_service.setter
    def kg_service(self, value: KnowledgeGraphService | None) -> None:
        self._kg_service = value

    async def reconcile_stores(self, dry_run: bool = False) -> dict[str, Any]:
        """Reconcile Qdrant and Neo4j with SQLite source of truth."""
        sqlite_ids = set(self._storage.list_all_ids())
        report: dict[str, Any] = {
            "dry_run": dry_run,
            "sqlite_count": len(sqlite_ids),
            "qdrant": {"orphans_found": 0, "orphans_deleted": 0, "errors": 0},
            "neo4j": {
                "orphan_memories_found": 0,
                "orphan_memories_deleted": 0,
                "orphan_entities_deleted": 0,
                "errors": 0,
            },
        }

        if self._vector_store:
            try:
                qdrant_ids = set(await self._vector_store.scroll_ids())
                orphaned = qdrant_ids - sqlite_ids
                report["qdrant"]["total"] = len(qdrant_ids)
                report["qdrant"]["orphans_found"] = len(orphaned)

                if not dry_run and orphaned:
                    try:
                        await self._vector_store.delete_many(list(orphaned))
                        report["qdrant"]["orphans_deleted"] = len(orphaned)
                    except Exception as e:
                        logger.warning(
                            f"Batch delete of {len(orphaned)} Qdrant orphans failed: {e}"
                        )
                        report["qdrant"]["errors"] += len(orphaned)
            except Exception as e:
                logger.error(f"Qdrant reconciliation failed: {e}")
                report["qdrant"]["error"] = str(e)

        if self._kg_service:
            try:
                neo4j_ids = await self._kg_service.get_all_memory_node_ids()
                orphaned = neo4j_ids - sqlite_ids
                report["neo4j"]["total"] = len(neo4j_ids)
                report["neo4j"]["orphan_memories_found"] = len(orphaned)

                if not dry_run and orphaned:
                    deleted = await self._kg_service.remove_memories_from_graph(orphaned)
                    report["neo4j"]["orphan_memories_deleted"] = deleted
                    if deleted < len(orphaned):
                        report["neo4j"]["errors"] += len(orphaned) - deleted

                    entities_deleted = await self._kg_service.remove_orphaned_entities(scope="all")
                    report["neo4j"]["orphan_entities_deleted"] = entities_deleted
            except Exception as e:
                logger.error(f"Neo4j reconciliation failed: {e}")
                report["neo4j"]["error"] = str(e)

        return report

    async def reindex_embeddings(self, project_id: str | None = None) -> dict[str, Any]:
        """Regenerate embeddings for stored memories."""
        if not self._vector_store or not self._embed_fn:
            return {"success": False, "error": "Vector store or embedding function not configured"}

        memories = self._storage.list_memories(project_id=project_id, limit=MAX_REINDEX_LIMIT)
        total = len(memories)
        memory_dicts: list[dict[str, Any]] = [
            {"id": mem.id, "content": mem.content, "project_id": mem.project_id} for mem in memories
        ]

        try:
            if project_id is None:
                await self._vector_store.rebuild(memory_dicts, self._embed_fn)
            else:
                await self._vector_store.delete(filters={"project_id": project_id})
                batch: list[tuple[str, list[float], dict[str, Any]]] = []
                for mem in memory_dicts:
                    mem_id: str = mem["id"]
                    embedding = await self._embed_fn(mem["content"])
                    payload = {k: v for k, v in mem.items() if k != "id"}
                    batch.append((mem_id, embedding, payload))
                    if len(batch) >= 500:
                        await self._vector_store.batch_upsert(batch)
                        logger.info(f"Reindex progress: {len(batch)}/{total} vectors")
                        batch = []
                if batch:
                    await self._vector_store.batch_upsert(batch)
            generated = len(memory_dicts)
        except Exception as e:
            logger.error(f"Failed to rebuild vector store: {e}")
            return {"success": False, "total_memories": total, "error": str(e)}

        return {"success": True, "total_memories": total, "embeddings_generated": generated}

    async def clear_indices(self, project_id: str | None = None) -> dict[str, Any]:
        """Fast wipe of all secondary indices for a project (or all projects)."""
        report: dict[str, Any] = {}

        if self._kg_service:
            report["graph_cleared"] = await self._kg_service.clear_graph(project_id=project_id)

        if self._vector_store:
            try:
                if project_id:
                    await self._vector_store.delete(filters={"project_id": project_id})
                else:
                    await self._vector_store.delete_collection(self._vector_store._collection_name)
                report["vectors_cleared"] = True
            except Exception as e:
                logger.error(f"Failed to clear vectors: {e}")
                report["vectors_cleared"] = False
                report["vectors_error"] = str(e)

        try:
            if project_id:
                deleted = await asyncio.to_thread(
                    self._storage.delete_project_crossrefs, project_id
                )
            else:
                deleted = await asyncio.to_thread(
                    lambda: self._storage.db.execute("DELETE FROM memory_crossrefs").rowcount
                )
            report["crossrefs_cleared"] = deleted
        except Exception as e:
            logger.error(f"Failed to clear crossrefs: {e}")
            report["crossrefs_cleared"] = 0
            report["crossrefs_error"] = str(e)

        try:
            fts = self._fts_searcher_factory()
            await asyncio.to_thread(lambda: fts._db.execute("DELETE FROM memories_fts"))
            report["fts_cleared"] = True
        except Exception as e:
            logger.error(
                f"Failed to clear memory FTS5 index: {e} "
                "(this is an FTS-local issue, not whole-DB corruption)"
            )
            report["fts_cleared"] = False
            report["fts_error"] = str(e)

        scope = f"project {project_id}" if project_id else "all projects"
        logger.info(f"Indices cleared for {scope}: {report}")
        return report

    async def rebuild_indices(self, project_id: str | None = None) -> dict[str, Any]:
        """Rebuild all secondary indices from the SQLite source of truth."""
        report: dict[str, Any] = {}
        scope = f"project {project_id}" if project_id else "all projects"
        logger.info(f"Starting index rebuild for {scope}")

        report["embeddings"] = await self.reindex_embeddings(project_id=project_id)

        if project_id:
            all_memories = await self.fetch_all_project_memories(project_id)
        else:
            all_memories = await asyncio.to_thread(
                self._storage.list_memories, project_id, None, MAX_REINDEX_LIMIT
            )
        total = len(all_memories)

        crossref_sem = asyncio.Semaphore(10)
        crossref_done = 0
        crossref_done_lock = asyncio.Lock()

        async def _rebuild_crossref(mem: Memory) -> int:
            nonlocal crossref_done
            async with crossref_sem:
                try:
                    result = await self._crossref_service.rebuild_for_memory(mem)
                except (CrossrefRebuildError, ValueError) as e:
                    logger.warning(f"Crossref failed for {mem.id}: {e}")
                    result = 0
                async with crossref_done_lock:
                    crossref_done += 1
                    if crossref_done % 50 == 0 or crossref_done == total:
                        logger.info(f"Crossref progress: {crossref_done}/{total}")
                return result

        crossref_results = await asyncio.gather(*[_rebuild_crossref(m) for m in all_memories])
        crossrefs_created = sum(crossref_results)
        logger.info(f"Crossref rebuild complete: {crossrefs_created} links from {total} memories")
        report["crossrefs"] = {
            "memories_processed": total,
            "crossrefs_created": crossrefs_created,
        }

        if self._kg_service:
            report["graph_rebuilt"] = await self._kg_rebuilder(project_id)

        try:
            report["fts5"] = await asyncio.to_thread(self._fts_searcher_factory().reindex)
        except Exception as e:
            logger.error(
                f"Failed to rebuild memory FTS5 index: {e} "
                "(FTS-local failure, other indices were rebuilt successfully)"
            )
            report["fts5"] = {"success": False, "error": str(e)}

        logger.info(f"Index rebuild complete for {scope}: {report}")
        return report

    async def invalidate_all(self, project_id: str | None = None) -> dict[str, Any]:
        """Clear all secondary indices for a project (or globally)."""
        return await self.clear_indices(project_id=project_id)

    async def fetch_all_project_memories(self, project_id: str) -> list[Memory]:
        """Fetch all memories for a project using pagination."""
        all_memories: list[Memory] = []
        offset = 0
        batch_size = 500
        while True:
            batch = await asyncio.to_thread(
                self._storage.list_memories,
                project_id,
                None,
                batch_size,
                offset,
            )
            if not batch:
                break
            all_memories.extend(batch)
            if len(batch) < batch_size:
                break
            offset += batch_size
        return all_memories

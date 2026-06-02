"""Indexing service for embedding, cross-reference, and graph lifecycle ops."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

from gobby.memory.services.crossref import CrossrefRebuildError, CrossrefService
from gobby.storage.memories import Memory

if TYPE_CHECKING:
    from gobby.memory.services.knowledge_graph import KnowledgeGraphService

logger = logging.getLogger(__name__)

MAX_REINDEX_LIMIT = 100_000
GLOBAL_REINDEX_DEDUPE_WINDOW_SECONDS = 60.0


class MemoryStorageProtocol(Protocol):
    db: Any

    def list_all_ids(self, *, limit: int | None = None, offset: int = 0) -> list[str]: ...

    def list_memories(
        self,
        project_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
    ) -> list[Memory]: ...

    def delete_project_crossrefs(self, project_id: str) -> int: ...


class VectorStoreProtocol(Protocol):
    @property
    def collection_name(self) -> str: ...

    async def scroll_ids(self, batch_size: int = 1000) -> list[str]: ...

    async def delete_many(
        self,
        memory_ids: list[str],
        collection_name: str | None = None,
    ) -> None: ...

    async def delete(
        self,
        memory_id: str | None = None,
        filters: dict[str, str] | None = None,
        collection_name: str | None = None,
    ) -> None: ...

    async def batch_upsert(
        self,
        items: list[tuple[str, list[float], dict[str, Any]]],
        collection_name: str | None = None,
    ) -> None: ...

    async def delete_collection(self, collection_name: str) -> None: ...

    async def rebuild(
        self,
        memories: list[dict[str, Any]],
        embed_fn: Callable[[str], Awaitable[list[float]]],
    ) -> None: ...


class IndexingService:
    """Orchestrates wipe, rebuild, and reconcile across vector/graph/crossref indices."""

    def __init__(
        self,
        *,
        storage: MemoryStorageProtocol,
        vector_store: VectorStoreProtocol | None,
        embed_fn: Callable[..., Any] | None,
        kg_service: KnowledgeGraphService | None,
        crossref_service: CrossrefService,
        kg_rebuilder: Callable[[str | None], Awaitable[dict[str, Any]]],
        run_db: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self._storage = storage
        self._vector_store = vector_store
        self._embed_fn = embed_fn
        self._kg_service = kg_service
        self._crossref_service = crossref_service
        self._kg_rebuilder = kg_rebuilder
        self._run_db = run_db
        self._global_reindex_lock = asyncio.Lock()
        self._global_reindex_task: asyncio.Task[dict[str, Any]] | None = None
        self._last_global_reindex_identity_fingerprint: str | None = None
        self._last_global_reindex_fingerprint: str | None = None
        self._last_global_reindex_completed_at: float | None = None

    async def _run_storage(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._run_db is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db(func, *args, **kwargs)

    @staticmethod
    def _memory_dicts(memories: list[Memory]) -> list[dict[str, Any]]:
        return [
            {"id": mem.id, "content": mem.content, "project_id": mem.project_id} for mem in memories
        ]

    @staticmethod
    def _fingerprint_memory_identity(memory_dicts: list[dict[str, Any]]) -> str:
        digest = hashlib.sha256()
        for mem in sorted(memory_dicts, key=lambda item: str(item["id"])):
            digest.update(
                json.dumps(
                    {
                        "id": mem["id"],
                        "project_id": mem.get("project_id"),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            )
            digest.update(b"\n")
        return digest.hexdigest()

    @staticmethod
    def _fingerprint_memory_dicts(memory_dicts: list[dict[str, Any]]) -> str:
        digest = hashlib.sha256()
        for mem in sorted(memory_dicts, key=lambda item: str(item["id"])):
            digest.update(
                json.dumps(
                    {
                        "id": mem["id"],
                        "content": mem["content"],
                        "project_id": mem.get("project_id"),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            )
            digest.update(b"\n")
        return digest.hexdigest()

    async def _global_reindex_is_current(
        self,
        memory_dicts: list[dict[str, Any]],
        identity_fingerprint: str,
        fingerprint: str,
    ) -> bool:
        if self._last_global_reindex_identity_fingerprint != identity_fingerprint:
            return False
        if self._last_global_reindex_fingerprint != fingerprint:
            return False
        if self._last_global_reindex_completed_at is None:
            return False
        elapsed = asyncio.get_running_loop().time() - self._last_global_reindex_completed_at
        if elapsed > GLOBAL_REINDEX_DEDUPE_WINDOW_SECONDS:
            return False
        if self._vector_store is None:
            return False

        expected_ids = {str(mem["id"]) for mem in memory_dicts}
        try:
            actual_ids = set(await self._vector_store.scroll_ids())
        except Exception:
            self._last_global_reindex_identity_fingerprint = None
            self._last_global_reindex_fingerprint = None
            logger.warning(
                "Could not verify vector store IDs before skipping reindex",
                exc_info=True,
            )
            raise
        if actual_ids != expected_ids:
            self._last_global_reindex_identity_fingerprint = None
            self._last_global_reindex_fingerprint = None
            return False
        return True

    @property
    def kg_service(self) -> KnowledgeGraphService | None:
        return self._kg_service

    @kg_service.setter
    def kg_service(self, value: KnowledgeGraphService | None) -> None:
        self._kg_service = value

    async def reconcile_stores(self, dry_run: bool = False) -> dict[str, Any]:
        """Reconcile Qdrant and FalkorDB with the memory storage source of truth."""
        storage_ids = set(await self._run_storage(self._storage.list_all_ids))
        report: dict[str, Any] = {
            "dry_run": dry_run,
            "storage_count": len(storage_ids),
            "qdrant": {"orphans_found": 0, "orphans_deleted": 0, "errors": 0},
            "falkordb": {
                "orphan_memories_found": 0,
                "orphan_memories_deleted": 0,
                "orphan_entities_deleted": 0,
                "errors": 0,
            },
        }

        if self._vector_store:
            try:
                qdrant_ids = set(await self._vector_store.scroll_ids())
                orphaned = qdrant_ids - storage_ids
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
                falkordb_ids = await self._kg_service.get_all_memory_node_ids()
                orphaned = falkordb_ids - storage_ids
                report["falkordb"]["total"] = len(falkordb_ids)
                report["falkordb"]["orphan_memories_found"] = len(orphaned)

                if not dry_run and orphaned:
                    deleted = await self._kg_service.remove_memories_from_graph(orphaned)
                    report["falkordb"]["orphan_memories_deleted"] = deleted
                    if deleted < len(orphaned):
                        report["falkordb"]["errors"] += len(orphaned) - deleted

                    entities_deleted = await self._kg_service.remove_orphaned_entities(scope="all")
                    report["falkordb"]["orphan_entities_deleted"] = entities_deleted
            except Exception as e:
                logger.error(f"FalkorDB reconciliation failed: {e}")
                report["falkordb"]["error"] = str(e)

        return report

    async def reindex_embeddings(self, project_id: str | None = None) -> dict[str, Any]:
        """Regenerate embeddings for stored memories."""
        if not self._vector_store or not self._embed_fn:
            return {"success": False, "error": "Vector store or embedding function not configured"}

        if project_id is None:
            return await self._reindex_global_embeddings()

        total = 0
        try:
            memories = await self._run_storage(
                self._storage.list_memories,
                project_id=project_id,
                limit=MAX_REINDEX_LIMIT,
            )
            total = len(memories)
            memory_dicts = self._memory_dicts(memories)
            await self._vector_store.delete(filters={"project_id": project_id})
            batch: list[tuple[str, list[float], dict[str, Any]]] = []
            processed = 0
            for mem in memory_dicts:
                mem_id: str = mem["id"]
                embedding = await self._embed_fn(mem["content"])
                payload = {k: v for k, v in mem.items() if k != "id"}
                batch.append((mem_id, embedding, payload))
                if len(batch) >= 500:
                    await self._vector_store.batch_upsert(batch)
                    processed += len(batch)
                    logger.info(f"Reindex progress: {processed}/{total} vectors")
                    batch = []
            if batch:
                await self._vector_store.batch_upsert(batch)
                processed += len(batch)
                logger.info(f"Reindex progress: {processed}/{total} vectors")
            generated = len(memory_dicts)
        except Exception as e:
            logger.error(f"Failed to rebuild vector store: {e}")
            return {"success": False, "total_memories": total, "error": str(e)}

        return {
            "success": True,
            "total_memories": total,
            "embeddings_generated": generated,
            "skipped": False,
        }

    async def _reindex_global_embeddings(self) -> dict[str, Any]:
        async with self._global_reindex_lock:
            if self._global_reindex_task is None or self._global_reindex_task.done():
                self._global_reindex_task = asyncio.create_task(
                    self._run_global_embedding_reindex()
                )
            task = self._global_reindex_task

        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._global_reindex_lock:
                    if self._global_reindex_task is task:
                        self._global_reindex_task = None

    async def _run_global_embedding_reindex(self) -> dict[str, Any]:
        total = 0
        try:
            memories = await self._run_storage(
                self._storage.list_memories,
                limit=MAX_REINDEX_LIMIT,
            )
            total = len(memories)
            memory_dicts = self._memory_dicts(memories)
            identity_fingerprint = self._fingerprint_memory_identity(memory_dicts)
            fingerprint = self._fingerprint_memory_dicts(memory_dicts)
            if await self._global_reindex_is_current(
                memory_dicts,
                identity_fingerprint,
                fingerprint,
            ):
                logger.info(
                    "Skipping global embedding reindex; source snapshot unchanged (%s memories)",
                    total,
                )
                return {
                    "success": True,
                    "total_memories": total,
                    "embeddings_generated": 0,
                    "skipped": True,
                    "skip_reason": "source_snapshot_unchanged",
                }
            if self._vector_store is None or self._embed_fn is None:
                return {
                    "success": False,
                    "error": "Vector store or embedding function not configured",
                }
            await self._vector_store.rebuild(memory_dicts, self._embed_fn)
            self._last_global_reindex_identity_fingerprint = identity_fingerprint
            self._last_global_reindex_fingerprint = fingerprint
            self._last_global_reindex_completed_at = asyncio.get_running_loop().time()
        except Exception as e:
            logger.error(f"Failed to rebuild vector store: {e}")
            return {"success": False, "total_memories": total, "error": str(e)}

        return {
            "success": True,
            "total_memories": total,
            "embeddings_generated": total,
            "skipped": False,
        }

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
                    await self._vector_store.delete_collection(self._vector_store.collection_name)
                self._last_global_reindex_identity_fingerprint = None
                self._last_global_reindex_fingerprint = None
                self._last_global_reindex_completed_at = None
                report["vectors_cleared"] = True
            except Exception as e:
                logger.error(f"Failed to clear vectors: {e}")
                report["vectors_cleared"] = False
                report["vectors_error"] = str(e)

        try:
            if project_id:
                deleted = await self._run_storage(
                    self._storage.delete_project_crossrefs, project_id
                )
            else:
                deleted = await self._run_storage(self._delete_all_memory_crossrefs)
            report["crossrefs_cleared"] = deleted
        except Exception as e:
            logger.error(f"Failed to clear crossrefs: {e}")
            report["crossrefs_cleared"] = 0
            report["crossrefs_error"] = str(e)

        scope = f"project {project_id}" if project_id else "all projects"
        logger.info(f"Indices cleared for {scope}: {report}")
        return report

    async def rebuild_indices(self, project_id: str | None = None) -> dict[str, Any]:
        """Rebuild all secondary indices from memory storage."""
        report: dict[str, Any] = {}
        scope = f"project {project_id}" if project_id else "all projects"
        logger.info(f"Starting index rebuild for {scope}")

        report["embeddings"] = await self.reindex_embeddings(project_id=project_id)

        if project_id:
            all_memories = await self.fetch_all_project_memories(project_id)
        else:
            all_memories = await self._run_storage(
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

        logger.info(f"Index rebuild complete for {scope}: {report}")
        return report

    async def invalidate_all(self, project_id: str | None = None) -> dict[str, Any]:
        """Clear all secondary indices for a project (or globally)."""
        return await self.clear_indices(project_id=project_id)

    def _delete_all_memory_crossrefs(self) -> int:
        """Delete every memory_crossrefs row and return the affected row count."""
        return int(self._storage.db.execute("DELETE FROM memory_crossrefs").rowcount)

    async def fetch_all_project_memories(self, project_id: str) -> list[Memory]:
        """Fetch all memories for a project using pagination."""
        all_memories: list[Memory] = []
        offset = 0
        batch_size = 500
        while True:
            batch = await self._run_storage(
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

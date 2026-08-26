"""Indexing service for embedding, cross-reference, and graph lifecycle ops."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol, cast

from gobby.memory.embedding_text import memory_embedding_text
from gobby.memory.services.crossref import CrossrefRebuildError, CrossrefService
from gobby.projects.fenced_vector_store import global_write_context, project_write_context
from gobby.storage.memories import Memory, Visibility
from gobby.storage.memories_scope import ALL_MEMORIES, MemoryScope

if TYPE_CHECKING:
    from gobby.memory.services.knowledge_graph import KnowledgeGraphService

logger = logging.getLogger(__name__)

REINDEX_PAGE_SIZE = 500
REBUILD_SNAPSHOT_SWEEP_PAGE_SIZE = 200
GLOBAL_REINDEX_DEDUPE_WINDOW_SECONDS = 60.0


class MemoryStorageProtocol(Protocol):
    db: Any

    def list_live_ids(self, *, limit: int | None = None, offset: int = 0) -> list[str]: ...

    def get_memories(
        self,
        memory_ids: list[str],
        scope: MemoryScope = ALL_MEMORIES,
        *,
        visibility: Visibility = "active",
    ) -> list[Memory]: ...

    def list_memories(
        self,
        scope: MemoryScope = ALL_MEMORIES,
        memory_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
        *,
        visibility: Visibility = "active",
    ) -> list[Memory]: ...

    def delete_project_crossrefs(self, project_id: str) -> int: ...

    def list_vector_reindex_ids(self) -> list[str]: ...

    def mark_vectors_reindexed(self, indexed_content: dict[str, str]) -> int: ...

    def mark_vector_reindex_needed(self, memory_id: str) -> None: ...

    def mark_vector_snapshot_reindexed(
        self,
        memory_id: str,
        content: str,
        project_id: str,
        is_global: bool,
    ) -> bool: ...

    def reconcile_vector_snapshot_page(
        self,
        snapshots: list[tuple[str, str, str, bool]],
        reindex_ids: list[str],
    ) -> set[str]: ...


class VectorStoreProtocol(Protocol):
    @property
    def collection_name(self) -> str: ...

    async def scroll_ids(
        self,
        batch_size: int = 1000,
        filters: dict[str, str] | None = None,
    ) -> list[str]: ...

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
        reconcile_memory: Callable[[str], Awaitable[bool]] | None = None,
        rebuild_crossrefs: Callable[..., Awaitable[int]] | None = None,
        cleanup_rowless: Callable[[str], Awaitable[None]] | None = None,
        run_db: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self._storage = storage
        self._vector_store = vector_store
        self._embed_fn = embed_fn
        self._kg_service = kg_service
        self._crossref_service = crossref_service
        self._kg_rebuilder = kg_rebuilder
        self._reconcile_memory = reconcile_memory
        self._rebuild_crossrefs = rebuild_crossrefs
        self._cleanup_rowless = cleanup_rowless
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
        # ``rationale`` rides along for the embedding text only; the vector payload
        # and the identity fingerprint leave it out.
        return [
            {
                "id": mem.id,
                "content": mem.content,
                "rationale": mem.rationale,
                "project_id": mem.project_id,
                "is_global": mem.is_global,
                "memory_type": mem.memory_type.value,
            }
            for mem in memories
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
                        "is_global": mem.get("is_global"),
                        "memory_type": mem.get("memory_type"),
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
                        "rationale": mem.get("rationale"),
                        "project_id": mem.get("project_id"),
                        "is_global": mem.get("is_global"),
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
        if await self._run_storage(self._storage.list_vector_reindex_ids):
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
        if not dry_run:
            async with global_write_context(self._vector_store):
                return await self._reconcile_stores_admitted(dry_run=False)
        return await self._reconcile_stores_admitted(dry_run=True)

    async def _reconcile_stores_admitted(self, dry_run: bool) -> dict[str, Any]:
        storage_ids = set(await self._run_storage(self._storage.list_live_ids))
        intent_ids = set(await self._run_storage(self._storage.list_vector_reindex_ids))
        report: dict[str, Any] = {
            "dry_run": dry_run,
            "storage_count": len(storage_ids),
            "qdrant": {
                "orphans_found": 0,
                "orphans_deleted": 0,
                "missing_found": 0,
                "missing_embedded": 0,
                "stale_found": 0,
                "stale_reindexed": 0,
                "errors": 0,
            },
            "falkordb": {
                "orphan_memories_found": 0,
                "orphan_memories_deleted": 0,
                "orphan_entities_deleted": 0,
                "errors": 0,
            },
            "projection_intents": {
                "found": len(intent_ids),
                "attempted": 0,
                "converged": 0,
                "remaining": len(intent_ids),
                "errors": 0,
            },
        }

        repaired_ids: set[str] = set()
        if not dry_run and self._reconcile_memory is not None:
            for memory_id in sorted(intent_ids):
                report["projection_intents"]["attempted"] += 1
                try:
                    if await self._reconcile_memory(memory_id):
                        repaired_ids.add(memory_id)
                except Exception as exc:
                    logger.warning("Projection-intent repair failed for %s: %s", memory_id, exc)
                    report["projection_intents"]["errors"] += 1
            report["projection_intents"]["converged"] = len(repaired_ids)
            report["projection_intents"]["remaining"] = len(intent_ids - repaired_ids)

        if self._vector_store:
            try:
                qdrant_ids = set(await self._vector_store.scroll_ids())
                orphaned = qdrant_ids - storage_ids
                missing = storage_ids - qdrant_ids
                stale = intent_ids
                reindex_ids = missing if self._reconcile_memory is not None else missing | stale
                report["qdrant"]["total"] = len(qdrant_ids)
                report["qdrant"]["orphans_found"] = len(orphaned)
                report["qdrant"]["missing_found"] = len(missing)
                report["qdrant"]["stale_found"] = len(stale)
                report["qdrant"]["stale_reindexed"] = len(repaired_ids & stale)

                if not dry_run and orphaned:
                    try:
                        await self._vector_store.delete_many(list(orphaned))
                        report["qdrant"]["orphans_deleted"] = len(orphaned)
                    except Exception as e:
                        logger.warning(
                            "Batch delete of %s Qdrant orphans failed: %s", len(orphaned), e
                        )
                        report["qdrant"]["errors"] += len(orphaned)
                if not dry_run and reindex_ids:
                    indexed_content, failures = await self._backfill_embeddings(reindex_ids)
                    embedded_ids = set(indexed_content)
                    report["qdrant"]["missing_embedded"] = len(embedded_ids & missing)
                    reindexed_stale = embedded_ids & stale
                    cleared_stale = 0
                    if reindexed_stale and self._reconcile_memory is None:
                        cleared_stale = await self._run_storage(
                            self._storage.mark_vectors_reindexed,
                            {
                                memory_id: indexed_content[memory_id]
                                for memory_id in reindexed_stale
                            },
                        )
                    report["qdrant"]["stale_reindexed"] = (
                        len(repaired_ids & stale)
                        if self._reconcile_memory is not None
                        else cleared_stale
                    )
                    report["qdrant"]["errors"] += len(failures)
                    if failures:
                        report["qdrant"]["reindex_failures"] = failures
            except Exception as e:
                logger.error("Qdrant reconciliation failed: %s", e)
                report["qdrant"]["error"] = str(e)
                report["qdrant"]["errors"] += 1

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
                logger.error("FalkorDB reconciliation failed: %s", e)
                report["falkordb"]["error"] = str(e)

        return report

    async def _backfill_embeddings(
        self,
        memory_ids: set[str],
    ) -> tuple[dict[str, str], list[dict[str, str]]]:
        """Replace missing or stale vectors without aborting the pass."""
        ordered_ids = sorted(memory_ids)
        if self._embed_fn is None:
            return {}, [
                {"memory_id": memory_id, "error": "embedding function not configured"}
                for memory_id in ordered_ids
            ]

        try:
            memories = await self._run_storage(
                self._storage.get_memories,
                ordered_ids,
                visibility="all",
            )
        except Exception as error:
            logger.warning("Failed to load memories missing Qdrant vectors: %s", error)
            return {}, [
                {"memory_id": memory_id, "error": f"memory load failed: {error}"}
                for memory_id in ordered_ids
            ]

        by_id = {memory.id: memory for memory in memories}
        failures: list[dict[str, str]] = []
        batch: list[tuple[str, list[float], dict[str, Any]]] = []
        indexed_content: dict[str, str] = {}
        vector_store = cast(VectorStoreProtocol, self._vector_store)

        async def _flush_batch() -> None:
            nonlocal batch
            if not batch:
                return
            batch_ids = [memory_id for memory_id, _embedding, _payload in batch]
            try:
                await vector_store.batch_upsert(batch)
                indexed_content.update(
                    {memory_id: str(payload["content"]) for memory_id, _embedding, payload in batch}
                )
            except Exception as error:
                logger.warning(
                    "Failed to backfill %s missing Qdrant vector(s): %s",
                    len(batch),
                    error,
                )
                failures.extend(
                    {"memory_id": memory_id, "error": f"vector upsert failed: {error}"}
                    for memory_id in batch_ids
                )
            batch = []

        for memory_id in ordered_ids:
            memory = by_id.get(memory_id)
            if memory is None:
                failures.append(
                    {"memory_id": memory_id, "error": "memory disappeared during reconciliation"}
                )
                continue
            try:
                embedding = await self._embed_fn(
                    memory_embedding_text(memory.content, memory.rationale)
                )
            except Exception as error:
                logger.warning("Failed to embed missing memory %s: %s", memory_id, error)
                failures.append({"memory_id": memory_id, "error": str(error)})
                continue
            batch.append(
                (
                    memory.id,
                    embedding,
                    {
                        "content": memory.content,
                        "project_id": memory.project_id,
                        "is_global": memory.is_global,
                        "memory_type": memory.memory_type.value,
                    },
                )
            )
            if len(batch) >= REINDEX_PAGE_SIZE:
                await _flush_batch()
        await _flush_batch()
        return indexed_content, failures

    async def reindex_embeddings(self, project_id: str | None = None) -> dict[str, Any]:
        """Regenerate embeddings for stored memories."""
        if not self._vector_store or not self._embed_fn:
            return {"success": False, "error": "Vector store or embedding function not configured"}

        if project_id is None:
            return await self._reindex_global_embeddings()

        async with project_write_context(self._vector_store, project_id):
            return await self._reindex_project_embeddings_admitted(project_id)

    async def _reindex_project_embeddings_admitted(self, project_id: str) -> dict[str, Any]:
        total = 0
        try:
            vector_store = self._vector_store
            embed_fn = self._embed_fn
            assert vector_store is not None
            assert embed_fn is not None
            existing_ids = set(await vector_store.scroll_ids(filters={"project_id": project_id}))
            memories = await self.fetch_all_project_memories(project_id)
            total = len(memories)
            if self._reconcile_memory is not None:
                generated = 0
                for memory in memories:
                    if await self._reconcile_memory(memory.id):
                        generated += 1
                incoming_ids = {memory.id for memory in memories}
                stale_ids = sorted(existing_ids - incoming_ids)
                for index in range(0, len(stale_ids), REINDEX_PAGE_SIZE):
                    await vector_store.delete_many(stale_ids[index : index + REINDEX_PAGE_SIZE])
                return {
                    "success": generated == total,
                    "total_memories": total,
                    "embeddings_generated": generated,
                    "skipped": False,
                }
            memory_dicts = self._memory_dicts(memories)
            incoming_ids = {str(mem["id"]) for mem in memory_dicts}
            batch: list[tuple[str, list[float], dict[str, Any]]] = []
            processed = 0
            for mem in memory_dicts:
                mem_id: str = mem["id"]
                embedding = await embed_fn(
                    memory_embedding_text(mem["content"], mem.get("rationale"))
                )
                payload = {k: v for k, v in mem.items() if k not in ("id", "rationale")}
                batch.append((mem_id, embedding, payload))
                if len(batch) >= REINDEX_PAGE_SIZE:
                    await vector_store.batch_upsert(batch)
                    processed += len(batch)
                    logger.info("Reindex progress: %s/%s vectors", processed, total)
                    batch = []
            if batch:
                await vector_store.batch_upsert(batch)
                processed += len(batch)
                logger.info("Reindex progress: %s/%s vectors", processed, total)
            stale_ids = sorted(existing_ids - incoming_ids)
            for index in range(0, len(stale_ids), REINDEX_PAGE_SIZE):
                await vector_store.delete_many(stale_ids[index : index + REINDEX_PAGE_SIZE])
            await self._run_storage(
                self._storage.mark_vectors_reindexed,
                {str(mem["id"]): str(mem["content"]) for mem in memory_dicts},
            )
            generated = len(memory_dicts)
        except Exception as e:
            logger.error("Failed to rebuild vector store: %s", e)
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
        async with global_write_context(self._vector_store):
            return await self._run_global_embedding_reindex_admitted()

    async def _run_global_embedding_reindex_admitted(self) -> dict[str, Any]:
        total = 0
        try:
            memories = await self.fetch_all_memories()
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
            vector_store = cast(VectorStoreProtocol, self._vector_store)
            embed_fn = cast(Callable[..., Any], self._embed_fn)
            await vector_store.rebuild(memory_dicts, embed_fn)
            await self._sweep_rebuild_snapshot(memory_dicts)
            self._last_global_reindex_identity_fingerprint = identity_fingerprint
            self._last_global_reindex_fingerprint = fingerprint
            self._last_global_reindex_completed_at = asyncio.get_running_loop().time()
        except Exception as e:
            logger.error("Failed to rebuild vector store: %s", e)
            return {"success": False, "total_memories": total, "error": str(e)}

        return {
            "success": True,
            "total_memories": total,
            "embeddings_generated": total,
            "skipped": False,
        }

    async def _sweep_rebuild_snapshot(self, memory_dicts: list[dict[str, Any]]) -> None:
        """CAS-clear exact rows and repair/delete mutations missed by a rebuild snapshot."""
        vector_store = cast(VectorStoreProtocol, self._vector_store)
        snapshot = {str(memory["id"]): memory for memory in memory_dicts}
        current = {memory.id: memory for memory in await self.fetch_all_memories()}
        current_memories = list(current.values())
        for start in range(0, len(current_memories), REBUILD_SNAPSHOT_SWEEP_PAGE_SIZE):
            page = current_memories[start : start + REBUILD_SNAPSHOT_SWEEP_PAGE_SIZE]
            snapshots: list[tuple[str, str, str, bool]] = []
            reindex_ids: list[str] = []
            for memory in page:
                scheduled = snapshot.get(memory.id)
                if (
                    scheduled is not None
                    and str(scheduled["content"]) == memory.content
                    and str(scheduled["project_id"]) == memory.project_id
                    and bool(scheduled["is_global"]) == memory.is_global
                ):
                    snapshots.append(
                        (memory.id, memory.content, memory.project_id, memory.is_global)
                    )
                else:
                    reindex_ids.append(memory.id)
            await self._run_storage(
                self._storage.reconcile_vector_snapshot_page,
                snapshots,
                reindex_ids,
            )
        for memory_id in sorted(set(snapshot) - set(current)):
            if self._cleanup_rowless is not None:
                await self._cleanup_rowless(memory_id)
            else:
                await vector_store.delete(memory_id)

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
                logger.error("Failed to clear vectors: %s", e)
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
            logger.error("Failed to clear crossrefs: %s", e)
            report["crossrefs_cleared"] = 0
            report["crossrefs_error"] = str(e)

        scope = f"project {project_id}" if project_id else "all projects"
        logger.info("Indices cleared for %s: %s", scope, report)
        return report

    async def rebuild_indices(self, project_id: str | None = None) -> dict[str, Any]:
        """Rebuild all secondary indices from memory storage."""
        report: dict[str, Any] = {}
        scope = f"project {project_id}" if project_id else "all projects"
        logger.info("Starting index rebuild for %s", scope)

        report["embeddings"] = await self.reindex_embeddings(project_id=project_id)

        if project_id:
            all_memories = await self.fetch_all_project_memories(project_id)
        else:
            all_memories = await self.fetch_all_memories()
        total = len(all_memories)

        crossref_sem = asyncio.Semaphore(10)
        crossref_done = 0
        crossref_done_lock = asyncio.Lock()

        async def _rebuild_crossref(mem: Memory) -> int:
            nonlocal crossref_done
            async with crossref_sem:
                try:
                    if self._rebuild_crossrefs is not None:
                        result = await self._rebuild_crossrefs(mem)
                    else:
                        result = await self._crossref_service.rebuild_for_memory(mem)
                except (CrossrefRebuildError, ValueError) as e:
                    logger.warning("Crossref failed for %s: %s", mem.id, e)
                    result = 0
                async with crossref_done_lock:
                    crossref_done += 1
                    if crossref_done % 50 == 0 or crossref_done == total:
                        logger.info("Crossref progress: %s/%s", crossref_done, total)
                return result

        crossref_results = await asyncio.gather(*[_rebuild_crossref(m) for m in all_memories])
        crossrefs_created = sum(crossref_results)
        logger.info(
            "Crossref rebuild complete: %s links from %s memories", crossrefs_created, total
        )
        report["crossrefs"] = {
            "memories_processed": total,
            "crossrefs_created": crossrefs_created,
        }

        if self._kg_service:
            report["graph_rebuilt"] = await self._kg_rebuilder(project_id)

        logger.info("Index rebuild complete for %s: %s", scope, report)
        return report

    async def invalidate_all(self, project_id: str | None = None) -> dict[str, Any]:
        """Clear all secondary indices for a project (or globally)."""
        return await self.clear_indices(project_id=project_id)

    def _delete_all_memory_crossrefs(self) -> int:
        """Delete every memory_crossrefs row and return the affected row count."""
        return int(self._storage.db.execute("DELETE FROM memory_crossrefs").rowcount)

    async def fetch_all_project_memories(self, project_id: str) -> list[Memory]:
        """Fetch all memories for a project using pagination."""
        return await self.fetch_all_memories(project_id=project_id)

    async def fetch_all_memories(self, project_id: str | None = None) -> list[Memory]:
        """Fetch all active memories using bounded storage pages."""
        all_memories: list[Memory] = []
        offset = 0
        scope = MemoryScope.owner(project_id) if project_id is not None else ALL_MEMORIES
        while True:
            batch = await self._run_storage(
                self._storage.list_memories,
                scope=scope,
                limit=REINDEX_PAGE_SIZE,
                offset=offset,
            )
            if not batch:
                break
            all_memories.extend(batch)
            if len(batch) < REINDEX_PAGE_SIZE:
                break
            offset += len(batch)
        return all_memories

"""Knowledge graph rebuild and read orchestration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from gobby.llm.base import LLMProviderCancellation
from gobby.memory.services.knowledge_graph.models import (
    KnowledgeGraphResult,
    KnowledgeGraphStatus,
)
from gobby.storage.memories import Memory

if TYPE_CHECKING:
    from gobby.memory.falkor_client import FalkorClient
    from gobby.memory.services.knowledge_graph.service import KnowledgeGraphService
    from gobby.storage.memories import LocalMemoryManager

logger = logging.getLogger(__name__)

MAX_REINDEX_LIMIT = 100_000
DEFAULT_GRAPH_LIMIT = 500


class KnowledgeGraphRebuildService:
    """Coordinate knowledge graph clearing, counting, reading, and rebuilds."""

    def __init__(
        self,
        *,
        storage_provider: Callable[[], LocalMemoryManager],
        kg_service_provider: Callable[[], KnowledgeGraphService | None],
        falkor_client_provider: Callable[[], FalkorClient | None],
        run_db: Callable[..., Awaitable[Any]],
        list_memories: Callable[..., list[Memory]],
        fetch_all_project_memories: Callable[[str], Awaitable[list[Memory]]],
        mark_graph_processed: Callable[[str], None],
        max_rebuild_concurrency: int = 2,
    ) -> None:
        if max_rebuild_concurrency < 1:
            raise ValueError("max_rebuild_concurrency must be >= 1")
        self._storage_provider = storage_provider
        self._kg_service_provider = kg_service_provider
        self._falkor_client_provider = falkor_client_provider
        self._run_db = run_db
        self._list_memories = list_memories
        self._fetch_all_project_memories = fetch_all_project_memories
        self._mark_graph_processed = mark_graph_processed
        self._max_rebuild_concurrency = max_rebuild_concurrency

    @property
    def storage(self) -> LocalMemoryManager:
        return self._storage_provider()

    async def clear_knowledge_graph(self, project_id: str | None = None) -> dict[str, Any]:
        """Clear the FalkorDB graph projection and requeue affected memories."""
        kg_service = self._kg_service_provider()
        if not kg_service:
            return {"success": False, "error": "KnowledgeGraphService not initialized"}
        cleared = await kg_service.clear_graph(project_id=project_id)
        pending = await self._run_db(self.storage.mark_pending_graphs, project_id)
        return {"success": True, "memories_marked_pending": pending, **cleared}

    async def get_knowledge_graph_counts(
        self,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Return actual FalkorDB knowledge-graph counts."""
        kg_service = self._kg_service_provider()
        if kg_service:
            return await kg_service.get_graph_counts(project_id=project_id)
        falkor_client = self._falkor_client_provider()
        if falkor_client:
            return await falkor_client.get_graph_counts(project_id=project_id)
        return {"success": False, "error": "FalkorDB not configured"}

    async def rebuild_knowledge_graph(
        self,
        project_id: str | None = None,
        limit: int = MAX_REINDEX_LIMIT,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> dict[str, Any]:
        """Rebuild the FalkorDB knowledge-graph projection from stored memories."""
        kg_service = self._kg_service_provider()
        if not kg_service:
            return {"success": False, "error": "KnowledgeGraphService not initialized"}

        if project_id:
            all_memories = (await self._fetch_all_project_memories(project_id))[:limit]
        else:
            all_memories = await self._run_db(self._list_memories, None, None, limit)

        memories_marked_pending = 0
        for memory in all_memories:
            await self._run_db(self.storage.mark_pending_graph, memory.id)
            memories_marked_pending += 1

        status_counts = {status.value: 0 for status in KnowledgeGraphStatus}
        errors = 0
        processed = 0
        failed_memories: list[dict[str, Any]] = []

        kg_worker_count = min(self._max_rebuild_concurrency, len(all_memories))
        kg_done = 0
        kg_done_lock = asyncio.Lock()

        async def _emit_progress() -> None:
            if progress_callback is None:
                return
            progress = {
                "project_id": project_id,
                "memories_total": len(all_memories),
                "memories_completed": kg_done,
                "memories_marked_processed": processed,
                "memories_marked_pending": memories_marked_pending,
                "status_counts": dict(status_counts),
                "errors": errors,
                "failed_memories": list(failed_memories),
            }
            maybe_awaitable = progress_callback(progress)
            if maybe_awaitable is not None:
                await maybe_awaitable

        async def _rebuild_kg(mem: Memory) -> KnowledgeGraphResult:
            nonlocal errors, kg_done, processed
            try:
                result = await kg_service.add_to_graph(
                    mem.content,
                    memory_id=mem.id,
                    project_id=mem.project_id,
                )
            except LLMProviderCancellation as e:
                logger.info("KG extraction cancelled for memory %s: %s", mem.id, e)
                result = KnowledgeGraphResult(
                    KnowledgeGraphStatus.RETRYABLE_FAILURE,
                    errors=[str(e) or e.__class__.__name__],
                )
            async with kg_done_lock:
                status_counts[result.status.value] += 1
                kg_done += 1
                if result.status in (
                    KnowledgeGraphStatus.SUCCESS,
                    KnowledgeGraphStatus.NOOP_NO_ENTITIES,
                ):
                    await self._run_db(self._mark_graph_processed, mem.id)
                    processed += 1
                else:
                    errors += 1
                    failed_memories.append(
                        {
                            "memory_id": mem.id,
                            "project_id": mem.project_id,
                            "status": result.status.value,
                            "errors": list(result.errors),
                        }
                    )
                await _emit_progress()
                if kg_done % 50 == 0 or kg_done == len(all_memories):
                    logger.info(f"KG extraction progress: {kg_done}/{len(all_memories)}")
            return result

        await _emit_progress()
        queue: asyncio.Queue[Memory | None] = asyncio.Queue()
        for memory in all_memories:
            queue.put_nowait(memory)
        if kg_worker_count > 0:
            for _ in range(kg_worker_count):
                queue.put_nowait(None)

        async def _worker() -> None:
            while True:
                memory = await queue.get()
                try:
                    if memory is None:
                        return
                    await _rebuild_kg(memory)
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(_worker()) for _ in range(kg_worker_count)]
        await queue.join()
        if workers:
            await asyncio.gather(*workers)

        logger.info(
            "KG rebuild complete for %s: %s",
            f"project {project_id}" if project_id else "all projects",
            status_counts,
        )
        return {
            "success": True,
            "memories_processed": len(all_memories),
            "memories_marked_pending": memories_marked_pending,
            "memories_marked_processed": processed,
            "status_counts": status_counts,
            "memories_extracted": status_counts[KnowledgeGraphStatus.SUCCESS.value],
            "noop_no_entities": status_counts[KnowledgeGraphStatus.NOOP_NO_ENTITIES.value],
            "errors": errors,
            "failed_memories": failed_memories,
        }

    async def get_entity_graph(
        self,
        limit: int = DEFAULT_GRAPH_LIMIT,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get the FalkorDB entity graph for visualization."""
        kg_service = self._kg_service_provider()
        if kg_service:
            return await kg_service.get_entity_graph(limit=limit, project_id=project_id)
        falkor_client = self._falkor_client_provider()
        if falkor_client:
            try:
                return await falkor_client.get_entity_graph(
                    limit=limit,
                    project_id=project_id,
                )
            except Exception as e:
                logger.warning(f"FalkorDB query failed: {e}")
                return None
        return None

    async def get_entity_neighbors(
        self,
        entity_key: str,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get neighbors for a single FalkorDB entity."""
        kg_service = self._kg_service_provider()
        if kg_service:
            return await kg_service.get_entity_neighbors(
                entity_key,
                project_id=project_id,
            )
        falkor_client = self._falkor_client_provider()
        if falkor_client:
            try:
                return await falkor_client.get_entity_neighbors(
                    entity_key,
                    project_id=project_id,
                )
            except Exception as e:
                logger.warning(f"FalkorDB query failed: {e}")
                return None
        return None

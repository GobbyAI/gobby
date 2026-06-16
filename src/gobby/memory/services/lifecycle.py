"""Memory lifecycle orchestration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from gobby.memory.protocol import MemoryBackendProtocol, MemoryRecord
from gobby.memory.services.crossref import CrossrefService
from gobby.memory.vectorstore import is_recoverable_vector_store_error
from gobby.storage.memories import LocalMemoryManager, Memory

if TYPE_CHECKING:
    from gobby.config.persistence import MemoryConfig
    from gobby.memory.services.dedup import DedupService
    from gobby.memory.services.knowledge_graph import KnowledgeGraphService
    from gobby.memory.vectorstore import VectorStore

logger = logging.getLogger(__name__)


class MemoryLifecycleService:
    """Create, update, delete, embed, and queue memory side effects."""

    def __init__(
        self,
        *,
        config: MemoryConfig,
        storage_provider: Callable[[], LocalMemoryManager],
        backend_provider: Callable[[], MemoryBackendProtocol],
        vector_store: VectorStore | None,
        embed_fn: Callable[..., Any] | None,
        crossref_service: CrossrefService,
        dedup_service_provider: Callable[[], DedupService | None],
        kg_service_provider: Callable[[], KnowledgeGraphService | None],
        background_tasks: set[asyncio.Task[Any]],
        record_to_memory: Callable[[MemoryRecord], Memory],
        get_memory: Callable[[str], Memory | None],
        embed_and_upsert: Callable[..., Awaitable[None]],
        vector_store_failure_logger: Callable[[str, BaseException], None],
    ) -> None:
        self._config = config
        self._storage_provider = storage_provider
        self._backend_provider = backend_provider
        self._vector_store = vector_store
        self._embed_fn = embed_fn
        self._crossref_service = crossref_service
        self._dedup_service_provider = dedup_service_provider
        self._kg_service_provider = kg_service_provider
        self._background_tasks = background_tasks
        self._record_to_memory = record_to_memory
        self._get_memory = get_memory
        self._embed_and_upsert = embed_and_upsert
        self._log_vector_store_failure = vector_store_failure_logger
        self.embeddings_available: bool | None = None

    @property
    def storage(self) -> LocalMemoryManager:
        return self._storage_provider()

    @property
    def backend(self) -> MemoryBackendProtocol:
        return self._backend_provider()

    async def embed_and_upsert(
        self,
        memory_id: str,
        content: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Embed content and upsert to VectorStore when available."""
        if not self._vector_store or not self._embed_fn:
            return
        if self.embeddings_available is False:
            return
        try:
            embedding = await self._embed_fn(content)
            self.embeddings_available = True
        except Exception as e:
            if self.embeddings_available is None:
                logger.warning(
                    f"Embedding failed for {memory_id}: {e}; "
                    "suppressing further embedding warnings until provider recovers"
                )
                self.embeddings_available = False
            else:
                logger.debug(f"Embedding failed for {memory_id}: {e}")
            return

        try:
            await self._vector_store.upsert(memory_id, embedding, payload or {})
        except Exception as e:
            if is_recoverable_vector_store_error(e):
                self._log_vector_store_failure(f"VectorStore upsert unavailable for {memory_id}", e)
            else:
                logger.warning("VectorStore upsert failed for %s: %s", memory_id, e)

    def fire_background_dedup(
        self,
        content: str,
        project_id: str | None,
        memory_type: str,
        tags: list[str] | None,
        source_type: str,
        source_session_id: str | None,
    ) -> None:
        """Fire a background dedup task."""

        async def _run_dedup() -> None:
            try:
                dedup_service = self._dedup_service_provider()
                if dedup_service is None:
                    return
                await dedup_service.process(
                    content=content,
                    project_id=project_id,
                    memory_type=memory_type,
                    tags=tags,
                    source_type=source_type,
                    source_session_id=source_session_id,
                )
            except Exception as e:
                logger.warning(f"Background dedup failed: {e}")

        task = asyncio.create_task(_run_dedup(), name="memory-dedup")
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def enqueue_for_graph(
        self,
        memory_id: str,
        project_id: str | None = None,
    ) -> None:
        """Queue memory for background KG processing."""
        _ = project_id
        try:
            self.storage.mark_pending_graph(memory_id)
            logger.debug(f"Queued memory {memory_id} for graph processing")
        except Exception as e:
            logger.warning(f"Failed to queue memory {memory_id} for graph: {e}")

    def get_pending_graph_memories(self, limit: int = 20) -> list[Memory]:
        """Get memories pending KG graph processing."""
        return self.storage.get_pending_graph_memories(limit=limit)

    def mark_graph_processed(self, memory_id: str) -> None:
        """Mark a memory as having been processed by the KG pipeline."""
        self.storage.mark_graph_processed(memory_id)

    async def create_memory(
        self,
        content: str,
        memory_type: str = "fact",
        project_id: str | None = None,
        source_type: str = "agent",
        source_session_id: str | None = None,
        tags: list[str] | None = None,
    ) -> Memory:
        """Store a new memory in storage and secondary indices."""
        normalized_content = content.strip()
        if await self.backend.content_exists(normalized_content, project_id):
            existing_record = await self.backend.get_memory_by_content(
                normalized_content, project_id
            )
            if existing_record:
                logger.debug(f"Memory already exists: {existing_record.id}")
                return self._record_to_memory(existing_record)

        # A soft-hidden duplicate is reactivated, not re-created as an invisible
        # row: backend.create funnels through LocalMemoryManager.create_memory,
        # whose deterministic-uuid5 collision path restores the hidden row and
        # returns it active. Keeping the reactivation in that single storage
        # chokepoint also covers direct storage/backend creates that bypass this
        # service precheck entirely.
        record = await self.backend.create(
            content=content,
            memory_type=memory_type,
            project_id=project_id,
            source_type=source_type,
            source_session_id=source_session_id,
            tags=tags,
        )
        memory = self._record_to_memory(record)

        await self._embed_and_upsert(
            memory.id,
            content,
            payload={"project_id": project_id},
        )

        if getattr(self._config, "auto_crossref", False):
            try:
                await self._crossref_service.create(memory)
            except Exception as e:
                logger.warning(f"Auto-crossref failed for {memory.id}: {e}")

        if self._dedup_service_provider():
            self.fire_background_dedup(
                content=content,
                project_id=project_id,
                memory_type=memory_type,
                tags=tags,
                source_type=source_type,
                source_session_id=source_session_id,
            )

        if self._kg_service_provider():
            self.enqueue_for_graph(memory_id=memory.id, project_id=project_id)

        return memory

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory from storage, VectorStore, and FalkorDB."""
        existing_memory = self._get_memory(memory_id)
        result = self.storage.delete_memory(memory_id)
        await self._delete_secondary_indices(memory_id, existing_memory, result)
        return result

    async def adelete_memory(self, memory_id: str) -> bool:
        """Delete a memory through the async backend and secondary indices."""
        existing_memory = self._get_memory(memory_id)
        result = await self.backend.delete(memory_id)
        await self._delete_secondary_indices(memory_id, existing_memory, result)
        return result

    async def _delete_secondary_indices(
        self,
        memory_id: str,
        existing_memory: Memory | None,
        deleted: bool,
    ) -> None:
        if not deleted:
            return
        await self.purge_secondary_indices(
            memory_id,
            project_id=existing_memory.project_id if existing_memory else None,
        )

    async def purge_secondary_indices(
        self,
        memory_id: str,
        project_id: str | None = None,
    ) -> None:
        """Drop a removed memory's VectorStore vector and FalkorDB graph artifacts.

        Used both when a memory is deleted and when the dream GC purge hard-removes an
        aged soft-hidden row, reconciling the secondary stores that retained the row
        until purge. Best-effort: secondary-store faults are logged, not raised, so a
        single unreachable store cannot block reconciliation of the rest.
        """
        if self._vector_store:
            try:
                await self._vector_store.delete(memory_id)
            except Exception as e:
                logger.warning(f"VectorStore delete failed for {memory_id}: {e}")
        kg_service = self._kg_service_provider()
        if kg_service:
            try:
                await kg_service.remove_memory_from_graph(memory_id, project_id=project_id)
            except Exception as e:
                logger.warning(f"Graph delete failed for {memory_id}: {e}")

    async def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> Memory:
        """Update an existing memory and re-embed if content changed."""
        result = self.storage.update_memory(
            memory_id=memory_id,
            content=content,
            tags=tags,
        )
        if content is not None:
            await self._embed_and_upsert(
                memory_id,
                content,
                payload={"project_id": result.project_id},
            )
        return result

    async def aupdate_memory(
        self,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> Memory:
        """Update an existing memory through the async backend."""
        record = await self.backend.update(
            memory_id=memory_id,
            content=content,
            tags=tags,
        )
        memory = self._record_to_memory(record)
        if content is not None:
            await self._embed_and_upsert(
                memory_id,
                content,
                payload={"project_id": memory.project_id},
            )
        return memory

"""Memory lifecycle orchestration."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

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
EMBEDDING_WARNING_INTERVAL_SECONDS = 60.0


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
        embed_and_upsert: Callable[..., Awaitable[bool]],
        vector_store_failure_logger: Callable[[str, BaseException], None],
        run_db: Callable[..., Awaitable[Any]] | None = None,
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
        self._run_db = run_db
        self._last_embedding_warning_at = -EMBEDDING_WARNING_INTERVAL_SECONDS

    async def _run_storage[T](self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        if self._run_db is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return cast(T, await self._run_db(func, *args, **kwargs))

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
    ) -> bool:
        """Embed content and upsert to VectorStore when available."""
        if not self._vector_store or not self._embed_fn:
            return False
        try:
            embedding = await self._embed_fn(content)
        except Exception as e:
            self._log_embedding_failure(memory_id, e)
            return False

        try:
            await self._vector_store.upsert(memory_id, embedding, payload or {})
            return True
        except Exception as e:
            if is_recoverable_vector_store_error(e):
                self._log_vector_store_failure(f"VectorStore upsert unavailable for {memory_id}", e)
            else:
                logger.warning("VectorStore upsert failed for %s: %s", memory_id, e)
            return False

    def _log_embedding_failure(self, memory_id: str, error: BaseException) -> None:
        """Rate-limit warnings without suppressing future embedding attempts."""
        now = time.monotonic()
        message = f"Embedding failed for {memory_id}"
        if now - self._last_embedding_warning_at >= EMBEDDING_WARNING_INTERVAL_SECONDS:
            logger.warning("%s: %s", message, error)
            self._last_embedding_warning_at = now
        else:
            logger.debug("%s: %s", message, error)

    def fire_background_dedup(
        self,
        content: str,
        project_id: str | None,
        memory_type: str,
        tags: list[str] | None,
        source_type: str,
        source_session_id: str | None,
        exclude_memory_id: str | None = None,
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
                    exclude_memory_id=exclude_memory_id,
                )
            except Exception as e:
                logger.warning(f"Background dedup failed: {e}")

        task = asyncio.create_task(_run_dedup(), name="memory-dedup")
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def enqueue_for_graph(
        self,
        memory_id: str,
        project_id: str | None = None,
    ) -> None:
        """Queue memory for background KG processing."""
        _ = project_id
        try:
            await self._run_storage(self.storage.mark_pending_graph, memory_id)
            logger.debug(f"Queued memory {memory_id} for graph processing")
        except Exception as e:
            logger.warning(f"Failed to queue memory {memory_id} for graph: {e}")

    def get_pending_graph_memories(self, limit: int = 20) -> list[Memory]:
        """Get memories pending KG graph processing."""
        return self.storage.get_pending_graph_memories(limit=limit)

    def mark_graph_processed(self, memory_id: str) -> None:
        """Mark a memory as having been processed by the KG pipeline."""
        self.storage.mark_graph_processed(memory_id)

    def record_graph_failure(
        self,
        memory_id: str,
        *,
        deterministic: bool,
        max_attempts: int,
    ) -> str:
        """Persist a graph failure and return the resulting queue status."""
        return self.storage.record_graph_failure(
            memory_id,
            deterministic=deterministic,
            max_attempts=max_attempts,
        )

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
                exclude_memory_id=memory.id,
            )

        if self._kg_service_provider():
            await self.enqueue_for_graph(memory_id=memory.id, project_id=project_id)

        return memory

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory from storage, VectorStore, and FalkorDB."""
        existing_memory = await self._run_storage(self._get_memory, memory_id)
        result = await self._run_storage(self.storage.delete_memory, memory_id)
        await self._delete_secondary_indices(memory_id, existing_memory, result)
        return result

    async def delete_memory_scoped(self, memory_id: str, project_id: str | None) -> bool:
        """Delete a memory only when visible to a project, then reconcile its indices."""
        existing_memory = await self._run_storage(self._get_memory, memory_id)
        result = await self._run_storage(self.storage.delete_memory_scoped, memory_id, project_id)
        await self._delete_secondary_indices(memory_id, existing_memory, result)
        return result

    async def adelete_memory(self, memory_id: str) -> bool:
        """Delete a memory through the async backend and secondary indices."""
        existing_memory = await self._run_storage(self._get_memory, memory_id)
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

    async def sync_memory_scope_indices(
        self,
        memory_id: str,
        project_id: str | None,
    ) -> list[dict[str, str]]:
        """Best-effort secondary sync after a primary-store scope change."""
        failures: list[dict[str, str]] = []
        try:
            await self._run_storage(self.storage.mark_pending_graph, memory_id)
        except Exception as exc:
            logger.warning("Graph scope sync failed for %s: %s", memory_id, exc)
            failures.append({"memory_id": memory_id, "index": "knowledge_graph", "error": str(exc)})
        if self._vector_store:
            try:
                await self._vector_store.set_payload(memory_id, {"project_id": project_id})
            except Exception as exc:
                logger.warning("VectorStore scope sync failed for %s: %s", memory_id, exc)
                failures.append({"memory_id": memory_id, "index": "embedding", "error": str(exc)})
        return failures

    async def restore_memory_indices(
        self,
        memory_id: str,
        content: str,
        project_id: str | None,
    ) -> None:
        """Recreate vector and graph-index state for a restored memory row."""
        await self._embed_and_upsert(
            memory_id,
            content,
            payload={"project_id": project_id},
        )
        try:
            await self._run_storage(self.storage.mark_pending_graph, memory_id)
        except Exception as exc:
            logger.warning("Graph restore sync failed for %s: %s", memory_id, exc)

    async def rescope_memory(self, memory_id: str, new_project_id: str | None) -> Memory:
        """Update a memory's scope, then best-effort sync secondary stores."""
        result = await self._run_storage(self.storage.rescope_memory, memory_id, new_project_id)
        failures = await self.sync_memory_scope_indices(memory_id, result.project_id)
        if failures:
            logger.warning(
                "Memory rescope completed with secondary sync failures for %s: %s",
                memory_id,
                failures,
            )
        return result

    async def _refresh_content_indices(
        self,
        *,
        old_memory: Memory | None,
        memory: Memory,
    ) -> None:
        """Best-effort secondary sync after a memory content revision."""
        indexed = await self._embed_and_upsert(
            memory.id,
            memory.content,
            payload={"project_id": memory.project_id},
        )
        if indexed:
            cleared = await self._run_storage(
                self.storage.mark_vectors_reindexed,
                {memory.id: memory.content},
            )
            if cleared:
                memory.vector_needs_reindex = False

        kg_service = self._kg_service_provider()
        if kg_service:
            try:
                await kg_service.remove_memory_from_graph(
                    memory.id,
                    project_id=old_memory.project_id if old_memory else memory.project_id,
                )
            except Exception as exc:
                logger.warning("Graph content refresh failed for %s: %s", memory.id, exc)

        try:
            await self._run_storage(self.storage.mark_pending_graph, memory.id)
        except Exception as exc:
            logger.warning("Graph requeue failed for %s: %s", memory.id, exc)

        try:
            await self._run_storage(self.storage.delete_crossrefs, memory.id)
        except Exception as exc:
            logger.warning("Crossref cleanup failed for %s: %s", memory.id, exc)

        if getattr(self._config, "auto_crossref", False):
            try:
                await self._crossref_service.rebuild_for_memory(memory)
            except Exception as exc:
                logger.warning("Crossref rebuild failed for %s: %s", memory.id, exc)

    async def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
        memory_type: str | None = None,
    ) -> Memory:
        """Update a memory and refresh secondary indices after content revisions."""
        old_memory = (
            await self._run_storage(self.storage.get_memory, memory_id, visibility="all")
            if content is not None
            else None
        )
        result = await self._run_storage(
            self.storage.update_memory,
            memory_id=memory_id,
            content=content,
            tags=tags,
            memory_type=memory_type,
        )
        if old_memory is not None and old_memory.content != result.content:
            await self._refresh_content_indices(old_memory=old_memory, memory=result)
        return result

    async def aupdate_memory(
        self,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> Memory:
        """Update an existing memory through the async backend."""
        old_record = (
            await self.backend.get(memory_id, visibility="all") if content is not None else None
        )
        record = await self.backend.update(
            memory_id=memory_id,
            content=content,
            tags=tags,
        )
        memory = self._record_to_memory(record)
        if old_record is not None and old_record.content != memory.content:
            await self._refresh_content_indices(
                old_memory=self._record_to_memory(old_record),
                memory=memory,
            )
        return memory

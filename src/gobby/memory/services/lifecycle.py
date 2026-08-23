"""Memory lifecycle orchestration."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from gobby.memory.dream.candidates import memory_to_candidate
from gobby.memory.dream.related import (
    RelatedEvidenceChannelError,
    RelatedEvidenceError,
    RelatedEvidenceSession,
    RetrievalScope,
    gather_related_evidence,
)
from gobby.memory.protocol import MemoryBackendProtocol, MemoryRecord
from gobby.memory.services.crossref import CrossrefService
from gobby.memory.services.secondary_reconciliation import (
    purge_secondary_indices as purge_secondary_indices_impl,
)
from gobby.memory.services.secondary_reconciliation import (
    rebuild_crossrefs_for_memory as rebuild_crossrefs_for_memory_impl,
)
from gobby.memory.services.secondary_reconciliation import (
    reconcile_active_snapshot as reconcile_active_snapshot_impl,
)
from gobby.memory.vectorstore import is_recoverable_vector_store_error
from gobby.memory.write_result import MemoryWriteOutcome
from gobby.projects.fenced_vector_store import global_write_context, project_write_context
from gobby.storage.memories import (
    LocalMemoryManager,
    Memory,
    MemoryScope,
    MemoryType,
    validate_memory_type,
)

if TYPE_CHECKING:
    from gobby.config.persistence import MemoryConfig
    from gobby.memory.services.dedup import DedupService
    from gobby.memory.services.knowledge_graph import KnowledgeGraphService
    from gobby.memory.vectorstore import VectorStore

logger = logging.getLogger(__name__)
EMBEDDING_WARNING_INTERVAL_SECONDS = 60.0
SUPERSESSION_CLEANUP_BUDGET_SECONDS = 5.0
MUTATOR_RECONCILIATION_BUDGET_SECONDS = 5.0
WRITE_MARK_DUE_MAX_CONCURRENCY = 2
_local_mode_dedup_warning_logged = False

# Memory bodies are bounded at write time so an oversize row can never reach the
# recall injection budget. 3,000 comes from the live distribution rather than
# from budget headroom: p99 is 2,468 chars and p99.9 is 3,179, so the cap leaves
# routine records untouched while keeping a worst-case three-memory injection
# inside the existing inline budget.
MAX_MEMORY_CONTENT_CHARS = 3000


def _enforce_content_cap(content: str | None) -> None:
    """Reject memory content over the write cap, naming the length and limit."""
    if content is not None and len(content) > MAX_MEMORY_CONTENT_CHARS:
        raise ValueError(
            f"memory content is {len(content)} characters, over the "
            f"{MAX_MEMORY_CONTENT_CHARS}-character limit; condense it before writing"
        )


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
        self._write_mark_due_semaphore = asyncio.Semaphore(WRITE_MARK_DUE_MAX_CONCURRENCY)
        self._related_evidence_sessions: set[RelatedEvidenceSession] = set()

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
        project_id = payload.get("project_id") if payload else None
        write_context = (
            project_write_context(self._vector_store, str(project_id))
            if project_id is not None
            else global_write_context(self._vector_store)
        )
        try:
            async with write_context:
                return await self._embed_and_upsert_admitted(memory_id, content, payload)
        except Exception as e:
            logger.warning("VectorStore writer admission failed for %s: %s", memory_id, e)
            return False

    async def _embed_and_upsert_admitted(
        self,
        memory_id: str,
        content: str,
        payload: dict[str, Any] | None,
    ) -> bool:
        vector_store = self._vector_store
        embed_fn = self._embed_fn
        assert vector_store is not None
        assert embed_fn is not None
        try:
            embedding = await embed_fn(content)
        except Exception as e:
            self._log_embedding_failure(memory_id, e)
            return False

        try:
            await vector_store.upsert(memory_id, embedding, payload or {})
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
        project_id: str,
        is_global: bool,
        memory_type: str,
        tags: list[str] | None,
        source_type: str,
        source_session_id: str | None,
        exclude_memory_id: str | None = None,
        rationale: str | None = None,
        source_task_id: str | None = None,
        created_by_agent: str | None = None,
    ) -> None:
        """Fire a background dedup task."""
        global _local_mode_dedup_warning_logged
        is_remote = getattr(self._vector_store, "is_remote", None)
        if callable(is_remote) and not is_remote():
            if not _local_mode_dedup_warning_logged:
                logger.warning("Background memory dedup is disabled for local Qdrant mode")
                _local_mode_dedup_warning_logged = True
            return

        async def _run_dedup() -> None:
            try:
                dedup_service = self._dedup_service_provider()
                if dedup_service is None:
                    return
                await dedup_service.process(
                    content=content,
                    project_id=project_id,
                    is_global=is_global,
                    memory_type=memory_type,
                    tags=tags,
                    source_type=source_type,
                    source_session_id=source_session_id,
                    exclude_memory_id=exclude_memory_id,
                    rationale=rationale,
                    source_task_id=source_task_id,
                    created_by_agent=created_by_agent,
                )
            except Exception as e:
                logger.warning("Background dedup failed: %s", e)

        task = asyncio.create_task(_run_dedup(), name="memory-dedup")
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def schedule_write_mark_due(
        self,
        memory: Memory,
        outcome: MemoryWriteOutcome,
    ) -> asyncio.Task[None] | None:
        """Schedule bounded related-memory wakeup for newly active knowledge."""
        if not self._config.dream.write_supersession_mark_due_enabled:
            return None
        if outcome not in {"created", "reactivated"}:
            return None

        anchor_at = memory.created_at if outcome == "created" else memory.updated_at
        scope = (
            RetrievalScope.global_only()
            if memory.is_global
            else RetrievalScope.project_only(memory.project_id)
        )
        expected_project_id = None if memory.is_global else memory.project_id

        async def mark_related_due() -> None:
            session: RelatedEvidenceSession | None = None
            try:
                async with self._write_mark_due_semaphore:
                    session = RelatedEvidenceSession()
                    self._related_evidence_sessions.add(session)
                    candidate = memory_to_candidate(memory, anchor_at)
                    enriched = await gather_related_evidence(
                        [candidate],
                        db=self.storage.db,
                        vector_store=self._vector_store,
                        dream_config=self._config.dream,
                        session=session,
                        scope=scope,
                        temporal_direction="older",
                        anchor_at=anchor_at,
                    )
                    related_ids = [item.id for item in enriched[0].related] if enriched else []
                    if related_ids:
                        await self._run_storage(
                            self.storage.mark_memories_due,
                            related_ids,
                            expected_project_id=expected_project_id,
                        )
            except asyncio.CancelledError:
                raise
            except RelatedEvidenceChannelError as exc:
                logger.warning(
                    "Background related-memory mark-due failed for %s: channel=%s attempts=%d "
                    "detail=%s",
                    memory.id,
                    exc.channel,
                    exc.attempts,
                    exc.detail,
                )
            except RelatedEvidenceError as exc:
                logger.warning(
                    "Background related-memory mark-due failed for %s: %s",
                    memory.id,
                    exc,
                )
            except Exception:
                logger.warning(
                    "Background related-memory mark-due failed for %s",
                    memory.id,
                    exc_info=True,
                )
            finally:
                if session is not None:
                    try:
                        await session.aclose()
                    finally:
                        self._related_evidence_sessions.discard(session)

        task = asyncio.create_task(mark_related_due(), name=f"memory-mark-due-{memory.id}")
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def close_related_evidence_sessions(self) -> None:
        """Concurrently drain any sessions retained after task cancellation."""
        sessions = tuple(self._related_evidence_sessions)
        if sessions:
            await asyncio.gather(
                *(session.aclose() for session in sessions), return_exceptions=True
            )
        self._related_evidence_sessions.clear()

    async def enqueue_for_graph(
        self,
        memory_id: str,
    ) -> None:
        """Queue memory for background KG processing."""
        try:
            await self._run_storage(self.storage.mark_pending_graph, memory_id)
            logger.debug("Queued memory %s for graph processing", memory_id)
        except Exception as e:
            logger.warning("Failed to queue memory %s for graph: %s", memory_id, e)

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
        project_id: str,
        memory_type: str | MemoryType = MemoryType.FACT,
        source_type: str = "agent",
        source_session_id: str | None = None,
        tags: list[str] | None = None,
        supersedes: list[str] | None = None,
        *,
        is_global: bool = False,
        rationale: str | None = None,
        source_task_id: str | None = None,
        created_by_agent: str | None = None,
    ) -> Memory:
        """Store a new memory in storage and secondary indices."""
        _enforce_content_cap(content)
        memory_type = validate_memory_type(memory_type)
        result = await self.backend.create(
            content=content,
            project_id=project_id,
            memory_type=memory_type.value,
            is_global=is_global,
            source_type=source_type,
            source_session_id=source_session_id,
            tags=tags,
            supersedes=supersedes,
            rationale=rationale,
            source_task_id=source_task_id,
            created_by_agent=created_by_agent,
        )
        memory = self._record_to_memory(result.memory)

        for superseded_id in dict.fromkeys(supersedes or []):
            await self.purge_secondary_indices(
                superseded_id,
                project_id=project_id,
                is_global=is_global,
                require_hidden=True,
            )

        if result.outcome == "deduped":
            logger.debug("Memory already exists: %s", memory.id)
            return memory

        await self._reconcile_active_snapshot(memory)

        self.schedule_write_mark_due(memory, result.outcome)

        if self._dedup_service_provider():
            self.fire_background_dedup(
                content=content,
                project_id=project_id,
                is_global=is_global,
                memory_type=memory_type,
                tags=tags,
                source_type=source_type,
                source_session_id=source_session_id,
                exclude_memory_id=memory.id,
                rationale=rationale,
                source_task_id=source_task_id,
                created_by_agent=created_by_agent,
            )

        return memory

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory from storage, VectorStore, and FalkorDB."""
        existing_memory = await self._run_storage(self._get_memory, memory_id)
        result = await self._run_storage(self.storage.delete_memory, memory_id)
        await self._delete_secondary_indices(memory_id, existing_memory, result)
        return result

    async def delete_memory_scoped(self, memory_id: str, project_id: str) -> bool:
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
            is_global=existing_memory.is_global if existing_memory else None,
        )

    async def purge_secondary_indices(
        self,
        memory_id: str,
        project_id: str | None = None,
        is_global: bool | None = None,
        *,
        require_hidden: bool = False,
    ) -> None:
        """Drop a removed memory's VectorStore vector and FalkorDB graph artifacts."""
        await purge_secondary_indices_impl(
            self,
            memory_id,
            project_id,
            is_global,
            require_hidden=require_hidden,
        )

    async def _reconcile_active_snapshot(
        self,
        memory: Memory,
        *,
        graph_cleanup_project_id: str | None = None,
        graph_cleanup_is_global: bool | None = None,
        payload_only: bool = False,
        notify_changed: bool = True,
    ) -> bool:
        """Rebuild secondaries only while the scheduled active row remains locked."""
        return await reconcile_active_snapshot_impl(
            self,
            memory,
            graph_cleanup_project_id=graph_cleanup_project_id,
            graph_cleanup_is_global=graph_cleanup_is_global,
            payload_only=payload_only,
            notify_changed=notify_changed,
        )

    async def sync_memory_scope_indices(
        self,
        memory: Memory,
        *,
        previous_project_id: str | None = None,
        previous_is_global: bool | None = None,
        notify_changed: bool = True,
    ) -> list[dict[str, str]]:
        """Rebuild scope-dependent projections through the active-row fence."""
        reconcile_kwargs: dict[str, Any] = {
            "graph_cleanup_project_id": previous_project_id,
            "graph_cleanup_is_global": previous_is_global,
        }
        if not notify_changed:
            reconcile_kwargs["notify_changed"] = False
        converged = await self._reconcile_active_snapshot(memory, **reconcile_kwargs)
        if converged:
            return []
        return [{"memory_id": memory.id, "index": "secondary", "error": "not converged"}]

    async def restore_memory_indices(
        self,
        memory_id: str,
        content: str,
        project_id: str,
        is_global: bool,
        memory_type: str,
        *,
        notify_changed: bool = True,
    ) -> bool:
        """Recreate secondary state through the active-row fence."""
        try:
            stored = await self._run_storage(
                self.storage.get_memory,
                memory_id,
                visibility="all",
            )
        except ValueError:
            return False
        if (
            stored.content != content
            or stored.project_id != project_id
            or stored.is_global != is_global
            or stored.memory_type != validate_memory_type(memory_type)
        ):
            return False
        return await self._reconcile_active_snapshot(
            stored,
            graph_cleanup_project_id=project_id,
            graph_cleanup_is_global=is_global,
            notify_changed=notify_changed,
        )

    async def reconcile_memory_indices(self, memory_id: str) -> bool:
        """Repair a durable projection intent through the active-row fence."""
        try:
            memory = await self._run_storage(
                self.storage.get_memory,
                memory_id,
                visibility="all",
            )
        except ValueError:
            return False
        if memory.deleted_at is not None:
            return False
        return await self._reconcile_active_snapshot(
            memory,
            graph_cleanup_project_id=memory.project_id,
            graph_cleanup_is_global=memory.is_global,
        )

    async def rebuild_crossrefs_for_memory(
        self,
        memory: Memory,
        threshold: float | None = None,
        max_links: int | None = None,
    ) -> int:
        """Rebuild one memory's links on the shared active-snapshot fence."""
        return await rebuild_crossrefs_for_memory_impl(
            self,
            memory,
            threshold,
            max_links,
        )

    async def move_memory(self, memory_id: str, new_project_id: str) -> Memory:
        """Move memory ownership, then rebuild secondary projections."""
        previous = await self._run_storage(
            self.storage.get_memory,
            memory_id,
            visibility="all",
        )
        result = await self._run_storage(self.storage.move_memory, memory_id, new_project_id)
        failures = await self.sync_memory_scope_indices(
            result,
            previous_project_id=previous.project_id,
            previous_is_global=previous.is_global,
        )
        if failures:
            logger.warning(
                "Memory move completed with secondary sync failures for %s: %s",
                memory_id,
                failures,
            )
        return result

    async def set_memory_global(self, memory_id: str, is_global: bool) -> Memory:
        """Change visibility, then rebuild secondary projections."""
        previous = await self._run_storage(
            self.storage.get_memory,
            memory_id,
            visibility="all",
        )
        result = await self._run_storage(
            self.storage.set_memory_global,
            memory_id,
            is_global,
        )
        failures = await self.sync_memory_scope_indices(
            result,
            previous_project_id=previous.project_id,
            previous_is_global=previous.is_global,
        )
        if failures:
            logger.warning(
                "Memory visibility change completed with secondary sync failures for %s: %s",
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
        """Rebuild content-dependent projections through the active-row fence."""
        await self._reconcile_active_snapshot(
            memory,
            graph_cleanup_project_id=(old_memory.project_id if old_memory else memory.project_id),
            graph_cleanup_is_global=(old_memory.is_global if old_memory else memory.is_global),
        )

    async def _sync_updated_indices(self, old_memory: Memory | None, memory: Memory) -> None:
        if old_memory is None:
            return
        if old_memory.content != memory.content:
            await self._refresh_content_indices(old_memory=old_memory, memory=memory)
        elif old_memory.memory_type != memory.memory_type:
            await self._reconcile_active_snapshot(memory, payload_only=True)

    async def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
        memory_type: str | None = None,
    ) -> Memory:
        """Update a memory and refresh secondary indices after content revisions."""
        _enforce_content_cap(content)
        old_memory = (
            await self._run_storage(self.storage.get_memory, memory_id, visibility="all")
            if content is not None or memory_type is not None
            else None
        )
        result = await self._run_storage(
            self.storage.update_memory,
            memory_id=memory_id,
            content=content,
            tags=tags,
            memory_type=memory_type,
        )
        await self._sync_updated_indices(old_memory, result)
        return result

    async def update_memory_scoped(
        self,
        memory_id: str,
        project_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
        memory_type: str | None = None,
    ) -> Memory:
        """Update a memory visible to a project and refresh its secondary indices."""
        _enforce_content_cap(content)
        old_memory = (
            await self._run_storage(
                self.storage.get_memory,
                memory_id,
                scope=MemoryScope.project_visible(project_id),
                visibility="all",
            )
            if content is not None or memory_type is not None
            else None
        )
        result = await self._run_storage(
            self.storage.update_memory_scoped,
            memory_id=memory_id,
            project_id=project_id,
            content=content,
            tags=tags,
            memory_type=memory_type,
        )
        await self._sync_updated_indices(old_memory, result)
        return result

    async def aupdate_memory(
        self,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> Memory:
        """Update an existing memory through the async backend."""
        _enforce_content_cap(content)
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

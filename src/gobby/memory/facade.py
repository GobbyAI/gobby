"""Compatibility method surface for MemoryManager."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal, cast

from gobby.memory.context import build_memory_context
from gobby.memory.protocol import MemoryRecord
from gobby.memory.services.maintenance import export_markdown as _export_markdown
from gobby.memory.services.maintenance import get_stats as _get_stats
from gobby.memory.services.repository import DEFAULT_LIST_LIMIT, MemoryRepository
from gobby.memory.services.search import DEFAULT_SEARCH_LIMIT, SearchService
from gobby.memory.write_result import MemoryWriteOutcome
from gobby.storage.memories import ALL_MEMORIES, Memory, MemoryScope, Visibility
from gobby.storage.projects import PERSONAL_PROJECT_ID

logger = logging.getLogger(__name__)
_PURGE_SECONDARY_BATCH_SIZE = 64

if TYPE_CHECKING:
    from gobby.memory.services.crossref import CrossrefService
    from gobby.memory.services.indexing import IndexingService
    from gobby.memory.services.keyword import MemoryKeywordSearchService
    from gobby.memory.services.knowledge_graph import KnowledgeGraphRebuildService
    from gobby.memory.services.lifecycle import MemoryLifecycleService
    from gobby.memory.services.projection_repair import (
        ProjectionScopeRepairResult,
        ProjectionScopeRepairService,
    )
    from gobby.memory.services.repository import MemoryRepository as _MemoryRepository
    from gobby.memory.services.search import SearchService as _SearchService
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.memories import LocalMemoryManager

DEFAULT_GRAPH_LIMIT = 500
DEFAULT_RELATIONSHIP_LIMIT = 2000
MAX_REINDEX_LIMIT = 100_000


def _memory_scope(project_id: str | None, *, include_global: bool = True) -> MemoryScope:
    if project_id is None:
        return ALL_MEMORIES
    if include_global:
        return MemoryScope.project_visible(project_id)
    return MemoryScope.project_only(project_id)


__all__ = [
    "DEFAULT_GRAPH_LIMIT",
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_RELATIONSHIP_LIMIT",
    "DEFAULT_SEARCH_LIMIT",
    "MAX_REINDEX_LIMIT",
    "MemoryManagerFacadeMethods",
]


class MemoryManagerFacadeMethods:
    """Delegating compatibility methods supplied to MemoryManager."""

    db: HubDatabase
    storage: LocalMemoryManager
    _crossref_service: CrossrefService
    _indexing_service: IndexingService
    _keyword_service: MemoryKeywordSearchService
    _kg_rebuild_service: KnowledgeGraphRebuildService
    _lifecycle_service: MemoryLifecycleService
    _projection_repair_service: ProjectionScopeRepairService
    _repository: _MemoryRepository
    _search_service: _SearchService
    _vector_store: Any | None

    async def run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _keyword_search(
        self,
        query: str,
        limit: int,
        project_id: str | None = None,
        include_global: bool = True,
    ) -> list[tuple[str, float]]:
        """Run dialect-aware keyword search and return ranked memory IDs."""
        return self._keyword_service.search(
            query,
            limit,
            project_id,
            include_global=include_global,
        )

    @staticmethod
    def _record_to_memory(record: MemoryRecord) -> Memory:
        """Convert a MemoryRecord from the backend to a Memory."""
        return MemoryRepository.record_to_memory(record)

    async def _embed_and_upsert(
        self,
        memory_id: str,
        content: str,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """Embed content and upsert to VectorStore when available."""
        return await self._lifecycle_service.embed_and_upsert(memory_id, content, payload)

    def _fire_background_dedup(
        self,
        content: str,
        project_id: str,
        is_global: bool,
        memory_type: str,
        tags: list[str] | None,
        source_type: str,
        source_session_id: str | None,
    ) -> None:
        """Fire a background dedup task."""
        self._lifecycle_service.fire_background_dedup(
            content,
            project_id,
            is_global,
            memory_type,
            tags,
            source_type,
            source_session_id,
        )

    async def _enqueue_for_graph(
        self,
        memory_id: str,
    ) -> None:
        """Queue memory for background KG processing."""
        await self._lifecycle_service.enqueue_for_graph(memory_id)

    def get_pending_graph_memories(self, limit: int = 20) -> list[Memory]:
        return self._lifecycle_service.get_pending_graph_memories(limit=limit)

    def mark_graph_processed(self, memory_id: str) -> None:
        self._lifecycle_service.mark_graph_processed(memory_id)

    def record_graph_failure(
        self,
        memory_id: str,
        *,
        deterministic: bool,
        max_attempts: int,
    ) -> str:
        return self._lifecycle_service.record_graph_failure(
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
        supersedes: list[str] | None = None,
        *,
        is_global: bool = False,
        rationale: str | None = None,
        source_task_id: str | None = None,
        created_by_agent: str | None = None,
    ) -> Memory:
        return await self._lifecycle_service.create_memory(
            content=content,
            project_id=project_id or PERSONAL_PROJECT_ID,
            memory_type=memory_type,
            source_type=source_type,
            source_session_id=source_session_id,
            tags=tags,
            supersedes=supersedes,
            is_global=is_global,
            rationale=rationale,
            source_task_id=source_task_id,
            created_by_agent=created_by_agent,
        )

    @staticmethod
    def _rrf_scores(*ranked_lists: list[str], k: int = 60) -> dict[str, float]:
        return SearchService.rrf_scores(*ranked_lists, k=k)

    @staticmethod
    def _rrf_merge(*ranked_lists: list[str], k: int = 60) -> list[str]:
        return SearchService.rrf_merge(*ranked_lists, k=k)

    async def search_memories(
        self,
        query: str | None = None,
        project_id: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
        memory_type: str | None = None,
        search_mode: str | None = None,
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
        min_score: float | None = None,
        *,
        embed_text: str | None = None,
        session_id: str | None = None,
        recall_request_id: str | None = None,
        caller: str = "memory.search",
        include_global: bool = True,
    ) -> list[Memory]:
        return await self._search_service.search(
            query=query,
            project_id=project_id,
            limit=limit,
            memory_type=memory_type,
            search_mode=search_mode,
            tags_all=tags_all,
            tags_any=tags_any,
            tags_none=tags_none,
            min_score=min_score,
            embed_text=embed_text,
            session_id=session_id,
            recall_request_id=recall_request_id,
            caller=caller,
            include_global=include_global,
        )

    async def search_memories_as_context(
        self,
        project_id: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> str:
        memories = await self.search_memories(
            project_id=project_id,
            limit=limit,
            caller="memory.context",
        )
        return build_memory_context(memories)

    async def _update_access_stats(self, memories: list[Memory]) -> None:
        await self._search_service.update_access_stats(memories)

    async def _search_graph_for_memories(
        self,
        query_embedding: list[float],
        limit: int = 10,
        min_score: float = 0.5,
        project_id: str | None = None,
    ) -> list[str]:
        return await self._search_service._search_graph_for_memories(
            query_embedding=query_embedding,
            limit=limit,
            min_score=min_score,
            project_id=project_id,
        )

    async def _keyword_ranked(
        self,
        query: str,
        limit: int,
        project_id: str | None,
    ) -> list[str]:
        return await self._search_service._keyword_ranked(query, limit, project_id)

    async def _keyword_fallback(
        self,
        query: str,
        limit: int,
        project_id: str | None,
        memory_type: str | None,
        tags_all: list[str] | None,
        tags_any: list[str] | None,
        tags_none: list[str] | None,
    ) -> list[Memory]:
        return await self._search_service._keyword_fallback(
            query, limit, project_id, memory_type, tags_all, tags_any, tags_none
        )

    async def delete_memory(self, memory_id: str) -> bool:
        return await self._lifecycle_service.delete_memory(memory_id)

    async def delete_memory_scoped(self, memory_id: str, project_id: str) -> bool:
        return await self._lifecycle_service.delete_memory_scoped(memory_id, project_id)

    async def adelete_memory(self, memory_id: str) -> bool:
        return await self._lifecycle_service.adelete_memory(memory_id)

    async def reconcile_stores(self, dry_run: bool = False) -> dict[str, Any]:
        return await self._indexing_service.reconcile_stores(dry_run=dry_run)

    async def reconcile_memory_indices(self, memory_id: str) -> bool:
        """Converge one memory's durable projection intent."""
        return await self._lifecycle_service.reconcile_memory_indices(memory_id)

    def count_memories(
        self,
        project_id: str | None = None,
        memory_type: str | None = None,
        *,
        visibility: Visibility = "active",
    ) -> int:
        return self._repository.count_memories(
            scope=_memory_scope(project_id),
            memory_type=memory_type,
            visibility=visibility,
        )

    def list_dream_candidates(
        self,
        *,
        limit: int,
        redream_cutoff: str,
        scope: MemoryScope,
        memory_type: str | None = None,
    ) -> list[Memory]:
        """Delegate to storage for the nightly dream sweep candidate page."""
        return self.storage.list_dream_candidates(
            limit=limit,
            redream_cutoff=redream_cutoff,
            scope=scope,
            memory_type=memory_type,
        )

    def list_dream_candidate_ids(
        self,
        *,
        redream_cutoff: str,
        scope: MemoryScope,
        memory_type: str | None = None,
        limit: int | None = None,
    ) -> list[str]:
        """Delegate to storage for a stable dry-run candidate snapshot."""
        return self.storage.list_dream_candidate_ids(
            redream_cutoff=redream_cutoff,
            scope=scope,
            memory_type=memory_type,
            limit=limit,
        )

    def get_memories(self, memory_ids: list[str], scope: MemoryScope) -> list[Memory]:
        """Hydrate active memories in requested order for a dream snapshot page."""
        return self.storage.get_memories(memory_ids, scope)

    def list_dream_scopes(self, *, redream_cutoff: str) -> list[MemoryScope]:
        """Delegate to storage for due dream sweep scopes."""
        return self.storage.list_dream_scopes(redream_cutoff=redream_cutoff)

    def mark_project_memories_due(self, project_id: str) -> int:
        """Delegate to storage: clear the dream cooldown for a project's memories."""
        return self.storage.mark_project_memories_due(project_id)

    def mark_global_memories_due(self) -> int:
        """Delegate to storage: clear the dream cooldown for global memories."""
        return self.storage.mark_global_memories_due()

    def mark_memories_due(
        self,
        memory_ids: list[str],
        *,
        expected_project_id: str | None,
    ) -> int:
        """Mark listed active memories due after atomically revalidating scope."""
        return self.storage.mark_memories_due(
            memory_ids,
            expected_project_id=expected_project_id,
        )

    def notify_memory_changed(self) -> None:
        """Notify listeners after a caller-owned transaction commits."""
        self.storage.notify_changed()

    def schedule_write_mark_due(
        self,
        memory: Memory,
        outcome: MemoryWriteOutcome,
    ) -> asyncio.Task[None] | None:
        """Schedule the shared outcome-aware write-time wakeup hook."""
        return self._lifecycle_service.schedule_write_mark_due(memory, outcome)

    def mark_dreamed(
        self,
        memory_id: str,
        *,
        hidden_as: Literal["review", "delete"] | None = None,
        when: str | None = None,
    ) -> bool:
        """Delegate to storage to stamp (and optionally soft-hide) a dreamed row."""
        return self.storage.mark_dreamed(memory_id, hidden_as=hidden_as, when=when)

    async def purge_dream_hidden(self, action: str, older_than_days: int) -> dict[str, Any]:
        """Hard-delete aged soft-hidden rows of one ``dream_action`` and reconcile stores.

        Storage removes the physical rows and returns their IDs; each removed memory's
        VectorStore vector and FalkorDB graph artifacts are then reconciled so the
        secondary stores — which retain soft-hidden rows until purge — stay consistent.
        """
        purged_ids = self.storage.purge_dream_hidden(action, older_than_days)
        for start in range(0, len(purged_ids), _PURGE_SECONDARY_BATCH_SIZE):
            batch = purged_ids[start : start + _PURGE_SECONDARY_BATCH_SIZE]
            results = await asyncio.gather(
                *(
                    self._lifecycle_service.purge_secondary_indices(memory_id)
                    for memory_id in batch
                ),
                return_exceptions=True,
            )
            for memory_id, result in zip(batch, results, strict=True):
                if isinstance(result, Exception):
                    logger.warning(
                        "Failed to purge secondary memory indices for %s",
                        memory_id,
                        exc_info=(type(result), result, result.__traceback__),
                    )
        return {"action": action, "purged": len(purged_ids), "memory_ids": purged_ids}

    def restore_memory(self, memory_id: str, *, when: str | None = None) -> bool:
        """Reactivate a soft-hidden (dream-flagged) memory.

        Clears ``deleted_at``/``dream_action`` and stamps ``last_dreamed_at`` so the
        row is visible to active reads again and is not immediately re-dreamed. The
        secondary stores retain soft-hidden rows until purge, so flipping the primary
        row's visibility is sufficient — no index reconcile is needed. Raises
        ``ValueError`` if the memory does not exist.
        """
        return self.storage.restore_memory(memory_id, when=when)

    def list_memories(
        self,
        project_id: str | None = None,
        memory_type: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
        *,
        visibility: Visibility = "active",
        include_global: bool = True,
    ) -> list[Memory]:
        return self._repository.list_memories(
            _memory_scope(project_id, include_global=include_global),
            memory_type,
            limit,
            offset,
            tags_all,
            tags_any,
            tags_none,
            visibility=visibility,
        )

    async def alist_memories(
        self,
        *,
        project_id: str | None = None,
        memory_type: str | None = None,
        limit: int | None = DEFAULT_LIST_LIMIT,
        offset: int = 0,
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
        visibility: Visibility = "active",
        include_global: bool = True,
    ) -> list[Memory]:
        return await self._repository.alist_memories(
            scope=_memory_scope(project_id, include_global=include_global),
            memory_type=memory_type,
            limit=limit,
            offset=offset,
            tags_all=tags_all,
            tags_any=tags_any,
            tags_none=tags_none,
            visibility=visibility,
        )

    def content_exists(
        self, content: str, project_id: str | None = None, *, visibility: Visibility = "active"
    ) -> bool:
        scope = MemoryScope.project_visible(project_id or PERSONAL_PROJECT_ID)
        return self._repository.content_exists(content, scope, visibility=visibility)

    async def acontent_exists(
        self, content: str, project_id: str | None = None, *, visibility: Visibility = "active"
    ) -> bool:
        scope = MemoryScope.project_visible(project_id or PERSONAL_PROJECT_ID)
        return await self._repository.acontent_exists(content, scope, visibility=visibility)

    def get_memory(
        self,
        memory_id: str,
        project_id: str | None = None,
        *,
        visibility: Visibility = "active",
    ) -> Memory | None:
        return self._repository.get_memory(
            memory_id,
            scope=_memory_scope(project_id),
            visibility=visibility,
        )

    async def aget_memory(
        self,
        memory_id: str,
        project_id: str | None = None,
        *,
        visibility: Visibility = "active",
    ) -> Memory | None:
        return await self._repository.aget_memory(
            memory_id,
            scope=_memory_scope(project_id),
            visibility=visibility,
        )

    def find_by_prefix(
        self,
        prefix: str,
        limit: int = 5,
        project_id: str | None = None,
    ) -> list[Memory]:
        return self._repository.find_by_prefix(prefix, limit, _memory_scope(project_id))

    async def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
        memory_type: str | None = None,
    ) -> Memory:
        return await self._lifecycle_service.update_memory(
            memory_id=memory_id,
            content=content,
            tags=tags,
            memory_type=memory_type,
        )

    async def update_memory_scoped(
        self,
        memory_id: str,
        project_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
        memory_type: str | None = None,
    ) -> Memory:
        return await self._lifecycle_service.update_memory_scoped(
            memory_id=memory_id,
            project_id=project_id,
            content=content,
            tags=tags,
            memory_type=memory_type,
        )

    async def move_memory(self, memory_id: str, new_project_id: str) -> Memory:
        return await self._lifecycle_service.move_memory(memory_id, new_project_id)

    async def promote_memory(self, memory_id: str) -> Memory:
        return await self._lifecycle_service.set_memory_global(memory_id, True)

    async def demote_memory(self, memory_id: str) -> Memory:
        return await self._lifecycle_service.set_memory_global(memory_id, False)

    async def sync_memory_scope_indices(
        self,
        memory: Memory,
        *,
        previous_project_id: str | None = None,
        previous_is_global: bool | None = None,
        notify_changed: bool = True,
    ) -> list[dict[str, str]]:
        return await self._lifecycle_service.sync_memory_scope_indices(
            memory,
            previous_project_id=previous_project_id,
            previous_is_global=previous_is_global,
            notify_changed=notify_changed,
        )

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
        return await self._lifecycle_service.restore_memory_indices(
            memory_id,
            content,
            project_id,
            is_global,
            memory_type,
            notify_changed=notify_changed,
        )

    async def repair_secondary_scope_projections(self) -> ProjectionScopeRepairResult:
        return await self._projection_repair_service.repair()

    async def aupdate_memory(
        self,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> Memory:
        return await self._lifecycle_service.aupdate_memory(memory_id, content, tags)

    async def get_stats(self, project_id: str | None = None) -> dict[str, Any]:
        return await _get_stats(
            self.storage,
            self.db,
            project_id,
            vector_store=self._vector_store,
        )

    async def reindex_embeddings(self, project_id: str | None = None) -> dict[str, Any]:
        return await self._indexing_service.reindex_embeddings(project_id=project_id)

    async def clear_indices(self, project_id: str | None = None) -> dict[str, Any]:
        return await self._indexing_service.clear_indices(project_id=project_id)

    async def rebuild_indices(self, project_id: str | None = None) -> dict[str, Any]:
        return await self._indexing_service.rebuild_indices(project_id=project_id)

    async def invalidate_all(self, project_id: str | None = None) -> dict[str, Any]:
        return await self._indexing_service.invalidate_all(project_id=project_id)

    async def _fetch_all_project_memories(self, project_id: str) -> list[Memory]:
        return await self._indexing_service.fetch_all_project_memories(project_id)

    async def rebuild_crossrefs_for_memory(
        self,
        memory: Memory,
        threshold: float | None = None,
        max_links: int | None = None,
    ) -> int:
        return await self._lifecycle_service.rebuild_crossrefs_for_memory(
            memory,
            threshold,
            max_links,
        )

    async def _create_crossrefs(
        self,
        memory: Memory,
        threshold: float | None = None,
        max_links: int | None = None,
    ) -> int:
        return await self._crossref_service.create(memory, threshold, max_links)

    async def get_related(
        self,
        memory_id: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
        min_similarity: float = 0.0,
        project_id: str | None = None,
    ) -> list[Memory]:
        return cast(
            list[Memory],
            await self.run_db(
                self._crossref_service.get_related,
                memory_id,
                limit,
                min_similarity,
                project_id,
            ),
        )

    async def clear_knowledge_graph(self, project_id: str | None = None) -> dict[str, Any]:
        return await self._kg_rebuild_service.clear_knowledge_graph(project_id=project_id)

    async def get_knowledge_graph_counts(
        self,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._kg_rebuild_service.get_knowledge_graph_counts(project_id=project_id)

    async def rebuild_knowledge_graph(
        self,
        project_id: str | None = None,
        limit: int = MAX_REINDEX_LIMIT,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> dict[str, Any]:
        return await self._kg_rebuild_service.rebuild_knowledge_graph(
            project_id=project_id,
            limit=limit,
            progress_callback=progress_callback,
        )

    async def get_entity_graph(
        self,
        limit: int = DEFAULT_GRAPH_LIMIT,
        relationship_limit: int = DEFAULT_RELATIONSHIP_LIMIT,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        return await self._kg_rebuild_service.get_entity_graph(
            limit=limit,
            relationship_limit=relationship_limit,
            project_id=project_id,
        )

    async def get_entity_neighbors(
        self,
        entity_key: str,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        return await self._kg_rebuild_service.get_entity_neighbors(
            entity_key,
            project_id=project_id,
        )

    def export_markdown(
        self,
        project_id: str | None = None,
        include_metadata: bool = True,
        include_stats: bool = True,
    ) -> str:
        return _export_markdown(self.storage, project_id, include_metadata, include_stats)

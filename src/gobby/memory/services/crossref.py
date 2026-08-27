"""Cross-reference service for memory similarity links."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from psycopg.rows import dict_row
from qdrant_client.models import Filter

from gobby.memory.embedding_text import memory_embedding_text
from gobby.memory.vectorstore import memory_scope_filter
from gobby.storage.memories import ALL_MEMORIES, LocalMemoryManager, Memory, MemoryScope
from gobby.storage.memories_crud import _memory_lock_key

if TYPE_CHECKING:
    from gobby.config.persistence import MemoryConfig
    from gobby.memory.vectorstore import VectorStore

logger = logging.getLogger(__name__)

DEFAULT_SEARCH_LIMIT = 10


def _crossref_scope_filter(project_id: str, is_global: bool) -> Filter:
    """Limit candidate edges to memories visible from the source scope."""
    scope = MemoryScope.global_only() if is_global else MemoryScope.project_visible(project_id)
    scope_filter = memory_scope_filter(scope)
    if scope_filter is None:
        raise RuntimeError("scoped crossref filter was not created")
    return scope_filter


class CrossrefRebuildError(RuntimeError):
    """Raised when cross-reference rebuild fails for a specific memory."""


class CrossrefService:
    """Manages similarity-based cross-references between memories."""

    def __init__(
        self,
        *,
        storage: LocalMemoryManager,
        vector_store: VectorStore | None,
        embed_fn: Callable[..., Awaitable[Any]] | None,
        config: MemoryConfig,
        run_db: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self._storage = storage
        self._vector_store = vector_store
        self._embed_fn = embed_fn
        self._config = config
        self._run_db = run_db

    async def _run_storage(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._run_db is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db(func, *args, **kwargs)

    async def rebuild_for_memory(
        self,
        memory: Memory,
        threshold: float | None = None,
        max_links: int | None = None,
        *,
        connection: Any | None = None,
    ) -> int:
        """Public wrapper for cross-reference creation."""
        try:
            return await self.create(
                memory,
                threshold,
                max_links,
                connection=connection,
            )
        except Exception as exc:
            raise CrossrefRebuildError(str(exc)) from exc

    async def create(
        self,
        memory: Memory,
        threshold: float | None = None,
        max_links: int | None = None,
        *,
        connection: Any | None = None,
    ) -> int:
        """Find and link similar memories using VectorStore search."""
        if not self._vector_store or not self._embed_fn:
            return 0

        threshold = threshold or getattr(self._config, "crossref_threshold", None) or 0.7
        max_links = max_links or getattr(self._config, "crossref_max_links", None) or 5

        embedding = await self._embed_fn(memory_embedding_text(memory.content, memory.rationale))
        results = await self._vector_store.search(
            embedding,
            limit=max_links + 1,
            filters=_crossref_scope_filter(memory.project_id, memory.is_global),
        )

        if connection is not None:
            return await self._replace_fenced(
                memory,
                results,
                threshold,
                max_links,
                connection,
            )

        count = 0
        for other_id, score in results:
            if other_id == memory.id:
                continue
            if score < threshold:
                continue
            if count >= max_links:
                break
            try:
                await self._run_storage(self._storage.create_crossref, memory.id, other_id, score)
                count += 1
            except Exception as e:
                logger.debug("Crossref creation failed: %s", e)

        return count

    async def _replace_fenced(
        self,
        memory: Memory,
        results: list[tuple[str, float]],
        threshold: float,
        max_links: int,
        connection: Any,
    ) -> int:
        """Replace a source's links after locking and revalidating every endpoint."""
        candidate_ids = [other_id for other_id, _score in results if other_id != memory.id]
        lock_ids = sorted({memory.id, *candidate_ids})
        result_scores = dict(results)
        current_scores = await self._current_stored_similarities(
            memory,
            candidate_ids,
            result_scores,
            max_links,
        )
        async with connection.cursor(row_factory=dict_row) as cursor:
            for lock_key in sorted({_memory_lock_key(memory_id) for memory_id in lock_ids}):
                await cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (lock_key,),
                )
            await cursor.execute(
                """
                SELECT id, content, project_id, is_global, deleted_at,
                       vector_needs_reindex
                FROM memories
                WHERE id = ANY(%s)
                ORDER BY id
                FOR SHARE
                """,
                (lock_ids,),
            )
            rows = {str(row["id"]): row for row in await cursor.fetchall()}
            source = rows.get(memory.id)
            if (
                source is None
                or source["deleted_at"] is not None
                or source["content"] != memory.content
                or str(source["project_id"]) != memory.project_id
                or bool(source["is_global"]) != memory.is_global
            ):
                return 0

            await cursor.execute(
                """
                DELETE FROM memory_crossrefs
                WHERE source_id = %s
                """,
                (memory.id,),
            )

            count = 0
            for other_id in candidate_ids:
                if count >= max_links:
                    break
                candidate = rows.get(other_id)
                if candidate is None or candidate["deleted_at"] is not None:
                    continue
                if bool(candidate["vector_needs_reindex"]):
                    continue
                candidate_is_global = bool(candidate["is_global"])
                candidate_project_id = str(candidate["project_id"])
                if memory.is_global:
                    if not candidate_is_global:
                        continue
                elif not candidate_is_global and candidate_project_id != memory.project_id:
                    continue

                score = current_scores[other_id]
                if score < threshold:
                    continue
                await cursor.execute(
                    """
                    INSERT INTO memory_crossrefs
                        (source_id, target_id, similarity)
                    VALUES (%s, %s, %s)
                    ON CONFLICT(source_id, target_id) DO UPDATE SET
                        similarity = excluded.similarity
                    """,
                    (memory.id, other_id, score),
                )
                count += 1
        return count

    async def _current_stored_similarities(
        self,
        memory: Memory,
        candidate_ids: list[str],
        fallbacks: dict[str, float],
        max_links: int,
    ) -> dict[str, float]:
        """Re-read current stored vector scores in one batch."""
        vector_store = self._vector_store
        if vector_store is None:
            return {candidate_id: fallbacks[candidate_id] for candidate_id in candidate_ids}
        try:
            current = await vector_store.search_by_stored_vectors(
                candidate_ids,
                limit=max_links + 1,
                query_filter=_crossref_scope_filter(memory.project_id, memory.is_global),
            )
        except Exception as exc:
            logger.debug("Stored-vector crossref revalidation failed: %s", exc)
            return {candidate_id: fallbacks[candidate_id] for candidate_id in candidate_ids}
        if not isinstance(current, dict):
            return {candidate_id: fallbacks[candidate_id] for candidate_id in candidate_ids}
        return {
            candidate_id: next(
                (
                    score
                    for other_id, score in current.get(candidate_id, [])
                    if other_id == memory.id
                ),
                0.0,
            )
            for candidate_id in candidate_ids
        }

    def get_related(
        self,
        memory_id: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
        min_similarity: float = 0.0,
        project_id: str | None = None,
    ) -> list[Memory]:
        """Get memories linked to this one via cross-references."""
        crossrefs = self._storage.get_crossrefs(
            memory_id, limit=limit, min_similarity=min_similarity
        )
        memories: list[Memory] = []
        scope = ALL_MEMORIES if project_id is None else MemoryScope.project_visible(project_id)
        for ref in crossrefs:
            other_id = ref.target_id if ref.source_id == memory_id else ref.source_id
            try:
                mem = self._storage.get_memory(other_id, scope=scope)
            except ValueError:
                continue
            mem.similarity = ref.similarity
            memories.append(mem)
        return memories

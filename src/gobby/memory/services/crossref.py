"""Cross-reference service for memory similarity links."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from gobby.storage.memories import LocalMemoryManager, Memory

if TYPE_CHECKING:
    from gobby.config.persistence import MemoryConfig
    from gobby.memory.vectorstore import VectorStore

logger = logging.getLogger(__name__)

DEFAULT_SEARCH_LIMIT = 10


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
    ) -> None:
        self._storage = storage
        self._vector_store = vector_store
        self._embed_fn = embed_fn
        self._config = config

    async def rebuild_for_memory(
        self,
        memory: Memory,
        threshold: float | None = None,
        max_links: int | None = None,
    ) -> int:
        """Public wrapper for cross-reference creation."""
        try:
            return await self.create(memory, threshold, max_links)
        except Exception as exc:
            raise CrossrefRebuildError(str(exc)) from exc

    async def create(
        self,
        memory: Memory,
        threshold: float | None = None,
        max_links: int | None = None,
    ) -> int:
        """Find and link similar memories using VectorStore search."""
        if not self._vector_store or not self._embed_fn:
            return 0

        threshold = threshold or getattr(self._config, "crossref_threshold", None) or 0.7
        max_links = max_links or getattr(self._config, "crossref_max_links", None) or 5

        embedding = await self._embed_fn(memory.content)
        results = await self._vector_store.search(embedding, limit=max_links + 1)

        count = 0
        for other_id, score in results:
            if other_id == memory.id:
                continue
            if score < threshold:
                continue
            if count >= max_links:
                break
            try:
                self._storage.create_crossref(memory.id, other_id, score)
                count += 1
            except Exception as e:
                logger.debug(f"Crossref creation failed: {e}")

        return count

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
        for ref in crossrefs:
            other_id = ref.target_id if ref.source_id == memory_id else ref.source_id
            try:
                mem = self._storage.get_memory(other_id, project_id=project_id)
            except ValueError:
                continue
            memories.append(mem)
        return memories

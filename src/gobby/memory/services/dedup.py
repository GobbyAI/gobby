"""
Deduplication service for memory creation.

Uses vector similarity search in Qdrant to detect duplicates and near-duplicates.
Deterministic threshold-based decisions replace the former LLM pipeline.
Falls back to simple storage when VectorStore is unavailable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.memory.vectorstore import VectorStore
    from gobby.storage.memories import LocalMemoryManager, Memory

logger = logging.getLogger(__name__)

# Similarity thresholds for dedup decisions
NEAR_EXACT_THRESHOLD = 0.95  # Score above this → duplicate, skip
SIMILAR_THRESHOLD = 0.85  # Score above this → update if new content is richer


@dataclass
class DedupResult:
    """Result of the dedup pipeline."""

    added: list[Memory] = field(default_factory=list)
    updated: list[Memory] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)


class DedupService:
    """
    Vector similarity deduplication for memories.

    Pipeline:
    1. Embed the new memory content
    2. Search Qdrant for similar existing memories
    3. Apply deterministic threshold decisions (no LLM)
       - score > 0.95 → near-exact duplicate, NOOP
       - score > 0.85 → similar, UPDATE if new content is richer
       - below threshold → genuinely new (already stored by create_memory)

    Falls back to simple store when VectorStore is unavailable.
    """

    def __init__(
        self,
        vector_store: VectorStore,
        storage: LocalMemoryManager,
        embed_fn: Callable[..., Any],
    ):
        self.vector_store = vector_store
        self.storage = storage
        self.embed_fn = embed_fn
        self._embeddings_available: bool | None = None

    async def process(
        self,
        content: str,
        project_id: str | None = None,
        memory_type: str = "fact",
        tags: list[str] | None = None,
        source_type: str = "agent",
        source_session_id: str | None = None,
    ) -> DedupResult:
        """
        Run vector similarity dedup on content.

        Args:
            content: Raw content to process
            project_id: Optional project scope
            memory_type: Memory type for new memories
            tags: Optional tags
            source_type: Origin of memory
            source_session_id: Origin session

        Returns:
            DedupResult with lists of added, updated, and deleted memories
        """
        result = DedupResult()

        # Embed the new memory content
        if self._embeddings_available is False:
            return await self._fallback_store(
                content, project_id, memory_type, tags, source_type, source_session_id
            )
        try:
            embedding = await self.embed_fn(content)
            self._embeddings_available = True
        except Exception as e:
            if self._embeddings_available is None:
                logger.warning(f"Embedding failed, falling back to simple store: {e}")
                self._embeddings_available = False
            return await self._fallback_store(
                content, project_id, memory_type, tags, source_type, source_session_id
            )

        # Search for similar existing memories
        try:
            filters = {"project_id": project_id} if project_id else None
            search_results = await self.vector_store.search(
                query_embedding=embedding,
                limit=5,
                filters=filters,
            )
        except Exception as e:
            logger.warning(f"Vector search failed, falling back to simple store: {e}")
            return await self._fallback_store(
                content, project_id, memory_type, tags, source_type, source_session_id
            )

        # Deterministic threshold decisions
        for memory_id, score in search_results:
            if score > NEAR_EXACT_THRESHOLD:
                # Near-exact duplicate → NOOP
                logger.debug(f"Near-exact duplicate found (score={score:.3f}), skipping")
                return result

            if score > SIMILAR_THRESHOLD:
                # Similar → UPDATE if new content is richer
                try:
                    existing = self.storage.get_memory(memory_id)
                except ValueError:
                    continue
                # TODO: Replace length heuristic with semantic richness comparison
                # Length is a rough proxy — longer content isn't always richer.
                if existing and len(content) > len(existing.content):
                    updated = self.storage.update_memory(memory_id, content=content)
                    await self._embed_and_upsert(memory_id, content, project_id)
                    result.updated.append(updated)
                    return result
                # Existing content is sufficient
                return result

        # Below threshold → genuinely new (already stored by create_memory caller)
        return result

    async def _embed_and_upsert(
        self,
        memory_id: str,
        content: str,
        project_id: str | None = None,
    ) -> None:
        """Embed content and upsert to VectorStore."""
        if self._embeddings_available is False:
            return  # Known-unavailable, skip silently
        try:
            embedding = await self.embed_fn(content)
            await self.vector_store.upsert(
                memory_id=memory_id,
                embedding=embedding,
                payload={
                    "content": content,
                    "project_id": project_id,
                },
            )
            self._embeddings_available = True
        except Exception as e:
            if self._embeddings_available is None:
                logger.warning(f"Embed/upsert failed for {memory_id}: {e}")
                self._embeddings_available = False

    async def _fallback_store(
        self,
        content: str,
        project_id: str | None,
        memory_type: str,
        tags: list[str] | None,
        source_type: str,
        source_session_id: str | None,
    ) -> DedupResult:
        """Fallback: store content directly without dedup."""
        logger.debug("Falling back to simple memory store (vector search unavailable)")
        memory = self.storage.create_memory(
            content=content,
            memory_type=memory_type,
            project_id=project_id,
            source_type=source_type,
            source_session_id=source_session_id,
            tags=tags,
        )
        await self._embed_and_upsert(memory.id, content, project_id)
        return DedupResult(added=[memory])

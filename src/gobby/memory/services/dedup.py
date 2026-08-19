"""
Deduplication service for memory creation.

Uses vector similarity search in Qdrant to detect duplicates and near-duplicates.
Deterministic threshold-based decisions replace the former LLM pipeline.
Falls back to simple storage when VectorStore is unavailable.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from qdrant_client.models import Filter

from gobby.memory.vectorstore import (
    is_recoverable_vector_store_error,
    memory_scope_filter,
)
from gobby.projects.fenced_vector_store import project_write_context
from gobby.storage.memories import MemoryScope, validate_memory_type

if TYPE_CHECKING:
    from gobby.memory.vectorstore import VectorStore
    from gobby.storage.memories import LocalMemoryManager, Memory

logger = logging.getLogger(__name__)

# Similarity thresholds for dedup decisions
NEAR_EXACT_THRESHOLD = 0.95  # Score above this → duplicate, skip
SIMILAR_THRESHOLD = 0.85  # Score above this → update if new content is richer
VECTORSTORE_WARNING_INTERVAL_SECONDS = 60.0
EMBEDDING_WARNING_INTERVAL_SECONDS = 60.0
_DETAIL_MARKER_RE = re.compile(
    r"`[^`]+`|https?://\S+|[/~][\w./-]+|#[0-9]+|\b\d{4}-\d{2}-\d{2}\b|\b\d+(?:\.\d+)?\b"
)
_STRUCTURED_LINE_RE = re.compile(r"^\s*(?:[-*]\s+|\w[\w .-]{0,40}:\s+\S)", re.MULTILINE)
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?]+(?:\s+|$)")
_WORD_RE = re.compile(r"[A-Za-z0-9_'-]+")


def _dedup_scope_filter(project_id: str, is_global: bool) -> Filter:
    """Limit dedup candidates to memories visible from the source scope."""
    scope = MemoryScope.global_only() if is_global else MemoryScope.project_visible(project_id)
    result = memory_scope_filter(scope)
    if result is None:
        raise RuntimeError("scoped dedup filter was not created")
    return result


@dataclass
class DedupResult:
    """Result of the dedup pipeline."""

    added: list[Memory] = field(default_factory=list)
    updated: list[Memory] = field(default_factory=list)


def _memory_richness_score(content: str) -> tuple[int, int, int, int, int]:
    """Score memory detail deterministically without treating length as the main signal."""
    stripped = content.strip()
    if not stripped:
        return (0, 0, 0, 0, 0)

    detail_markers = len(_DETAIL_MARKER_RE.findall(stripped))
    structured_lines = len(_STRUCTURED_LINE_RE.findall(stripped))
    sentence_count = max(1, len(_SENTENCE_BOUNDARY_RE.findall(stripped)))
    unique_words = {word.lower() for word in _WORD_RE.findall(stripped) if len(word) > 2}
    return (detail_markers, structured_lines, sentence_count, len(unique_words), len(stripped))


def _is_richer_memory_content(candidate: str, existing: str) -> bool:
    """Return whether candidate content carries more actionable memory detail."""
    return _memory_richness_score(candidate) > _memory_richness_score(existing)


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
        run_db: Callable[..., Awaitable[Any]] | None = None,
    ):
        self.vector_store = vector_store
        self.storage = storage
        self.embed_fn = embed_fn
        self._run_db = run_db
        self._last_embedding_warning_at = -EMBEDDING_WARNING_INTERVAL_SECONDS
        self._last_vector_store_warning_at = -VECTORSTORE_WARNING_INTERVAL_SECONDS

    async def _run_storage(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._run_db is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db(func, *args, **kwargs)

    async def process(
        self,
        content: str,
        project_id: str,
        is_global: bool = False,
        memory_type: str = "fact",
        tags: list[str] | None = None,
        source_type: str = "agent",
        source_session_id: str | None = None,
        exclude_memory_id: str | None = None,
        rationale: str | None = None,
        source_task_id: str | None = None,
        created_by_agent: str | None = None,
    ) -> DedupResult:
        """Hold writer admission for the detached dedup task's complete lifetime."""
        async with project_write_context(self.vector_store, project_id):
            return await self._process_admitted(
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

    async def _process_admitted(
        self,
        content: str,
        project_id: str,
        is_global: bool = False,
        memory_type: str = "fact",
        tags: list[str] | None = None,
        source_type: str = "agent",
        source_session_id: str | None = None,
        exclude_memory_id: str | None = None,
        rationale: str | None = None,
        source_task_id: str | None = None,
        created_by_agent: str | None = None,
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
            exclude_memory_id: Memory already stored by the caller, which must not deduplicate
                against itself
            rationale: Writer's durable-value claim forwarded on fallback store
            source_task_id: Creating task id forwarded on fallback store
            created_by_agent: Creating agent forwarded on fallback store

        Returns:
            DedupResult with lists of added and updated memories
        """
        memory_type = validate_memory_type(memory_type).value
        result = DedupResult()

        # Embed the new memory content
        try:
            embedding = await self.embed_fn(content)
        except Exception as e:
            self._log_embedding_failure("Embedding failed, falling back to simple store", e)
            if exclude_memory_id is not None:
                return result
            return await self._fallback_store(
                content,
                project_id,
                is_global,
                memory_type,
                tags,
                source_type,
                source_session_id,
                rationale,
                source_task_id,
                created_by_agent,
            )

        # Search for similar existing memories
        try:
            search_results = await self.vector_store.search(
                query_embedding=embedding,
                limit=5,
                filters=_dedup_scope_filter(project_id, is_global),
            )
        except Exception as e:
            if is_recoverable_vector_store_error(e):
                self._log_vector_store_failure(
                    "Vector search unavailable, falling back to simple store", e
                )
            else:
                logger.warning("Vector search failed, falling back to simple store: %s", e)
            if exclude_memory_id is not None:
                return result
            return await self._fallback_store(
                content,
                project_id,
                is_global,
                memory_type,
                tags,
                source_type,
                source_session_id,
                rationale,
                source_task_id,
                created_by_agent,
            )

        # Deterministic threshold decisions
        for memory_id, score in search_results:
            if memory_id == exclude_memory_id:
                continue

            if score > NEAR_EXACT_THRESHOLD:
                # Near-exact duplicate → NOOP
                logger.debug("Near-exact duplicate found (score=%.3f), skipping", score)
                return result

            if score > SIMILAR_THRESHOLD:
                # Similar → UPDATE if new content is richer
                try:
                    existing = await self._run_storage(self.storage.get_memory, memory_id)
                except ValueError:
                    continue
                if existing and _is_richer_memory_content(content, existing.content):
                    if exclude_memory_id is not None:
                        logger.debug(
                            "Similar memory update skipped because content is already stored by excluded source memory %s",
                            exclude_memory_id,
                        )
                        return result
                    updated = await self._run_storage(
                        self.storage.update_memory, memory_id, content=content
                    )
                    await self._embed_and_upsert(
                        memory_id,
                        content,
                        project_id,
                        is_global,
                        updated.memory_type.value,
                    )
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
        project_id: str,
        is_global: bool,
        memory_type: str,
    ) -> None:
        """Embed content and upsert to VectorStore."""
        try:
            embedding = await self.embed_fn(content)
        except Exception as e:
            self._log_embedding_failure(f"Embedding failed for {memory_id}", e)
            return

        try:
            await self.vector_store.upsert(
                memory_id=memory_id,
                embedding=embedding,
                payload={
                    "content": content,
                    "project_id": project_id,
                    "is_global": is_global,
                    "memory_type": memory_type,
                },
            )
        except Exception as e:
            if is_recoverable_vector_store_error(e):
                self._log_vector_store_failure(f"VectorStore upsert unavailable for {memory_id}", e)
            else:
                logger.warning("VectorStore upsert failed for %s: %s", memory_id, e)

    def _log_embedding_failure(self, message: str, error: BaseException) -> None:
        """Rate-limit warnings without suppressing future embedding attempts."""
        now = time.monotonic()
        if now - self._last_embedding_warning_at >= EMBEDDING_WARNING_INTERVAL_SECONDS:
            logger.warning("%s: %s", message, error)
            self._last_embedding_warning_at = now
        else:
            logger.debug("%s: %s", message, error)

    def _log_vector_store_failure(self, message: str, error: BaseException) -> None:
        """Rate-limit noisy VectorStore availability warnings."""
        now = time.monotonic()
        if now - self._last_vector_store_warning_at >= VECTORSTORE_WARNING_INTERVAL_SECONDS:
            logger.warning("%s: %s", message, error)
            self._last_vector_store_warning_at = now
        else:
            logger.debug("%s: %s", message, error)

    async def _fallback_store(
        self,
        content: str,
        project_id: str,
        is_global: bool,
        memory_type: str,
        tags: list[str] | None,
        source_type: str,
        source_session_id: str | None,
        rationale: str | None = None,
        source_task_id: str | None = None,
        created_by_agent: str | None = None,
    ) -> DedupResult:
        """Fallback: store content directly without dedup."""
        logger.debug("Falling back to simple memory store (vector search unavailable)")
        memory = await self._run_storage(
            self.storage.create_memory,
            content=content,
            memory_type=memory_type,
            project_id=project_id,
            is_global=is_global,
            source_type=source_type,
            source_session_id=source_session_id,
            tags=tags,
            rationale=rationale,
            source_task_id=source_task_id,
            created_by_agent=created_by_agent,
        )
        await self._embed_and_upsert(
            memory.id,
            content,
            project_id,
            is_global,
            memory.memory_type.value,
        )
        return DedupResult(added=[memory])

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from gobby.config.persistence import MemoryConfig
from gobby.memory.backends.storage_adapter import StorageAdapter
from gobby.memory.components.ingestion import IngestionService
from gobby.memory.context import build_memory_context
from gobby.memory.neo4j_client import Neo4jClient
from gobby.memory.protocol import MemoryBackendProtocol, MemoryRecord
from gobby.memory.scoring import temporal_decay
from gobby.memory.services.knowledge_graph import KnowledgeGraphService
from gobby.memory.services.maintenance import (
    export_markdown as _export_markdown,
)
from gobby.memory.services.maintenance import (
    get_stats as _get_stats,
)
from gobby.storage.database import DatabaseProtocol
from gobby.storage.memories import LocalMemoryManager, Memory

if TYPE_CHECKING:
    from gobby.llm.service import LLMService
    from gobby.memory.fts_search import MemoryFTS5Searcher
    from gobby.memory.services.dedup import DedupService
    from gobby.memory.vectorstore import VectorStore

logger = logging.getLogger(__name__)

# Boost factor applied to user-sourced memories in search results
_USER_SOURCE_BOOST = 1.2

DEFAULT_LIST_LIMIT = 50
DEFAULT_SEARCH_LIMIT = 10
DEFAULT_GRAPH_LIMIT = 500
MAX_REINDEX_LIMIT = 100_000


class CrossrefRebuildError(RuntimeError):
    """Raised when cross-reference rebuild fails for a specific memory."""


class MemoryManager:
    """
    High-level manager for memory operations.

    Handles storage in SQLite (LocalMemoryManager), vector search via
    Qdrant (VectorStore), cross-references, access stats, and business logic.
    """

    def __init__(
        self,
        db: DatabaseProtocol,
        config: MemoryConfig,
        llm_service: LLMService | None = None,
        vector_store: VectorStore | None = None,
        embed_fn: Callable[..., Any] | None = None,
        *,
        neo4j_url: str | None = None,
        neo4j_auth: str | None = None,
        neo4j_database: str = "neo4j",
        embedding_dim: int = 768,
        collection_prefix: str = "code_symbols_",
    ):
        self.db = db
        self.config = config
        self._llm_service = llm_service
        self._vector_store = vector_store
        self._embed_fn = embed_fn

        # Primary storage layer — always SQLite via LocalMemoryManager
        self.storage = LocalMemoryManager(db)

        # Lazily initialized to avoid import-time surprises from the FTS helper.
        self._fts_searcher: MemoryFTS5Searcher | None = None

        # Backend for async protocol operations (always StorageAdapter)
        self._backend: MemoryBackendProtocol = StorageAdapter(self.storage)

        # Initialize ingestion service for image memories
        self._ingestion_service = IngestionService(
            storage=self.storage,
            backend=self._backend,
            llm_service=llm_service,
        )

        # Track background tasks to prevent GC and surface exceptions
        self._background_tasks: set[asyncio.Task[Any]] = set()

        # Neo4j knowledge graph: initialize client when neo4j_url is configured
        if neo4j_url:
            self._neo4j_client: Neo4jClient | None = Neo4jClient(
                url=neo4j_url,
                auth=neo4j_auth,
                database=neo4j_database,
            )
        else:
            self._neo4j_client = None

        # Track whether embeddings are known-unavailable (log once, skip thereafter)
        self._embeddings_available: bool | None = None

        # DedupService: initialized when VectorStore + embed_fn available (no LLM needed)
        self._dedup_service: DedupService | None = None
        self._kg_service: KnowledgeGraphService | None = None
        if vector_store and embed_fn:
            try:
                from gobby.memory.services.dedup import DedupService as _DedupService

                self._dedup_service = _DedupService(
                    vector_store=vector_store,
                    storage=self.storage,
                    embed_fn=embed_fn,
                )
                logger.debug("DedupService initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize DedupService: {e}")

        # KnowledgeGraphService: requires LLM + Neo4j + VectorStore + embed_fn
        if llm_service and vector_store and embed_fn and self._neo4j_client:
            try:
                from gobby.prompts.loader import PromptLoader

                provider = llm_service.get_default_provider()
                prompt_loader = PromptLoader(db=self.db)
                self._kg_service = KnowledgeGraphService(
                    neo4j_client=self._neo4j_client,
                    llm_provider=provider,
                    embed_fn=embed_fn,
                    prompt_loader=prompt_loader,
                    vector_store=vector_store,
                    code_link_min_score=config.code_link_min_score,
                    code_symbol_collection_prefix=collection_prefix,
                    embedding_dim=embedding_dim,
                    model=config.kg_model,
                )
                logger.debug("KnowledgeGraphService initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize KnowledgeGraphService: {e}")

    async def close(self) -> None:
        """Close underlying clients (Neo4j httpx.AsyncClient, etc.)."""
        if self._neo4j_client:
            try:
                await self._neo4j_client.close()
            except Exception as e:
                logger.warning(f"Failed to close Neo4j client: {e}")
            self._neo4j_client = None
            self._kg_service = None

    def clear_graph_clients(self) -> None:
        """Disable graph features by clearing Neo4j client and KG service."""
        self._neo4j_client = None
        self._kg_service = None

    @property
    def kg_service(self) -> KnowledgeGraphService | None:
        """Get the knowledge graph service."""
        return self._kg_service

    @property
    def vector_store(self) -> Any | None:
        """Get the vector store."""
        return self._vector_store

    @property
    def embed_fn(self) -> Callable[..., Any] | None:
        """Get the embedding function."""
        return self._embed_fn

    @property
    def llm_service(self) -> LLMService | None:
        """Get the LLM service for image description."""
        return self._llm_service

    @llm_service.setter
    def llm_service(self, service: LLMService | None) -> None:
        """Set the LLM service for image description."""
        self._llm_service = service
        self._ingestion_service.llm_service = service

    def _get_fts_searcher(self) -> MemoryFTS5Searcher:
        """Lazily initialize the SQLite FTS5 searcher."""
        if self._fts_searcher is None:
            from gobby.memory.fts_search import MemoryFTS5Searcher

            self._fts_searcher = MemoryFTS5Searcher(self.db)
        return self._fts_searcher

    @staticmethod
    def _record_to_memory(record: MemoryRecord) -> Memory:
        """Convert a MemoryRecord from the backend to a Memory for downstream compatibility."""
        return Memory(
            id=record.id,
            memory_type=cast(
                Literal["fact", "preference", "pattern", "context"], record.memory_type
            ),
            content=record.content,
            created_at=record.created_at.isoformat() if record.created_at else "",
            updated_at=record.updated_at.isoformat() if record.updated_at else "",
            project_id=record.project_id,
            source_type=cast(Literal["user", "agent"], record.source_type or "agent"),
            source_session_id=record.source_session_id,
            access_count=record.access_count,
            last_accessed_at=(
                record.last_accessed_at.isoformat() if record.last_accessed_at else None
            ),
            tags=record.tags or [],
            media=None,  # Media handled separately via MemoryRecord
        )

    # =========================================================================
    # VectorStore helpers
    # =========================================================================

    async def _embed_and_upsert(
        self,
        memory_id: str,
        content: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Embed content and upsert to VectorStore (if available)."""
        if not self._vector_store or not self._embed_fn:
            return
        if self._embeddings_available is False:
            return  # Known-unavailable, skip silently
        try:
            embedding = await self._embed_fn(content)
            await self._vector_store.upsert(memory_id, embedding, payload or {})
            self._embeddings_available = True
        except Exception as e:
            if self._embeddings_available is None:
                # First failure — log once, then suppress
                logger.warning(f"VectorStore upsert failed for {memory_id}: {e}")
                self._embeddings_available = False
            # Subsequent failures silently skipped

    def _fire_background_dedup(
        self,
        content: str,
        project_id: str | None,
        memory_type: str,
        tags: list[str] | None,
        source_type: str,
        source_session_id: str | None,
    ) -> None:
        """Fire a background dedup task (non-blocking).

        The task is tracked in _background_tasks and auto-cleaned via
        a done callback. Exceptions are logged but never propagated.
        """

        async def _run_dedup() -> None:
            try:
                assert self._dedup_service is not None  # noqa: S101
                await self._dedup_service.process(
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

    def _enqueue_for_graph(
        self,
        memory_id: str,
        project_id: str | None = None,
    ) -> None:
        """Queue memory for background KG processing instead of immediate fire-and-forget.

        Marks the memory as pending graph processing. A separate background loop
        (in SessionLifecycleManager) processes the queue on a slower cadence.
        """
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

    # =========================================================================
    # CRUD operations
    # =========================================================================

    async def create_memory(
        self,
        content: str,
        memory_type: str = "fact",
        project_id: str | None = None,
        source_type: str = "agent",
        source_session_id: str | None = None,
        tags: list[str] | None = None,
    ) -> Memory:
        """
        Store a new memory in SQLite and VectorStore.

        Args:
            content: The memory content
            memory_type: Type of memory (fact, preference, etc)
            project_id: Optional project context
            source_type: Origin of memory
            source_session_id: Origin session
            tags: Optional tags
        """
        # Check for existing memory with same content to avoid duplicates.
        normalized_content = content.strip()
        if await self._backend.content_exists(normalized_content, project_id):
            existing_record = await self._backend.get_memory_by_content(
                normalized_content, project_id
            )
            if existing_record:
                logger.debug(f"Memory already exists: {existing_record.id}")
                return self._record_to_memory(existing_record)

        record = await self._backend.create(
            content=content,
            memory_type=memory_type,
            project_id=project_id,
            source_type=source_type,
            source_session_id=source_session_id,
            tags=tags,
        )
        memory = self._record_to_memory(record)

        # Embed and upsert to VectorStore
        await self._embed_and_upsert(
            memory.id,
            content,
            payload={"project_id": project_id},
        )

        # Auto cross-reference if enabled
        if getattr(self.config, "auto_crossref", False):
            try:
                await self._create_crossrefs(memory)
            except Exception as e:
                # Don't fail the create if crossref fails
                logger.warning(f"Auto-crossref failed for {memory.id}: {e}")

        # Fire-and-forget: background dedup task (when DedupService available)
        if self._dedup_service:
            self._fire_background_dedup(
                content=content,
                project_id=project_id,
                memory_type=memory_type,
                tags=tags,
                source_type=source_type,
                source_session_id=source_session_id,
            )

        # Queue for background KG processing (processed on slow cadence)
        if self._kg_service:
            self._enqueue_for_graph(memory_id=memory.id, project_id=project_id)

        return memory

    async def remember_with_image(
        self,
        image_path: str,
        context: str | None = None,
        memory_type: str = "fact",
        project_id: str | None = None,
        source_type: str = "user",
        source_session_id: str | None = None,
        tags: list[str] | None = None,
    ) -> Memory:
        """Store a memory with an image attachment."""
        memory = await self._ingestion_service.remember_with_image(
            image_path=image_path,
            context=context,
            memory_type=memory_type,
            project_id=project_id,
            source_type=source_type,
            source_session_id=source_session_id,
            tags=tags,
        )
        # Embed the described content into VectorStore
        await self._embed_and_upsert(
            memory.id,
            memory.content,
            payload={"project_id": project_id},
        )
        return memory

    async def remember_screenshot(
        self,
        screenshot_bytes: bytes,
        context: str | None = None,
        memory_type: str = "observation",
        project_id: str | None = None,
        source_type: str = "user",
        source_session_id: str | None = None,
        tags: list[str] | None = None,
    ) -> Memory:
        """Store a memory from raw screenshot bytes."""
        memory = await self._ingestion_service.remember_screenshot(
            screenshot_bytes=screenshot_bytes,
            context=context,
            memory_type=memory_type,
            project_id=project_id,
            source_type=source_type,
            source_session_id=source_session_id,
            tags=tags,
        )
        await self._embed_and_upsert(
            memory.id,
            memory.content,
            payload={"project_id": project_id},
        )
        return memory

    async def _search_graph_for_memories(
        self,
        query_embedding: list[float],
        limit: int = 10,
        min_score: float = 0.5,
        project_id: str | None = None,
    ) -> list[str]:
        """Search Neo4j graph for memory IDs via entity vector similarity.

        1. Vector search for similar entities → direct memory IDs
        2. Graph traversal from matched entities → related memory IDs
        3. Return ordered list: direct matches first, then traversed

        Args:
            query_embedding: Query embedding vector
            limit: Maximum memory IDs to return
            min_score: Minimum entity similarity score

        Returns:
            Ranked list of memory IDs (direct matches before traversed)
        """
        assert self._kg_service is not None  # noqa: S101

        # Step 1: Vector search for similar entities
        entity_results = await self._kg_service.search_entities_by_vector(
            query_embedding=query_embedding,
            limit=limit,
            min_score=min_score,
            project_id=project_id,
        )

        if not entity_results:
            return []

        # Collect direct memory IDs (ordered by entity similarity)
        direct_memory_ids: list[str] = []
        entity_names: list[str] = []
        for result in entity_results:
            entity_names.append(result["name"])
            for mid in result.get("memory_ids", []):
                if mid not in direct_memory_ids:
                    direct_memory_ids.append(mid)

        # Step 2: Graph traversal for related memories
        traversed_memory_ids = await self._kg_service.find_related_memory_ids(
            entity_names=entity_names,
            max_hops=2,
            limit=limit,
            project_id=project_id,
        )

        # Step 3: Merge — direct first, then traversed (deduped)
        seen = set(direct_memory_ids)
        merged = list(direct_memory_ids)
        for mid in traversed_memory_ids:
            if mid not in seen:
                seen.add(mid)
                merged.append(mid)

        return merged[:limit]

    @staticmethod
    def _rrf_scores(
        *ranked_lists: list[str],
        k: int = 60,
    ) -> dict[str, float]:
        """Compute Reciprocal Rank Fusion scores for one or more ranked lists."""
        scores: dict[str, float] = {}

        for ranked in ranked_lists:
            for rank, mid in enumerate(ranked):
                scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank + 1)

        return scores

    @staticmethod
    def _rrf_merge(
        *ranked_lists: list[str],
        k: int = 60,
    ) -> list[str]:
        """Merge ranked lists using Reciprocal Rank Fusion.

        RRF score: score(d) = Σ 1/(k + rank_i) across all sources.
        Memories appearing in multiple lists get scores from each, naturally
        ranking higher. k=60 is the standard constant.

        Args:
            *ranked_lists: Variable number of ranked ID lists (Qdrant, graph, FTS5, etc.)
            k: RRF constant (higher = more uniform weighting)

        Returns:
            Merged list of memory IDs sorted by RRF score (descending)
        """
        scores = MemoryManager._rrf_scores(*ranked_lists, k=k)
        return sorted(scores, key=lambda mid: scores[mid], reverse=True)

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
    ) -> list[Memory]:
        """
        Retrieve memories via VectorStore + optional Neo4j graph search.

        When Neo4j is configured, runs Qdrant vector search and graph entity
        search in parallel, then merges results using Reciprocal Rank Fusion.
        User-sourced memories receive a 1.2x score boost.
        If no query, returns memories from SQLite ordered by recency.

        Args:
            query: Optional search query for vector search
            project_id: Filter by project
            limit: Maximum memories to return
            memory_type: Filter by memory type
            search_mode: Ignored (kept for API compatibility)
            tags_all: Memory must have ALL of these tags
            tags_any: Memory must have at least ONE of these tags
            tags_none: Memory must have NONE of these tags
        """
        if query and self._vector_store and self._embed_fn:
            # Use YAKE keyword extraction for noisy queries to improve
            # embedding quality. Raw query still goes to FTS5 unchanged.
            from gobby.search.keywords import extract_keywords

            embed_query = extract_keywords(query) or query
            query_embedding = await self._embed_fn(embed_query, is_query=True)
            half_life = getattr(self.config, "temporal_decay_half_life_days", 30.0)
            effective_min_score = min_score if min_score is not None else 0.0

            def build_results(
                *,
                merged_ids: list[str],
                ranking_score_map: dict[str, float],
                qdrant_score_map: dict[str, float],
                qdrant_set: set[str],
                fts_set: set[str],
                graph_set: set[str] | None = None,
                rrf_applied: bool = False,
            ) -> list[Memory]:
                scored: list[tuple[Memory, float, float | None]] = []
                memories_by_id = {
                    mem.id: mem
                    for mem in self.storage.get_memories(merged_ids, project_id=project_id)
                }

                for memory_id in merged_ids:
                    mem = memories_by_id.get(memory_id)
                    if mem is None:
                        try:
                            mem = self.storage.get_memory(memory_id, project_id=project_id)
                        except ValueError:
                            continue

                    if memory_type and mem.memory_type != memory_type:
                        continue
                    if tags_all and not all(t in (mem.tags or []) for t in tags_all):
                        continue
                    if tags_any and not any(t in (mem.tags or []) for t in tags_any):
                        continue
                    if tags_none and any(t in (mem.tags or []) for t in tags_none):
                        continue

                    raw_semantic_score = qdrant_score_map.get(memory_id)
                    decay_factor: float | None = None
                    similarity: float | None = None
                    if raw_semantic_score is not None:
                        similarity = raw_semantic_score
                        if mem.source_type == "user":
                            similarity *= _USER_SOURCE_BOOST
                        decay_factor = temporal_decay(mem.updated_at, half_life)
                        similarity *= decay_factor

                    if effective_min_score > 0 and (
                        similarity is None or similarity < effective_min_score
                    ):
                        continue

                    sources = []
                    if memory_id in qdrant_set:
                        sources.append("semantic")
                    if graph_set and memory_id in graph_set:
                        sources.append("graph")
                    if memory_id in fts_set:
                        sources.append("fts5")

                    mem.search_via = "|".join(sources) or "unknown"
                    mem.raw_semantic_score = raw_semantic_score
                    mem.temporal_decay_factor = decay_factor
                    mem.similarity = similarity
                    mem.ranking_score = ranking_score_map.get(memory_id, 0.0)
                    if rrf_applied:
                        mem.ranking_mode = "rrf"
                    elif raw_semantic_score is not None:
                        mem.ranking_mode = "semantic_only"
                    else:
                        mem.ranking_mode = "nonsemantic_fallback"

                    scored.append((mem, mem.ranking_score, similarity))

                scored.sort(
                    key=lambda item: (
                        item[2] is not None,
                        item[2] if item[2] is not None else float("-inf"),
                        item[1],
                    ),
                    reverse=True,
                )
                return [mem for mem, _, _ in scored[:limit]]

            # Build filters for VectorStore
            filters: dict[str, Any] = {}
            if project_id:
                filters["project_id"] = project_id

            # Run Qdrant search (always) and graph search (when available) in parallel
            use_graph = self._kg_service is not None and getattr(
                self.config, "neo4j_graph_search", True
            )

            if use_graph:
                graph_min_score = getattr(self.config, "neo4j_graph_min_score", 0.5)
                rrf_k = getattr(self.config, "neo4j_rrf_k", 60)

                qdrant_coro = self._vector_store.search(
                    query_embedding,
                    limit=limit * 2,
                    filters=filters or None,
                )
                graph_coro = self._search_graph_for_memories(
                    query_embedding=query_embedding,
                    limit=limit * 2,
                    min_score=graph_min_score,
                    project_id=project_id,
                )
                fts_coro = self._fts5_ranked(query, limit * 2, project_id)

                qdrant_result, graph_result, fts_result = await asyncio.gather(
                    qdrant_coro, graph_coro, fts_coro, return_exceptions=True
                )

                # Handle Qdrant results (or fallback to empty)
                if isinstance(qdrant_result, BaseException):
                    logger.warning(f"Qdrant search failed: {qdrant_result}")
                    qdrant_results: list[tuple[str, float]] = []
                else:
                    qdrant_results = qdrant_result

                # Handle graph results (graceful degradation)
                if isinstance(graph_result, BaseException):
                    logger.warning(f"Graph search failed: {graph_result}")
                    graph_ranked: list[str] = []
                else:
                    graph_ranked = graph_result

                # Handle FTS5 results (graceful degradation)
                if isinstance(fts_result, BaseException):
                    logger.debug(f"FTS5 search failed: {fts_result}")
                    fts_ranked: list[str] = []
                else:
                    fts_ranked = fts_result

                qdrant_score_map = dict(qdrant_results)
                qdrant_ranked = [mid for mid, _ in qdrant_results]

                rrf_lists = [rl for rl in (qdrant_ranked, graph_ranked, fts_ranked) if rl]
                if len(rrf_lists) > 1:
                    ranking_score_map = self._rrf_scores(*rrf_lists, k=rrf_k)
                    merged_ids = sorted(
                        ranking_score_map,
                        key=lambda memory_id: ranking_score_map[memory_id],
                        reverse=True,
                    )
                    rrf_applied = True
                elif rrf_lists:
                    merged_ids = rrf_lists[0]
                    rrf_applied = False
                    if qdrant_ranked:
                        ranking_score_map = qdrant_score_map.copy()
                    else:
                        ranking_score_map = self._rrf_scores(merged_ids, k=rrf_k)
                else:
                    merged_ids = []
                    rrf_applied = False
                    ranking_score_map = {}

                qdrant_set = set(qdrant_ranked)
                graph_set = set(graph_ranked)
                fts_set = set(fts_ranked)
                memories = build_results(
                    merged_ids=merged_ids,
                    ranking_score_map=ranking_score_map,
                    qdrant_score_map=qdrant_score_map,
                    qdrant_set=qdrant_set,
                    fts_set=fts_set,
                    graph_set=graph_set,
                    rrf_applied=rrf_applied,
                )
            else:
                # Qdrant + FTS5 path (no graph search)
                rrf_k = getattr(self.config, "neo4j_rrf_k", 60)

                qdrant_coro = self._vector_store.search(
                    query_embedding,
                    limit=limit * 2,
                    filters=filters or None,
                )
                fts_coro = self._fts5_ranked(query, limit * 2, project_id)

                qdrant_result, fts_result = await asyncio.gather(
                    qdrant_coro, fts_coro, return_exceptions=True
                )

                if isinstance(qdrant_result, BaseException):
                    logger.warning(f"Qdrant search failed: {qdrant_result}")
                    qdrant_results = []
                else:
                    qdrant_results = qdrant_result

                if isinstance(fts_result, BaseException):
                    logger.debug(f"FTS5 search failed: {fts_result}")
                    fts_ranked = []
                else:
                    fts_ranked = fts_result

                qdrant_ranked = [mid for mid, _ in qdrant_results]
                qdrant_score_map = dict(qdrant_results)
                fts_set = set(fts_ranked)
                qdrant_set = set(qdrant_ranked)

                if qdrant_ranked and fts_ranked:
                    ranking_score_map = self._rrf_scores(qdrant_ranked, fts_ranked, k=rrf_k)
                    merged_ids = sorted(
                        ranking_score_map,
                        key=lambda memory_id: ranking_score_map[memory_id],
                        reverse=True,
                    )
                    rrf_applied = True
                elif qdrant_ranked:
                    merged_ids = qdrant_ranked
                    ranking_score_map = qdrant_score_map.copy()
                    rrf_applied = False
                elif fts_ranked:
                    merged_ids = fts_ranked
                    ranking_score_map = self._rrf_scores(fts_ranked, k=rrf_k)
                    rrf_applied = False
                else:
                    merged_ids = []
                    ranking_score_map = {}
                    rrf_applied = False

                memories = build_results(
                    merged_ids=merged_ids,
                    ranking_score_map=ranking_score_map,
                    qdrant_score_map=qdrant_score_map,
                    qdrant_set=qdrant_set,
                    fts_set=fts_set,
                    rrf_applied=rrf_applied,
                )
        else:
            # No query or no VectorStore — try FTS5 keyword search if we
            # have a query, otherwise fall back to recency-ordered SQLite list.
            if query:
                memories = await self._fts5_fallback(
                    query,
                    limit,
                    project_id,
                    memory_type,
                    tags_all,
                    tags_any,
                    tags_none,
                )
            else:
                memories = self.storage.list_memories(
                    project_id=project_id,
                    memory_type=memory_type,
                    limit=limit,
                    tags_all=tags_all,
                    tags_any=tags_any,
                    tags_none=tags_none,
                )

        # Update access stats for retrieved memories
        self._update_access_stats(memories)

        return memories

    async def _fts5_ranked(
        self,
        query: str,
        limit: int,
        project_id: str | None,
    ) -> list[str]:
        """Run FTS5 keyword search and return ranked memory IDs for RRF merge."""
        fts_results = await asyncio.to_thread(
            self._get_fts_searcher().search,
            query,
            limit,
            project_id,
        )
        return [mem_id for mem_id, _ in fts_results]

    async def _fts5_fallback(
        self,
        query: str,
        limit: int,
        project_id: str | None,
        memory_type: str | None,
        tags_all: list[str] | None,
        tags_any: list[str] | None,
        tags_none: list[str] | None,
    ) -> list[Memory]:
        """FTS5 keyword search fallback when vector search returns nothing."""
        fts_results = await asyncio.to_thread(
            self._get_fts_searcher().search,
            query,
            limit * 2,
            project_id,
        )
        if not fts_results:
            return []

        memories: list[Memory] = []
        for mem_id, score in fts_results:
            try:
                mem = await asyncio.to_thread(self.storage.get_memory, mem_id)
            except ValueError:
                continue
            if memory_type and mem.memory_type != memory_type:
                continue
            if tags_all and not all(t in (mem.tags or []) for t in tags_all):
                continue
            if tags_any and not any(t in (mem.tags or []) for t in tags_any):
                continue
            if tags_none and any(t in (mem.tags or []) for t in tags_none):
                continue
            mem.similarity = score
            mem.search_via = "fts5"
            memories.append(mem)
            if len(memories) >= limit:
                break
        return memories

    async def search_memories_as_context(
        self,
        project_id: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> str:
        """
        Retrieve memories and format them as context for LLM prompts.

        Returns:
            Formatted markdown string wrapped in <project-memory> tags,
            or empty string if no memories found
        """
        memories = await self.search_memories(
            project_id=project_id,
            limit=limit,
        )
        return build_memory_context(memories)

    def _update_access_stats(self, memories: list[Memory]) -> None:
        """Update access count and time for memories (debounced)."""
        if not memories:
            return

        now = datetime.now(UTC)
        debounce_seconds = getattr(self.config, "access_debounce_seconds", 60)

        for memory in memories:
            if memory.last_accessed_at:
                try:
                    last_access = datetime.fromisoformat(memory.last_accessed_at)
                    if last_access.tzinfo is None:
                        last_access = last_access.replace(tzinfo=UTC)
                    seconds_since = (now - last_access).total_seconds()
                    if seconds_since < debounce_seconds:
                        continue
                except (ValueError, TypeError):
                    pass

            try:
                self.storage.update_access_stats(memory.id, now.isoformat())
            except Exception as e:
                if "malformed" in str(e):
                    logger.warning(
                        f"Failed to update access stats for {memory.id}: {e} "
                        "(likely FTS trigger issue — see memory FTS repair docs)"
                    )
                else:
                    logger.warning(f"Failed to update access stats for {memory.id}: {e}")

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory from SQLite, VectorStore, and Neo4j."""
        result = self.storage.delete_memory(memory_id)
        if result and self._vector_store:
            try:
                await self._vector_store.delete(memory_id)
            except Exception as e:
                logger.warning(f"VectorStore delete failed for {memory_id}: {e}")
        if result and self._kg_service:
            try:
                await self._kg_service.remove_memory_from_graph(memory_id)
            except Exception as e:
                logger.warning(f"Graph delete failed for {memory_id}: {e}")
        return result

    async def adelete_memory(self, memory_id: str) -> bool:
        """Delete a memory (async version via backend)."""
        result = await self._backend.delete(memory_id)
        if result and self._vector_store:
            try:
                await self._vector_store.delete(memory_id)
            except Exception as e:
                logger.warning(f"VectorStore delete failed for {memory_id}: {e}")
        if result and self._kg_service:
            try:
                await self._kg_service.remove_memory_from_graph(memory_id)
            except Exception as e:
                logger.warning(f"Graph delete failed for {memory_id}: {e}")
        return result

    async def reconcile_stores(self, dry_run: bool = False) -> dict[str, Any]:
        """Reconcile Qdrant and Neo4j with SQLite source of truth.

        Finds orphaned vectors and graph nodes whose memory IDs no longer
        exist in SQLite, and deletes them.
        """
        sqlite_ids = set(self.storage.list_all_ids())
        report: dict[str, Any] = {
            "dry_run": dry_run,
            "sqlite_count": len(sqlite_ids),
            "qdrant": {"orphans_found": 0, "orphans_deleted": 0, "errors": 0},
            "neo4j": {
                "orphan_memories_found": 0,
                "orphan_memories_deleted": 0,
                "orphan_entities_deleted": 0,
                "errors": 0,
            },
        }

        # Reconcile Qdrant
        if self._vector_store:
            try:
                qdrant_ids = set(await self._vector_store.scroll_ids())
                orphaned = qdrant_ids - sqlite_ids
                report["qdrant"]["total"] = len(qdrant_ids)
                report["qdrant"]["orphans_found"] = len(orphaned)

                if not dry_run and orphaned:
                    try:
                        await self._vector_store.delete_many(list(orphaned))
                        report["qdrant"]["orphans_deleted"] = len(orphaned)
                    except Exception as e:
                        logger.warning(
                            f"Batch delete of {len(orphaned)} Qdrant orphans failed: {e}"
                        )
                        report["qdrant"]["errors"] += len(orphaned)
            except Exception as e:
                logger.error(f"Qdrant reconciliation failed: {e}")
                report["qdrant"]["error"] = str(e)

        # Reconcile Neo4j
        if self._kg_service:
            try:
                neo4j_ids = await self._kg_service.get_all_memory_node_ids()
                orphaned = neo4j_ids - sqlite_ids
                report["neo4j"]["total"] = len(neo4j_ids)
                report["neo4j"]["orphan_memories_found"] = len(orphaned)

                if not dry_run and orphaned:
                    deleted = await self._kg_service.remove_memories_from_graph(orphaned)
                    report["neo4j"]["orphan_memories_deleted"] = deleted
                    if deleted < len(orphaned):
                        report["neo4j"]["errors"] += len(orphaned) - deleted

                    # Clean orphaned entities after removing memory nodes
                    entities_deleted = await self._kg_service.remove_orphaned_entities()
                    report["neo4j"]["orphan_entities_deleted"] = entities_deleted
            except Exception as e:
                logger.error(f"Neo4j reconciliation failed: {e}")
                report["neo4j"]["error"] = str(e)

        return report

    def count_memories(self, project_id: str | None = None) -> int:
        """Return the total number of memories using COUNT(*)."""
        return self.storage.count_memories(project_id=project_id)

    def list_memories(
        self,
        project_id: str | None = None,
        memory_type: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
    ) -> list[Memory]:
        """List memories with optional filtering (SQLite only)."""
        return self.storage.list_memories(
            project_id=project_id,
            memory_type=memory_type,
            limit=limit,
            offset=offset,
            tags_all=tags_all,
            tags_any=tags_any,
            tags_none=tags_none,
        )

    async def alist_memories(
        self,
        project_id: str | None = None,
        memory_type: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> list[Memory]:
        """List memories via backend (async)."""
        records = await self._backend.list_memories(
            project_id=project_id,
            memory_type=memory_type,
            limit=limit,
            offset=offset,
        )
        return [self._record_to_memory(r) for r in records]

    def content_exists(self, content: str, project_id: str | None = None) -> bool:
        """Check if a memory with identical content already exists."""
        return self.storage.content_exists(content, project_id)

    async def acontent_exists(self, content: str, project_id: str | None = None) -> bool:
        """Check if a memory with identical content already exists (async)."""
        return await self._backend.content_exists(content, project_id)

    def get_memory(self, memory_id: str, project_id: str | None = None) -> Memory | None:
        """Get a specific memory by ID, optionally scoped to a project.

        Args:
            memory_id: The memory UUID to look up
            project_id: If provided, only return if memory belongs to this project
                or is global (project_id IS NULL)
        """
        try:
            return self.storage.get_memory(memory_id, project_id=project_id)
        except ValueError:
            return None

    async def aget_memory(self, memory_id: str, project_id: str | None = None) -> Memory | None:
        """Get a specific memory by ID (async).

        Args:
            memory_id: The memory UUID to look up
            project_id: If provided, validates memory belongs to this project
                after retrieval from backend
        """
        record = await self._backend.get(memory_id)
        if record:
            # Post-fetch project scoping (backend protocol has no project_id param).
            # Matches sync get_memory semantics: returns memory if it belongs to the
            # requested project OR is global (project_id IS NULL).
            if project_id and record.project_id and record.project_id != project_id:
                return None
            return self._record_to_memory(record)
        return None

    def find_by_prefix(
        self, prefix: str, limit: int = 5, project_id: str | None = None
    ) -> list[Memory]:
        """Find memories whose IDs start with the given prefix.

        Args:
            prefix: ID prefix to match
            limit: Maximum results to return
            project_id: If provided, only return memories belonging to this
                project or global memories (project_id IS NULL)
        """
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        if project_id:
            rows = self.db.fetchall(
                "SELECT * FROM memories WHERE id LIKE ? ESCAPE '\\' AND (project_id = ? OR project_id IS NULL) LIMIT ?",
                (f"{escaped}%", project_id, limit),
            )
        else:
            rows = self.db.fetchall(
                "SELECT * FROM memories WHERE id LIKE ? ESCAPE '\\' LIMIT ?",
                (f"{escaped}%", limit),
            )
        return [Memory.from_row(row) for row in rows]

    async def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> Memory:
        """
        Update an existing memory in SQLite and re-embed if content changed.

        Args:
            memory_id: The memory to update
            content: New content (optional)
            tags: New tags (optional)

        Returns:
            Updated Memory object

        Raises:
            ValueError: If memory not found
        """
        result = self.storage.update_memory(
            memory_id=memory_id,
            content=content,
            tags=tags,
        )

        # Re-embed if content changed
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
        """Update an existing memory (async via backend)."""
        record = await self._backend.update(
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

    def get_stats(self, project_id: str | None = None) -> dict[str, Any]:
        """Get statistics about stored memories."""
        return _get_stats(self.storage, self.db, project_id, vector_store=self._vector_store)

    # =========================================================================
    # Reindexing
    # =========================================================================

    async def reindex_embeddings(self, project_id: str | None = None) -> dict[str, Any]:
        """Regenerate embeddings for stored memories.

        When project_id is given, deletes vectors for that project and
        re-embeds only its memories.  When None, uses VectorStore.rebuild()
        to recreate the entire collection (handles dimension changes).
        """
        if not self._vector_store or not self._embed_fn:
            return {"success": False, "error": "Vector store or embedding function not configured"}

        memories = self.list_memories(project_id=project_id, limit=MAX_REINDEX_LIMIT)
        total = len(memories)
        memory_dicts: list[dict[str, Any]] = [
            {"id": mem.id, "content": mem.content, "project_id": mem.project_id} for mem in memories
        ]

        try:
            if project_id is None:
                # Global: nuke and rebuild entire collection
                await self._vector_store.rebuild(memory_dicts, self._embed_fn)
            else:
                # Project-scoped: delete by filter, then re-embed
                await self._vector_store.delete(filters={"project_id": project_id})
                batch: list[tuple[str, list[float], dict[str, Any]]] = []
                for mem in memory_dicts:
                    mem_id: str = mem["id"]
                    embedding = await self._embed_fn(mem["content"])
                    payload = {k: v for k, v in mem.items() if k != "id"}
                    batch.append((mem_id, embedding, payload))
                    if len(batch) >= 500:
                        await self._vector_store.batch_upsert(batch)
                        logger.info(f"Reindex progress: {len(batch)}/{total} vectors")
                        batch = []
                if batch:
                    await self._vector_store.batch_upsert(batch)
            generated = len(memory_dicts)
        except Exception as e:
            logger.error(f"Failed to rebuild vector store: {e}")
            return {"success": False, "total_memories": total, "error": str(e)}

        return {"success": True, "total_memories": total, "embeddings_generated": generated}

    async def clear_indices(self, project_id: str | None = None) -> dict[str, Any]:
        """Fast wipe of all secondary indices for a project (or all projects).

        Clears Neo4j graph, Qdrant vectors, crossrefs, and FTS5.  Does NOT
        delete memories from SQLite — that remains the source of truth.

        Returns immediately; the caller is responsible for scheduling a
        rebuild via ``rebuild_indices`` if desired.
        """
        report: dict[str, Any] = {}

        # 1. Clear Neo4j graph
        if self._kg_service:
            if project_id:
                report["graph_cleared"] = await self._kg_service.clear_project_graph(project_id)
            else:
                # TODO: add clear_all_graphs when needed; for now per-project only
                report["graph_cleared"] = {
                    "skipped": True,
                    "reason": "global clear not implemented",
                }

        # 2. Clear Qdrant vectors
        if self._vector_store:
            try:
                if project_id:
                    await self._vector_store.delete(filters={"project_id": project_id})
                else:
                    await self._vector_store.delete_collection(self._vector_store._collection_name)
                report["vectors_cleared"] = True
            except Exception as e:
                logger.error(f"Failed to clear vectors: {e}")
                report["vectors_cleared"] = False
                report["vectors_error"] = str(e)

        # 3. Clear crossrefs
        try:
            if project_id:
                deleted = await asyncio.to_thread(self.storage.delete_project_crossrefs, project_id)
            else:
                deleted = await asyncio.to_thread(
                    lambda: self.storage.db.execute("DELETE FROM memory_crossrefs").rowcount
                )
            report["crossrefs_cleared"] = deleted
        except Exception as e:
            logger.error(f"Failed to clear crossrefs: {e}")
            report["crossrefs_cleared"] = 0
            report["crossrefs_error"] = str(e)

        # 4. Clear FTS5 (fast — just a DELETE, no per-row work)
        try:
            fts = self._get_fts_searcher()
            await asyncio.to_thread(lambda: fts._db.execute("DELETE FROM memories_fts"))
            report["fts_cleared"] = True
        except Exception as e:
            logger.error(
                f"Failed to clear memory FTS5 index: {e} "
                "(this is an FTS-local issue, not whole-DB corruption)"
            )
            report["fts_cleared"] = False
            report["fts_error"] = str(e)

        scope = f"project {project_id}" if project_id else "all projects"
        logger.info(f"Indices cleared for {scope}: {report}")
        return report

    async def rebuild_indices(self, project_id: str | None = None) -> dict[str, Any]:
        """Rebuild all secondary indices from the SQLite source of truth.

        This is the slow path — meant to run as a background task after
        ``clear_indices``.  Rebuilds embeddings, crossrefs, KG, and FTS5.
        """
        report: dict[str, Any] = {}
        scope = f"project {project_id}" if project_id else "all projects"
        logger.info(f"Starting index rebuild for {scope}")

        # 1. Rebuild embeddings
        report["embeddings"] = await self.reindex_embeddings(project_id=project_id)

        # 2. Fetch memories for crossref + KG rebuild
        if project_id:
            all_memories = await self._fetch_all_project_memories(project_id)
        else:
            all_memories = await asyncio.to_thread(
                self.list_memories, None, None, MAX_REINDEX_LIMIT
            )
        total = len(all_memories)

        # 3. Rebuild crossrefs (concurrent, semaphore-limited)
        crossref_sem = asyncio.Semaphore(10)
        crossref_done = 0
        crossref_done_lock = asyncio.Lock()

        async def _rebuild_crossref(mem: Memory) -> int:
            nonlocal crossref_done
            async with crossref_sem:
                try:
                    result = await self.rebuild_crossrefs_for_memory(mem)
                except (CrossrefRebuildError, ValueError) as e:
                    logger.warning(f"Crossref failed for {mem.id}: {e}")
                    result = 0
                async with crossref_done_lock:
                    crossref_done += 1
                    if crossref_done % 50 == 0 or crossref_done == total:
                        logger.info(f"Crossref progress: {crossref_done}/{total}")
                return result

        crossref_results = await asyncio.gather(*[_rebuild_crossref(m) for m in all_memories])
        crossrefs_created = sum(crossref_results)
        logger.info(f"Crossref rebuild complete: {crossrefs_created} links from {total} memories")
        report["crossrefs"] = {
            "memories_processed": total,
            "crossrefs_created": crossrefs_created,
        }

        # 4. Rebuild knowledge graph (concurrent, semaphore-limited)
        if self._kg_service:
            kg_service = self._kg_service
            kg_sem = asyncio.Semaphore(5)
            kg_done = 0
            kg_done_lock = asyncio.Lock()

            async def _rebuild_kg(mem: Memory) -> bool:
                nonlocal kg_done
                async with kg_sem:
                    try:
                        await kg_service.add_to_graph(
                            mem.content,
                            memory_id=mem.id,
                            project_id=mem.project_id,
                        )
                        success = True
                    except Exception as e:
                        logger.warning(f"KG extraction failed for {mem.id}: {e}")
                        success = False
                    async with kg_done_lock:
                        kg_done += 1
                        if kg_done % 50 == 0 or kg_done == total:
                            logger.info(f"KG extraction progress: {kg_done}/{total}")
                    return success

            kg_results = await asyncio.gather(*[_rebuild_kg(m) for m in all_memories])
            extracted = sum(1 for r in kg_results if r)
            errors = sum(1 for r in kg_results if not r)
            logger.info(
                f"KG rebuild complete: {extracted} extracted, {errors} errors from {total} memories"
            )
            report["graph_rebuilt"] = {"extracted": extracted, "errors": errors}

        # 5. Reindex FTS5
        try:
            report["fts5"] = await asyncio.to_thread(self._get_fts_searcher().reindex)
        except Exception as e:
            logger.error(
                f"Failed to rebuild memory FTS5 index: {e} "
                "(FTS-local failure, other indices were rebuilt successfully)"
            )
            report["fts5"] = {"success": False, "error": str(e)}

        logger.info(f"Index rebuild complete for {scope}: {report}")
        return report

    async def invalidate_all(self, project_id: str | None = None) -> dict[str, Any]:
        """Clear all secondary indices for a project (or globally).

        Returns the clear report immediately.  The caller should schedule
        ``rebuild_indices`` as a background task if a rebuild is desired.
        """
        return await self.clear_indices(project_id=project_id)

    async def _fetch_all_project_memories(self, project_id: str) -> list[Memory]:
        """Fetch all memories for a project using pagination."""
        all_memories: list[Memory] = []
        offset = 0
        batch_size = 500
        while True:
            batch = await asyncio.to_thread(
                self.storage.list_memories,
                project_id,
                None,
                batch_size,
                offset,
            )
            if not batch:
                break
            all_memories.extend(batch)
            if len(batch) < batch_size:
                break
            offset += batch_size
        return all_memories

    # =========================================================================
    # Cross-references (using VectorStore for similarity search)
    # =========================================================================

    async def rebuild_crossrefs_for_memory(
        self,
        memory: Memory,
        threshold: float | None = None,
        max_links: int | None = None,
    ) -> int:
        """Public wrapper for cross-reference creation."""
        try:
            return await self._create_crossrefs(memory, threshold, max_links)
        except Exception as exc:
            raise CrossrefRebuildError(str(exc)) from exc

    async def _create_crossrefs(
        self,
        memory: Memory,
        threshold: float | None = None,
        max_links: int | None = None,
    ) -> int:
        """
        Find and link similar memories using VectorStore search.

        Args:
            memory: The memory to find links for
            threshold: Minimum similarity to create link (default from config)
            max_links: Maximum links to create (default from config)

        Returns:
            Number of cross-references created
        """
        if not self._vector_store or not self._embed_fn:
            return 0

        threshold = threshold or getattr(self.config, "crossref_threshold", None) or 0.7
        max_links = max_links or getattr(self.config, "crossref_max_links", None) or 5

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
                self.storage.create_crossref(memory.id, other_id, score)
                count += 1
            except Exception as e:
                logger.debug(f"Crossref creation failed: {e}")

        return count

    async def get_related(
        self,
        memory_id: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
        min_similarity: float = 0.0,
        project_id: str | None = None,
    ) -> list[Memory]:
        """
        Get memories linked to this one via cross-references.

        Args:
            memory_id: The memory ID to find related memories for
            limit: Maximum number of results
            min_similarity: Minimum similarity threshold
            project_id: If provided, only return related memories belonging
                to this project or global memories

        Returns:
            List of related Memory objects, sorted by similarity
        """
        crossrefs = self.storage.get_crossrefs(
            memory_id, limit=limit, min_similarity=min_similarity
        )
        memories: list[Memory] = []
        for ref in crossrefs:
            other_id = ref.target_id if ref.source_id == memory_id else ref.source_id
            try:
                mem = self.storage.get_memory(other_id, project_id=project_id)
            except ValueError:
                continue
            memories.append(mem)
        return memories

    # =========================================================================
    # Neo4j knowledge graph (delegated to KnowledgeGraphService)
    # =========================================================================

    async def get_entity_graph(self, limit: int = DEFAULT_GRAPH_LIMIT) -> dict[str, Any] | None:
        """Get the Neo4j entity graph for visualization."""
        if self._kg_service:
            return await self._kg_service.get_entity_graph(limit=limit)
        if self._neo4j_client:
            try:
                return await self._neo4j_client.get_entity_graph(limit=limit)
            except Exception as e:
                logger.warning(f"Neo4j query failed: {e}")
                return None
        return None

    async def get_entity_neighbors(self, name: str) -> dict[str, Any] | None:
        """Get neighbors for a single Neo4j entity."""
        if self._kg_service:
            return await self._kg_service.get_entity_neighbors(name)
        if self._neo4j_client:
            try:
                return await self._neo4j_client.get_entity_neighbors(name)
            except Exception as e:
                logger.warning(f"Neo4j query failed: {e}")
                return None
        return None

    def export_markdown(
        self,
        project_id: str | None = None,
        include_metadata: bool = True,
        include_stats: bool = True,
    ) -> str:
        """Export memories as a formatted markdown document."""
        return _export_markdown(self.storage, project_id, include_metadata, include_stats)

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
    from gobby.memory.services.dedup import DedupService
    from gobby.memory.vectorstore import VectorStore

logger = logging.getLogger(__name__)

# Boost factor applied to user-sourced memories in search results
_USER_SOURCE_BOOST = 1.2

DEFAULT_LIST_LIMIT = 50
DEFAULT_SEARCH_LIMIT = 10
DEFAULT_GRAPH_LIMIT = 500
MAX_REINDEX_LIMIT = 100_000


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
    ):
        self.db = db
        self.config = config
        self._llm_service = llm_service
        self._vector_store = vector_store
        self._embed_fn = embed_fn

        # Primary storage layer — always SQLite via LocalMemoryManager
        self.storage = LocalMemoryManager(db)

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
        if config.neo4j_url:
            self._neo4j_client: Neo4jClient | None = Neo4jClient(
                url=config.neo4j_url,
                auth=config.neo4j_auth,
                database=config.neo4j_database,
            )
        else:
            self._neo4j_client = None

        # DedupService + KnowledgeGraphService: initialized when LLM + VectorStore + embed_fn available
        self._dedup_service: DedupService | None = None
        self._kg_service: KnowledgeGraphService | None = None
        if llm_service and vector_store and embed_fn:
            try:
                from gobby.memory.services.dedup import DedupService as _DedupService
                from gobby.prompts.loader import PromptLoader

                provider = llm_service.get_default_provider()
                prompt_loader = PromptLoader(db=self.db)
                self._dedup_service = _DedupService(
                    llm_provider=provider,
                    vector_store=vector_store,
                    storage=self.storage,
                    embed_fn=embed_fn,
                    prompt_loader=prompt_loader,
                )
                logger.debug("DedupService initialized")

                # KnowledgeGraphService: requires Neo4j client
                if self._neo4j_client:
                    self._kg_service = KnowledgeGraphService(
                        neo4j_client=self._neo4j_client,
                        llm_provider=provider,
                        embed_fn=embed_fn,
                        prompt_loader=prompt_loader,
                    )
                    logger.debug("KnowledgeGraphService initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize DedupService: {e}")

    @property
    def kg_service(self) -> KnowledgeGraphService | None:
        """Get the knowledge graph service."""
        return self._kg_service

    @property
    def embed_fn(self) -> Callable[..., Any] | None:
        """Get the embedding function."""
        return self._embed_fn

    @property
    def llm_service(self) -> LLMService | None:
        """Get the LLM service for image description."""
        return self._ingestion_service.llm_service

    @llm_service.setter
    def llm_service(self, service: LLMService | None) -> None:
        """Set the LLM service for image description."""
        self._llm_service = service
        self._ingestion_service.llm_service = service

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
            source_type=cast(Literal["user", "session", "inferred"] | None, record.source_type),
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
        try:
            embedding = await self._embed_fn(content)
            await self._vector_store.upsert(memory_id, embedding, payload or {})
        except Exception as e:
            logger.warning(f"VectorStore upsert failed for {memory_id}: {e}")

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

    def _fire_background_graph(self, content: str, memory_id: str | None = None) -> None:
        """Fire a background knowledge graph task (non-blocking).

        Extracts entities and relationships from content and merges
        them into the Neo4j knowledge graph. When memory_id is provided,
        creates MENTIONED_IN links from entities to the source memory.
        """

        async def _run_graph() -> None:
            try:
                assert self._kg_service is not None  # noqa: S101
                await self._kg_service.add_to_graph(content, memory_id=memory_id)
            except Exception as e:
                logger.warning(f"Background graph extraction failed: {e}")

        task = asyncio.create_task(_run_graph(), name="memory-graph")

        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # =========================================================================
    # CRUD operations
    # =========================================================================

    async def create_memory(
        self,
        content: str,
        memory_type: str = "fact",
        project_id: str | None = None,
        source_type: str = "user",
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
            payload={
                "content": content,
                "memory_type": memory_type,
                "project_id": project_id,
            },
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

        # Fire-and-forget: background knowledge graph task
        if self._kg_service:
            self._fire_background_graph(content, memory_id=memory.id)

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
            payload={"content": memory.content, "project_id": project_id},
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
            payload={"content": memory.content, "project_id": project_id},
        )
        return memory

    async def _search_graph_for_memories(
        self,
        query_embedding: list[float],
        limit: int = 10,
        min_score: float = 0.5,
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
    def _rrf_merge(
        qdrant_ranked: list[str],
        graph_ranked: list[str],
        k: int = 60,
    ) -> list[str]:
        """Merge two ranked lists using Reciprocal Rank Fusion.

        RRF score: score(d) = Σ 1/(k + rank_i) across all sources.
        Memories appearing in both lists get scores from both, naturally
        ranking higher. k=60 is the standard constant.

        Args:
            qdrant_ranked: Memory IDs ranked by Qdrant cosine similarity
            graph_ranked: Memory IDs ranked by graph search
            k: RRF constant (higher = more uniform weighting)

        Returns:
            Merged list of memory IDs sorted by RRF score (descending)
        """
        scores: dict[str, float] = {}

        for rank, mid in enumerate(qdrant_ranked):
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank + 1)

        for rank, mid in enumerate(graph_ranked):
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank + 1)

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
            query_embedding = await self._embed_fn(query)

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
                )

                qdrant_result, graph_result = await asyncio.gather(
                    qdrant_coro, graph_coro, return_exceptions=True
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

                # Build Qdrant ranked list (by score)
                qdrant_ranked = [mid for mid, _ in qdrant_results]

                # Merge via RRF
                if graph_ranked:
                    merged_ids = self._rrf_merge(qdrant_ranked, graph_ranked, k=rrf_k)
                else:
                    merged_ids = qdrant_ranked

                # Resolve memories and apply filters
                scored: list[tuple[Memory, float]] = []
                for rank, memory_id in enumerate(merged_ids):
                    try:
                        mem = self.storage.get_memory(memory_id)
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

                    # Use RRF rank as primary ordering; apply user source boost to break ties
                    base_score = 1.0 / (rank + 1)
                    if mem.source_type == "user":
                        base_score *= _USER_SOURCE_BOOST
                    scored.append((mem, base_score))

                scored.sort(key=lambda x: x[1], reverse=True)
                memories = [m for m, _ in scored[:limit]]
            else:
                # Qdrant-only path (no graph search)
                results = await self._vector_store.search(
                    query_embedding,
                    limit=limit * 2,
                    filters=filters or None,
                )

                scored = []
                for memory_id, score in results:
                    try:
                        mem = self.storage.get_memory(memory_id)
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

                    boosted = score * _USER_SOURCE_BOOST if mem.source_type == "user" else score
                    scored.append((mem, boosted))

                scored.sort(key=lambda x: x[1], reverse=True)
                memories = [m for m, _ in scored[:limit]]
        else:
            # No query or no VectorStore: list from SQLite
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
                logger.warning(f"Failed to update access stats for {memory.id}: {e}")

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory from SQLite and VectorStore."""
        result = self.storage.delete_memory(memory_id)
        if result and self._vector_store:
            try:
                await self._vector_store.delete(memory_id)
            except Exception as e:
                logger.warning(f"VectorStore delete failed for {memory_id}: {e}")
        return result

    async def adelete_memory(self, memory_id: str) -> bool:
        """Delete a memory (async version via backend)."""
        result = await self._backend.delete(memory_id)
        if result and self._vector_store:
            try:
                await self._vector_store.delete(memory_id)
            except Exception as e:
                logger.warning(f"VectorStore delete failed for {memory_id}: {e}")
        return result

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

    def get_memory(self, memory_id: str) -> Memory | None:
        """Get a specific memory by ID."""
        try:
            return self.storage.get_memory(memory_id)
        except ValueError:
            return None

    async def aget_memory(self, memory_id: str) -> Memory | None:
        """Get a specific memory by ID (async)."""
        record = await self._backend.get(memory_id)
        if record:
            return self._record_to_memory(record)
        return None

    def find_by_prefix(self, prefix: str, limit: int = 5) -> list[Memory]:
        """Find memories whose IDs start with the given prefix."""
        rows = self.db.fetchall(
            "SELECT * FROM memories WHERE id LIKE ? LIMIT ?",
            (f"{prefix}%", limit),
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
                payload={"content": content, "project_id": result.project_id},
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
                payload={"content": content, "project_id": memory.project_id},
            )
        return memory

    def get_stats(self, project_id: str | None = None) -> dict[str, Any]:
        """Get statistics about stored memories."""
        return _get_stats(self.storage, self.db, project_id, vector_store=self._vector_store)

    # =========================================================================
    # Reindexing
    # =========================================================================

    async def reindex_embeddings(self) -> dict[str, Any]:
        """Regenerate embeddings for all stored memories."""
        if not self._vector_store or not self._embed_fn:
            return {"success": False, "error": "Vector store or embedding function not configured"}

        memories = self.list_memories(limit=MAX_REINDEX_LIMIT)
        total = len(memories)
        generated = 0

        for mem in memories:
            try:
                await self._embed_and_upsert(mem.id, mem.content)
                generated += 1
            except Exception as e:
                logger.warning(f"Failed to reindex memory {mem.id}: {e}")

        return {"success": True, "total_memories": total, "embeddings_generated": generated}

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
        return await self._create_crossrefs(memory, threshold, max_links)

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
    ) -> list[Memory]:
        """
        Get memories linked to this one via cross-references.

        Args:
            memory_id: The memory ID to find related memories for
            limit: Maximum number of results
            min_similarity: Minimum similarity threshold

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
                mem = self.storage.get_memory(other_id)
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

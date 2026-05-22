from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal, cast

from gobby.config.persistence import MemoryConfig
from gobby.memory.backends.storage_adapter import StorageAdapter
from gobby.memory.components.ingestion import IngestionService
from gobby.memory.context import build_memory_context
from gobby.memory.falkor_client import FalkorClient
from gobby.memory.protocol import MemoryBackendProtocol, MemoryRecord
from gobby.memory.services.crossref import CrossrefRebuildError, CrossrefService
from gobby.memory.services.indexing import IndexingService
from gobby.memory.services.knowledge_graph import (
    KnowledgeGraphResult,
    KnowledgeGraphService,
    KnowledgeGraphStatus,
)
from gobby.memory.services.maintenance import (
    export_markdown as _export_markdown,
)
from gobby.memory.services.maintenance import (
    get_stats as _get_stats,
)
from gobby.memory.services.search import SearchService
from gobby.memory.vectorstore import (
    VECTORSTORE_WARNING_INTERVAL_SECONDS,
    is_recoverable_vector_store_error,
    log_rate_limited_warning,
)
from gobby.storage.database import DatabaseProtocol
from gobby.storage.memories import LocalMemoryManager, Memory

if TYPE_CHECKING:
    from gobby.llm.service import LLMService
    from gobby.memory.services.dedup import DedupService
    from gobby.memory.vectorstore import VectorStore

__all__ = ["CrossrefRebuildError", "MemoryManager"]

logger = logging.getLogger(__name__)

DEFAULT_LIST_LIMIT = 50
DEFAULT_SEARCH_LIMIT = 10
DEFAULT_GRAPH_LIMIT = 500
MAX_REINDEX_LIMIT = 100_000


class MemoryManager:
    """High-level facade for memory operations.

    Wires storage (LocalMemoryManager + async backend), search (SearchService),
    indexing/lifecycle (IndexingService), cross-references (CrossrefService),
    image ingestion (IngestionService), dedup (DedupService), and the
    FalkorDB knowledge graph (KnowledgeGraphService).

    Public API is intentionally broad and stable; this class delegates the
    heavy lifting to the per-concern services above.
    """

    def __init__(
        self,
        db: DatabaseProtocol,
        config: MemoryConfig,
        llm_service: LLMService | None = None,
        vector_store: VectorStore | None = None,
        embed_fn: Callable[..., Any] | None = None,
        *,
        falkordb_host: str | None = None,
        falkordb_port: int = 16379,
        falkordb_password: str | None = None,
        falkordb_graph_name: str = "gobby_kg",
        falkordb_graph_search: bool = True,
        falkordb_graph_min_score: float = 0.5,
        rrf_k: int = 60,
        falkordb_rrf_k: int = 60,
        embedding_dim: int = 768,
        collection_prefix: str = "code_symbols_",
        run_db: Callable[..., Awaitable[Any]] | None = None,
    ):
        self.db = db
        self.config = config
        self._run_db = run_db
        self._llm_service = llm_service
        self._vector_store = vector_store
        self._embed_fn = embed_fn
        self._falkordb_graph_search = falkordb_graph_search
        self._falkordb_graph_min_score = falkordb_graph_min_score
        self._falkordb_rrf_k = falkordb_rrf_k

        self.storage = LocalMemoryManager(db)
        self._backend: MemoryBackendProtocol = StorageAdapter(self.storage, run_db=run_db)
        self._ingestion_service = IngestionService(
            storage=self.storage,
            backend=self._backend,
            llm_service=llm_service,
        )
        self._background_tasks: set[asyncio.Task[Any]] = set()

        if falkordb_host:
            self._falkor_client: FalkorClient | None = FalkorClient(
                host=falkordb_host,
                port=falkordb_port,
                password=falkordb_password,
                graph_name=falkordb_graph_name,
            )
        else:
            self._falkor_client = None

        self._embeddings_available: bool | None = None
        self._last_vector_store_warning_at = -VECTORSTORE_WARNING_INTERVAL_SECONDS

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

        if llm_service and self._falkor_client:
            try:
                from gobby.prompts.loader import PromptLoader

                provider, model, _ = llm_service.get_provider_for_feature(config.kg)
                prompt_loader = PromptLoader(db=self.db)
                self._kg_service = KnowledgeGraphService(
                    falkor_client=self._falkor_client,
                    llm_provider=provider,
                    embed_fn=embed_fn,
                    prompt_loader=prompt_loader,
                    vector_store=vector_store,
                    code_link_min_score=config.code_link_min_score,
                    code_symbol_collection_prefix=collection_prefix,
                    embedding_dim=embedding_dim,
                    model=model,
                )
                logger.debug("KnowledgeGraphService initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize KnowledgeGraphService: {e}")

        self._search_service = SearchService(
            storage=self.storage,
            vector_store=vector_store,
            embed_fn=embed_fn,
            kg_service=self._kg_service,
            keyword_search=self._keyword_search,
            config=config,
            falkordb_graph_search=falkordb_graph_search,
            falkordb_graph_min_score=falkordb_graph_min_score,
            rrf_k=rrf_k,
            falkordb_rrf_k=falkordb_rrf_k,
            vector_store_failure_logger=self._log_vector_store_failure,
            run_db=run_db,
        )
        self._crossref_service = CrossrefService(
            storage=self.storage,
            vector_store=vector_store,
            embed_fn=embed_fn,
            config=config,
            run_db=run_db,
        )
        self._indexing_service = IndexingService(
            storage=self.storage,
            vector_store=vector_store,
            embed_fn=embed_fn,
            kg_service=self._kg_service,
            crossref_service=self._crossref_service,
            kg_rebuilder=self.rebuild_knowledge_graph,
            run_db=run_db,
        )

    async def run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run memory storage work on the daemon DB executor when available."""
        if self._run_db is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db(func, *args, **kwargs)

    async def close(self) -> None:
        """Close underlying graph clients."""
        if self._falkor_client:
            try:
                await self._falkor_client.close()
            except Exception as e:
                logger.warning(f"Failed to close FalkorDB client: {e}")
            self.clear_graph_clients()

    def clear_graph_clients(self) -> None:
        """Disable graph features by clearing FalkorDB client and KG service references."""
        self._falkor_client = None
        self._kg_service = None
        self._search_service._kg_service = None
        self._indexing_service._kg_service = None

    @property
    def kg_service(self) -> KnowledgeGraphService | None:
        return self._kg_service

    @property
    def vector_store(self) -> Any | None:
        return self._vector_store

    @property
    def embed_fn(self) -> Callable[..., Any] | None:
        return self._embed_fn

    @property
    def llm_service(self) -> LLMService | None:
        return self._llm_service

    @llm_service.setter
    def llm_service(self, service: LLMService | None) -> None:
        self._llm_service = service
        self._ingestion_service.llm_service = service

    @property
    def falkor_client(self) -> FalkorClient | None:
        """Shared FalkorDB client for graph-backed subsystems, when configured."""
        return self._falkor_client

    def _keyword_search(
        self,
        query: str,
        limit: int,
        project_id: str | None = None,
    ) -> list[tuple[str, float]]:
        """Run dialect-aware keyword search and return ranked memory IDs."""
        from gobby.search.keyword import pick_search_backend

        backend = pick_search_backend(self.db, "memories")
        hits = backend.search(query, limit, filters={"project_id": project_id})
        return [(hit.id, hit.score) for hit in hits]

    @staticmethod
    def _record_to_memory(record: MemoryRecord) -> Memory:
        """Convert a MemoryRecord from the backend to a Memory."""
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
            media=None,
        )

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
            return
        try:
            embedding = await self._embed_fn(content)
            self._embeddings_available = True
        except Exception as e:
            if self._embeddings_available is None:
                logger.warning(
                    f"Embedding failed for {memory_id}: {e}; "
                    "suppressing further embedding warnings until provider recovers"
                )
                self._embeddings_available = False
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

    def _log_vector_store_failure(self, message: str, error: BaseException) -> None:
        """Rate-limit noisy VectorStore availability warnings."""
        self._last_vector_store_warning_at = log_rate_limited_warning(
            logger,
            self._last_vector_store_warning_at,
            message,
            error,
        )

    def _fire_background_dedup(
        self,
        content: str,
        project_id: str | None,
        memory_type: str,
        tags: list[str] | None,
        source_type: str,
        source_session_id: str | None,
    ) -> None:
        """Fire a background dedup task (non-blocking)."""

        async def _run_dedup() -> None:
            try:
                dedup_service = self._dedup_service
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

    def _enqueue_for_graph(
        self,
        memory_id: str,
        project_id: str | None = None,
    ) -> None:
        """Queue memory for background KG processing."""
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
        """Store a new memory in storage and VectorStore."""
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

        await self._embed_and_upsert(
            memory.id,
            content,
            payload={"project_id": project_id},
        )

        if getattr(self.config, "auto_crossref", False):
            try:
                await self._crossref_service.create(memory)
            except Exception as e:
                logger.warning(f"Auto-crossref failed for {memory.id}: {e}")

        if self._dedup_service:
            self._fire_background_dedup(
                content=content,
                project_id=project_id,
                memory_type=memory_type,
                tags=tags,
                source_type=source_type,
                source_session_id=source_session_id,
            )

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

    @staticmethod
    def _rrf_scores(*ranked_lists: list[str], k: int = 60) -> dict[str, float]:
        """Compute Reciprocal Rank Fusion scores for one or more ranked lists."""
        return SearchService.rrf_scores(*ranked_lists, k=k)

    @staticmethod
    def _rrf_merge(*ranked_lists: list[str], k: int = 60) -> list[str]:
        """Merge ranked lists using Reciprocal Rank Fusion."""
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
    ) -> list[Memory]:
        """Retrieve memories via VectorStore + optional FalkorDB graph search."""
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
        )

    async def search_memories_as_context(
        self,
        project_id: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> str:
        """Retrieve memories and format them as context for LLM prompts."""
        memories = await self.search_memories(project_id=project_id, limit=limit)
        return build_memory_context(memories)

    def _update_access_stats(self, memories: list[Memory]) -> None:
        """Update access count and time for memories (debounced)."""
        self._search_service.update_access_stats(memories)

    async def _search_graph_for_memories(
        self,
        query_embedding: list[float],
        limit: int = 10,
        min_score: float = 0.5,
        project_id: str | None = None,
    ) -> list[str]:
        """Search FalkorDB graph for memory IDs via entity vector similarity."""
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
        """Run keyword search and return ranked memory IDs for RRF merge."""
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
        """Keyword search fallback when vector search returns nothing."""
        return await self._search_service._keyword_fallback(
            query, limit, project_id, memory_type, tags_all, tags_any, tags_none
        )

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory from storage, VectorStore, and FalkorDB."""
        existing_memory = self.get_memory(memory_id)
        result = self.storage.delete_memory(memory_id)
        if result and self._vector_store:
            try:
                await self._vector_store.delete(memory_id)
            except Exception as e:
                logger.warning(f"VectorStore delete failed for {memory_id}: {e}")
        if result and self._kg_service:
            try:
                await self._kg_service.remove_memory_from_graph(
                    memory_id,
                    project_id=existing_memory.project_id if existing_memory else None,
                )
            except Exception as e:
                logger.warning(f"Graph delete failed for {memory_id}: {e}")
        return result

    async def adelete_memory(self, memory_id: str) -> bool:
        """Delete a memory (async version via backend)."""
        existing_memory = self.get_memory(memory_id)
        result = await self._backend.delete(memory_id)
        if result and self._vector_store:
            try:
                await self._vector_store.delete(memory_id)
            except Exception as e:
                logger.warning(f"VectorStore delete failed for {memory_id}: {e}")
        if result and self._kg_service:
            try:
                await self._kg_service.remove_memory_from_graph(
                    memory_id,
                    project_id=existing_memory.project_id if existing_memory else None,
                )
            except Exception as e:
                logger.warning(f"Graph delete failed for {memory_id}: {e}")
        return result

    async def reconcile_stores(self, dry_run: bool = False) -> dict[str, Any]:
        """Reconcile Qdrant and FalkorDB with memory storage."""
        return await self._indexing_service.reconcile_stores(dry_run=dry_run)

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
        """List memories with optional filtering."""
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
        """Get a specific memory by ID, optionally scoped to a project."""
        try:
            return self.storage.get_memory(memory_id, project_id=project_id)
        except ValueError:
            return None

    async def aget_memory(self, memory_id: str, project_id: str | None = None) -> Memory | None:
        """Get a specific memory by ID (async)."""
        # Backend lookup is ID-only; project scoping is enforced after retrieval.
        record = await self._backend.get(memory_id)
        if record:
            if project_id and record.project_id and record.project_id != project_id:
                return None
            return self._record_to_memory(record)
        return None

    def find_by_prefix(
        self, prefix: str, limit: int = 5, project_id: str | None = None
    ) -> list[Memory]:
        """Find memories whose IDs start with the given prefix."""
        bs = chr(92)
        pct = "%"
        und = "_"
        escaped = prefix.replace(bs, bs + bs).replace(pct, bs + pct).replace(und, bs + und)
        like_value = f"{escaped}%"
        escape_clause = " ESCAPE '" + bs + "'"
        if project_id:
            sql = (
                "SELECT * FROM memories WHERE id LIKE ?"
                + escape_clause
                + " AND (project_id = ? OR project_id IS NULL) LIMIT ?"
            )
            rows = self.db.fetchall(sql, (like_value, project_id, limit))
        else:
            sql = "SELECT * FROM memories WHERE id LIKE ?" + escape_clause + " LIMIT ?"
            rows = self.db.fetchall(sql, (like_value, limit))
        return [Memory.from_row(row) for row in rows]

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

    async def reindex_embeddings(self, project_id: str | None = None) -> dict[str, Any]:
        """Regenerate embeddings for stored memories."""
        return await self._indexing_service.reindex_embeddings(project_id=project_id)

    async def clear_indices(self, project_id: str | None = None) -> dict[str, Any]:
        """Fast wipe of all secondary indices for a project (or all projects)."""
        return await self._indexing_service.clear_indices(project_id=project_id)

    async def rebuild_indices(self, project_id: str | None = None) -> dict[str, Any]:
        """Rebuild all secondary indices from memory storage."""
        return await self._indexing_service.rebuild_indices(project_id=project_id)

    async def invalidate_all(self, project_id: str | None = None) -> dict[str, Any]:
        """Clear all secondary indices for a project (or globally)."""
        return await self._indexing_service.invalidate_all(project_id=project_id)

    async def _fetch_all_project_memories(self, project_id: str) -> list[Memory]:
        """Fetch all memories for a project using pagination."""
        return await self._indexing_service.fetch_all_project_memories(project_id)

    async def rebuild_crossrefs_for_memory(
        self,
        memory: Memory,
        threshold: float | None = None,
        max_links: int | None = None,
    ) -> int:
        """Public wrapper for cross-reference creation."""
        return await self._crossref_service.rebuild_for_memory(memory, threshold, max_links)

    async def _create_crossrefs(
        self,
        memory: Memory,
        threshold: float | None = None,
        max_links: int | None = None,
    ) -> int:
        """Find and link similar memories using VectorStore search."""
        return await self._crossref_service.create(memory, threshold, max_links)

    async def get_related(
        self,
        memory_id: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
        min_similarity: float = 0.0,
        project_id: str | None = None,
    ) -> list[Memory]:
        """Get memories linked to this one via cross-references."""
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
        """Clear the FalkorDB knowledge-graph projection and requeue affected memories."""
        if not self._kg_service:
            return {"success": False, "error": "KnowledgeGraphService not initialized"}
        cleared = await self._kg_service.clear_graph(project_id=project_id)
        pending = await self.run_db(self.storage.mark_pending_graphs, project_id)
        return {"success": True, "memories_marked_pending": pending, **cleared}

    async def rebuild_knowledge_graph(
        self,
        project_id: str | None = None,
        limit: int = MAX_REINDEX_LIMIT,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> dict[str, Any]:
        """Rebuild the FalkorDB knowledge-graph projection from stored memories."""
        if not self._kg_service:
            return {"success": False, "error": "KnowledgeGraphService not initialized"}

        if project_id:
            all_memories = (await self._fetch_all_project_memories(project_id))[:limit]
        else:
            all_memories = await self.run_db(self.list_memories, None, None, limit)

        status_counts = {status.value: 0 for status in KnowledgeGraphStatus}
        errors = 0
        processed = 0
        failed_memories: list[dict[str, Any]] = []

        kg_service = self._kg_service
        kg_worker_count = 5
        kg_done = 0
        kg_done_lock = asyncio.Lock()

        async def _emit_progress() -> None:
            if progress_callback is None:
                return
            progress = {
                "project_id": project_id,
                "memories_total": len(all_memories),
                "memories_completed": kg_done,
                "memories_marked_processed": processed,
                "status_counts": dict(status_counts),
                "errors": errors,
                "failed_memories": list(failed_memories),
            }
            maybe_awaitable = progress_callback(progress)
            if maybe_awaitable is not None:
                await maybe_awaitable

        async def _rebuild_kg(mem: Memory) -> KnowledgeGraphResult:
            nonlocal errors, kg_done, processed
            result = await kg_service.add_to_graph(
                mem.content,
                memory_id=mem.id,
                project_id=mem.project_id,
            )
            async with kg_done_lock:
                status_counts[result.status.value] += 1
                kg_done += 1
                if result.status in (
                    KnowledgeGraphStatus.SUCCESS,
                    KnowledgeGraphStatus.NOOP_NO_ENTITIES,
                ):
                    await self.run_db(self.mark_graph_processed, mem.id)
                    processed += 1
                else:
                    errors += 1
                    failed_memories.append(
                        {
                            "memory_id": mem.id,
                            "project_id": mem.project_id,
                            "status": result.status.value,
                            "errors": list(result.errors),
                        }
                    )
                await _emit_progress()
                if kg_done % 50 == 0 or kg_done == len(all_memories):
                    logger.info(f"KG extraction progress: {kg_done}/{len(all_memories)}")
            return result

        await _emit_progress()
        queue: asyncio.Queue[Memory | None] = asyncio.Queue()
        for mem in all_memories:
            queue.put_nowait(mem)
        for _ in range(kg_worker_count):
            queue.put_nowait(None)

        async def _worker() -> None:
            while True:
                mem = await queue.get()
                try:
                    if mem is None:
                        return
                    await _rebuild_kg(mem)
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(_worker()) for _ in range(kg_worker_count)]
        await queue.join()
        kg_results = await asyncio.gather(*workers)
        _ = kg_results

        logger.info(
            "KG rebuild complete for %s: %s",
            f"project {project_id}" if project_id else "all projects",
            status_counts,
        )
        return {
            "success": True,
            "memories_processed": len(all_memories),
            "memories_marked_processed": processed,
            "status_counts": status_counts,
            "memories_extracted": status_counts[KnowledgeGraphStatus.SUCCESS.value],
            "noop_no_entities": status_counts[KnowledgeGraphStatus.NOOP_NO_ENTITIES.value],
            "errors": errors,
            "failed_memories": failed_memories,
        }

    async def get_entity_graph(
        self,
        limit: int = DEFAULT_GRAPH_LIMIT,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get the FalkorDB entity graph for visualization."""
        if self._kg_service:
            return await self._kg_service.get_entity_graph(limit=limit, project_id=project_id)
        if self._falkor_client:
            try:
                return await self._falkor_client.get_entity_graph(
                    limit=limit,
                    project_id=project_id,
                )
            except Exception as e:
                logger.warning(f"FalkorDB query failed: {e}")
                return None
        return None

    async def get_entity_neighbors(
        self,
        entity_key: str,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get neighbors for a single FalkorDB entity."""
        if self._kg_service:
            return await self._kg_service.get_entity_neighbors(
                entity_key,
                project_id=project_id,
            )
        if self._falkor_client:
            try:
                return await self._falkor_client.get_entity_neighbors(
                    entity_key,
                    project_id=project_id,
                )
            except Exception as e:
                logger.warning(f"FalkorDB query failed: {e}")
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

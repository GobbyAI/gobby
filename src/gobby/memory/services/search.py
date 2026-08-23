"""Search service for memory retrieval (vector + graph + keyword + RRF)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from gobby.memory.recall_constants import RecallConstants, resolve_recall_constants
from gobby.memory.services._search_access import update_access_stats as update_memory_access_stats
from gobby.memory.services._search_backfill import collect_active_results
from gobby.memory.services._search_constants import DEFAULT_SEARCH_LIMIT
from gobby.memory.services._search_debug import emit_search_debug
from gobby.memory.services._search_graph import GraphScoredResult, search_graph_scored
from gobby.memory.services._search_keyword import KeywordSearch, keyword_fallback, keyword_ranked
from gobby.memory.services._search_models import SearchDebugHit, SearchDebugSnapshot, _Candidates
from gobby.memory.services._search_paths import search_qdrant_keyword, search_with_graph
from gobby.memory.services._search_results import build_results
from gobby.memory.services._search_rrf import rrf_merge, rrf_scores
from gobby.memory.vectorstore import memory_scope_filter
from gobby.storage.memories import (
    ALL_MEMORIES,
    LocalMemoryManager,
    Memory,
    MemoryScope,
    validate_memory_type,
)

if TYPE_CHECKING:
    from gobby.config.persistence import MemoryConfig
    from gobby.memory.services.knowledge_graph import KnowledgeGraphService
    from gobby.memory.vectorstore import VectorStore

__all__ = [
    "DEFAULT_SEARCH_LIMIT",
    "SearchDebugHit",
    "SearchDebugSnapshot",
    "SearchService",
]


class SearchService:
    """Encapsulates memory search across Qdrant, FalkorDB graph, and keyword search."""

    def __init__(
        self,
        *,
        storage: LocalMemoryManager,
        vector_store: VectorStore | None,
        embed_fn: Callable[..., Any] | None,
        kg_service: KnowledgeGraphService | None,
        keyword_search: KeywordSearch,
        config: MemoryConfig,
        falkordb_graph_search: bool,
        falkordb_graph_min_score: float,
        rrf_k: int,
        falkordb_rrf_k: int,
        vector_store_failure_logger: Callable[[str, BaseException], None],
        run_db: Callable[..., Awaitable[Any]] | None = None,
        search_debug_sink: Callable[[SearchDebugSnapshot], None] | None = None,
        recall_constants: RecallConstants | None = None,
    ) -> None:
        self._storage = storage
        self._vector_store = vector_store
        self._embed_fn = embed_fn
        self._kg_service = kg_service
        self._keyword_search = keyword_search
        self._config = config
        self._falkordb_graph_search = falkordb_graph_search
        self._falkordb_graph_min_score = falkordb_graph_min_score
        self._rrf_k = rrf_k
        self._falkordb_rrf_k = falkordb_rrf_k
        self._log_vector_store_failure = vector_store_failure_logger
        self._run_db = run_db
        self._search_debug_sink = search_debug_sink
        self._recall_constants = (
            recall_constants if recall_constants is not None else resolve_recall_constants(config)
        )

    async def _run_storage[T](self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        if self._run_db is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return cast(T, await self._run_db(func, *args, **kwargs))

    @property
    def kg_service(self) -> KnowledgeGraphService | None:
        return self._kg_service

    @kg_service.setter
    def kg_service(self, value: KnowledgeGraphService | None) -> None:
        self._kg_service = value

    def _require_vector_store(self) -> VectorStore:
        if self._vector_store is None:
            raise RuntimeError("Vector store is required for semantic memory search")
        return self._vector_store

    def _require_kg_service(self) -> KnowledgeGraphService:
        if self._kg_service is None:
            raise RuntimeError("Knowledge graph service is required for graph memory search")
        return self._kg_service

    @staticmethod
    def rrf_scores(*ranked_lists: list[str], k: int = 60) -> dict[str, float]:
        """Compute Reciprocal Rank Fusion scores for one or more ranked lists."""
        return rrf_scores(*ranked_lists, k=k)

    @staticmethod
    def rrf_merge(*ranked_lists: list[str], k: int = 60) -> list[str]:
        """Merge ranked lists using Reciprocal Rank Fusion."""
        return rrf_merge(*ranked_lists, k=k)

    async def search(
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
        """Retrieve memories via VectorStore + optional FalkorDB graph search.

        The vector and graph legs run on the embedding; the BM25 leg runs on
        ``query``. ``embed_text`` splits those representations: when supplied it
        is embedded verbatim (YAKE is skipped) while ``query`` stays the term
        bag the keyword leg needs. Omitting it — or passing ``None`` or an empty
        string — keeps the YAKE-derived embedding.
        """
        if memory_type is not None:
            memory_type = validate_memory_type(memory_type)
        scope = ALL_MEMORIES
        if project_id is not None:
            scope = (
                MemoryScope.project_visible(project_id)
                if include_global
                else MemoryScope.project_only(project_id)
            )
        if query and self._vector_store and self._embed_fn:
            if embed_text:
                embed_query = embed_text
            else:
                # An empty `embed_text` means the caller had nothing to supply, so
                # fall back rather than embed "" and hand the vector leg a
                # meaningless vector while discarding the query we do have.
                from gobby.search.keywords import extract_keywords

                embed_query = extract_keywords(query) or query
            query_embedding = await self._embed_fn(embed_query, is_query=True)
            half_life = self._recall_constants.half_life_days
            effective_min_score = min_score if min_score is not None else 0.0
            filters = memory_scope_filter(scope, memory_type)
            use_graph = self._kg_service is not None and self._falkordb_graph_search

            if use_graph:
                memories = await self._search_with_graph(
                    query=query,
                    embed_text=embed_text,
                    query_embedding=query_embedding,
                    limit=limit,
                    filters=filters,
                    project_id=project_id,
                    memory_type=memory_type,
                    tags_all=tags_all,
                    tags_any=tags_any,
                    tags_none=tags_none,
                    half_life=half_life,
                    effective_min_score=effective_min_score,
                    session_id=session_id,
                    recall_request_id=recall_request_id,
                    caller=caller,
                    include_global=include_global,
                )
            else:
                memories = await self._search_qdrant_keyword(
                    query=query,
                    embed_text=embed_text,
                    query_embedding=query_embedding,
                    limit=limit,
                    filters=filters,
                    project_id=project_id,
                    memory_type=memory_type,
                    tags_all=tags_all,
                    tags_any=tags_any,
                    tags_none=tags_none,
                    half_life=half_life,
                    effective_min_score=effective_min_score,
                    session_id=session_id,
                    recall_request_id=recall_request_id,
                    caller=caller,
                    include_global=include_global,
                )
        elif query:
            memories = await self._keyword_fallback(
                query,
                limit,
                project_id,
                memory_type,
                tags_all,
                tags_any,
                tags_none,
                embed_text=embed_text,
                session_id=session_id,
                recall_request_id=recall_request_id,
                caller=caller,
                include_global=include_global,
            )
        else:
            memories = await self._run_storage(
                self._storage.list_memories,
                scope=scope,
                memory_type=memory_type,
                limit=limit,
                tags_all=tags_all,
                tags_any=tags_any,
                tags_none=tags_none,
            )

        await self.update_access_stats(memories)
        return memories

    async def _search_with_graph(
        self,
        *,
        query: str,
        query_embedding: list[float],
        limit: int,
        filters: Any,
        project_id: str | None,
        memory_type: str | None,
        tags_all: list[str] | None,
        tags_any: list[str] | None,
        tags_none: list[str] | None,
        half_life: float,
        effective_min_score: float,
        embed_text: str | None = None,
        session_id: str | None = None,
        recall_request_id: str | None = None,
        caller: str = "memory.search",
        include_global: bool = True,
    ) -> list[Memory]:
        return await search_with_graph(
            self,
            query=query,
            embed_text=embed_text,
            query_embedding=query_embedding,
            limit=limit,
            filters=filters,
            project_id=project_id,
            memory_type=memory_type,
            tags_all=tags_all,
            tags_any=tags_any,
            tags_none=tags_none,
            half_life=half_life,
            effective_min_score=effective_min_score,
            graph_min_score=self._falkordb_graph_min_score,
            rrf_k=self._falkordb_rrf_k,
            session_id=session_id,
            recall_request_id=recall_request_id,
            caller=caller,
            include_global=include_global,
        )

    async def _search_qdrant_keyword(
        self,
        *,
        query: str,
        query_embedding: list[float],
        limit: int,
        filters: Any,
        project_id: str | None,
        memory_type: str | None,
        tags_all: list[str] | None,
        tags_any: list[str] | None,
        tags_none: list[str] | None,
        half_life: float,
        effective_min_score: float,
        embed_text: str | None = None,
        session_id: str | None = None,
        recall_request_id: str | None = None,
        caller: str = "memory.search",
        include_global: bool = True,
    ) -> list[Memory]:
        return await search_qdrant_keyword(
            self,
            query=query,
            embed_text=embed_text,
            query_embedding=query_embedding,
            limit=limit,
            filters=filters,
            project_id=project_id,
            memory_type=memory_type,
            tags_all=tags_all,
            tags_any=tags_any,
            tags_none=tags_none,
            half_life=half_life,
            effective_min_score=effective_min_score,
            rrf_k=self._rrf_k,
            session_id=session_id,
            recall_request_id=recall_request_id,
            caller=caller,
            include_global=include_global,
        )

    async def _collect_active_results(
        self,
        *,
        limit: int,
        collect: Callable[[int], Awaitable[_Candidates]],
        build: Callable[[_Candidates], list[Memory]],
    ) -> tuple[list[Memory], _Candidates]:
        async def _build_with_storage_runner(candidates: _Candidates) -> list[Memory]:
            return cast(list[Memory], await self._run_storage(build, candidates))

        return await collect_active_results(
            limit=limit,
            collect=collect,
            build=_build_with_storage_runner,
        )

    async def _emit_search_debug(
        self,
        *,
        query: str,
        project_id: str | None,
        session_id: str | None,
        recall_request_id: str | None,
        caller: str,
        merged_ids: list[str],
        returned: list[Memory],
        ranking_score_map: dict[str, float],
        rrf_applied: bool,
        embed_text: str | None = None,
        graph_score_map: dict[str, float] | None = None,
        graph_component_map: dict[str, dict[str, float | None]] | None = None,
    ) -> None:
        # The logged query is the text retrieval was actually driven by, because the
        # shadow judge renders it as the user's question. When the caller split the
        # two representations, the term bag is kept beside it — nothing recovers it
        # from an enriched embed text.
        logged_query = embed_text or query
        await self._run_storage(
            emit_search_debug,
            search_debug_sink=self._search_debug_sink,
            query=logged_query,
            bm25_query=query if logged_query != query else None,
            project_id=project_id,
            session_id=session_id,
            recall_request_id=recall_request_id,
            caller=caller,
            merged_ids=merged_ids,
            returned=returned,
            ranking_score_map=ranking_score_map,
            rrf_applied=rrf_applied,
            graph_score_map=graph_score_map,
            graph_component_map=graph_component_map,
            graph_synthetic_similarity_discount=(self._recall_constants.graph_synthetic_discount),
            constants_provenance=self._recall_constants.provenance,
        )

    def _build_results(
        self,
        *,
        merged_ids: list[str],
        ranking_score_map: dict[str, float],
        qdrant_score_map: dict[str, float],
        qdrant_set: set[str],
        keyword_set: set[str],
        graph_set: set[str] | None,
        graph_score_map: dict[str, float] | None = None,
        rrf_applied: bool,
        project_id: str | None,
        memory_type: str | None,
        tags_all: list[str] | None,
        tags_any: list[str] | None,
        tags_none: list[str] | None,
        half_life: float,
        effective_min_score: float,
        limit: int,
    ) -> list[Memory]:
        return build_results(
            storage=self._storage,
            merged_ids=merged_ids,
            ranking_score_map=ranking_score_map,
            qdrant_score_map=qdrant_score_map,
            qdrant_set=qdrant_set,
            keyword_set=keyword_set,
            graph_set=graph_set,
            graph_score_map=graph_score_map,
            rrf_applied=rrf_applied,
            project_id=project_id,
            memory_type=memory_type,
            tags_all=tags_all,
            tags_any=tags_any,
            tags_none=tags_none,
            half_life=half_life,
            effective_min_score=effective_min_score,
            limit=limit,
            graph_synthetic_discount=self._recall_constants.graph_synthetic_discount,
        )

    async def _search_graph_scored(
        self,
        query_embedding: list[float],
        limit: int = 10,
        min_score: float = 0.5,
        project_id: str | None = None,
        include_global: bool = True,
    ) -> GraphScoredResult:
        return await search_graph_scored(
            kg_service=self._require_kg_service(),
            query_embedding=query_embedding,
            related_expansion_timeout_seconds=(
                self._config.graph_related_expansion_timeout_seconds
            ),
            limit=limit,
            min_score=min_score,
            project_id=project_id,
            include_global=include_global,
        )

    async def _search_graph_for_memories(
        self,
        query_embedding: list[float],
        limit: int = 10,
        min_score: float = 0.5,
        project_id: str | None = None,
    ) -> list[str]:
        result = await self._search_graph_scored(
            query_embedding=query_embedding,
            limit=limit,
            min_score=min_score,
            project_id=project_id,
        )
        return [memory_id for memory_id, _ in result.scored]

    async def _keyword_ranked(
        self,
        query: str,
        limit: int,
        project_id: str | None,
        include_global: bool = True,
    ) -> list[str]:
        return await keyword_ranked(
            run_storage=self._run_storage,
            keyword_search=self._keyword_search,
            query=query,
            limit=limit,
            project_id=project_id,
            include_global=include_global,
        )

    async def _keyword_fallback(
        self,
        query: str,
        limit: int,
        project_id: str | None,
        memory_type: str | None,
        tags_all: list[str] | None,
        tags_any: list[str] | None,
        tags_none: list[str] | None,
        *,
        embed_text: str | None = None,
        session_id: str | None = None,
        recall_request_id: str | None = None,
        caller: str = "memory.search",
        include_global: bool = True,
    ) -> list[Memory]:
        memories = await keyword_fallback(
            run_storage=self._run_storage,
            storage=self._storage,
            keyword_search=self._keyword_search,
            query=query,
            limit=limit,
            project_id=project_id,
            memory_type=memory_type,
            tags_all=tags_all,
            tags_any=tags_any,
            tags_none=tags_none,
            include_global=include_global,
        )
        # One event per completed search — fallback searches must not be silent.
        await self._emit_search_debug(
            query=query,
            embed_text=embed_text,
            project_id=project_id,
            session_id=session_id,
            recall_request_id=recall_request_id,
            caller=caller,
            merged_ids=[mem.id for mem in memories],
            returned=memories,
            ranking_score_map={
                mem.id: mem.similarity for mem in memories if mem.similarity is not None
            },
            rrf_applied=False,
        )
        return memories

    async def update_access_stats(self, memories: list[Memory]) -> None:
        """Update access count and time for memories (debounced)."""
        await self._run_storage(
            update_memory_access_stats,
            storage=self._storage,
            config=self._config,
            memories=memories,
        )

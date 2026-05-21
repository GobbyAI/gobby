"""Search service for memory retrieval (vector + graph + keyword + RRF)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.memory.scoring import temporal_decay
from gobby.memory.vectorstore import is_recoverable_vector_store_error
from gobby.storage.memories import LocalMemoryManager, Memory

if TYPE_CHECKING:
    from gobby.config.persistence import MemoryConfig
    from gobby.memory.services.knowledge_graph import KnowledgeGraphService
    from gobby.memory.vectorstore import VectorStore

logger = logging.getLogger(__name__)

DEFAULT_SEARCH_LIMIT = 10
_USER_SOURCE_BOOST = 1.2


class SearchService:
    """Encapsulates memory search across Qdrant, Neo4j graph, and keyword search."""

    def __init__(
        self,
        *,
        storage: LocalMemoryManager,
        vector_store: VectorStore | None,
        embed_fn: Callable[..., Any] | None,
        kg_service: KnowledgeGraphService | None,
        keyword_search: Callable[[str, int, str | None], list[tuple[str, float]]],
        config: MemoryConfig,
        neo4j_graph_search: bool,
        neo4j_graph_min_score: float,
        rrf_k: int,
        neo4j_rrf_k: int,
        vector_store_failure_logger: Callable[[str, BaseException], None],
        run_db: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self._storage = storage
        self._vector_store = vector_store
        self._embed_fn = embed_fn
        self._kg_service = kg_service
        self._keyword_search = keyword_search
        self._config = config
        self._neo4j_graph_search = neo4j_graph_search
        self._neo4j_graph_min_score = neo4j_graph_min_score
        self._rrf_k = rrf_k
        self._neo4j_rrf_k = neo4j_rrf_k
        self._log_vector_store_failure = vector_store_failure_logger
        self._run_db = run_db

    async def _run_storage(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._run_db is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db(func, *args, **kwargs)

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
        scores: dict[str, float] = {}
        for ranked in ranked_lists:
            for rank, mid in enumerate(ranked):
                scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank + 1)
        return scores

    @staticmethod
    def rrf_merge(*ranked_lists: list[str], k: int = 60) -> list[str]:
        """Merge ranked lists using Reciprocal Rank Fusion."""
        scores = SearchService.rrf_scores(*ranked_lists, k=k)
        return sorted(scores, key=lambda mid: scores[mid], reverse=True)

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
    ) -> list[Memory]:
        """Retrieve memories via VectorStore + optional Neo4j graph search."""
        if query and self._vector_store and self._embed_fn:
            from gobby.search.keywords import extract_keywords

            embed_query = extract_keywords(query) or query
            query_embedding = await self._embed_fn(embed_query, is_query=True)
            half_life = getattr(self._config, "temporal_decay_half_life_days", 30.0)
            effective_min_score = min_score if min_score is not None else 0.0

            filters: dict[str, Any] = {}
            if project_id:
                filters["project_id"] = project_id

            use_graph = self._kg_service is not None and self._neo4j_graph_search

            if use_graph:
                memories = await self._search_with_graph(
                    query=query,
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
                )
            else:
                memories = await self._search_qdrant_keyword(
                    query=query,
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
                )
        else:
            if query:
                memories = await self._keyword_fallback(
                    query, limit, project_id, memory_type, tags_all, tags_any, tags_none
                )
            else:
                memories = self._storage.list_memories(
                    project_id=project_id,
                    memory_type=memory_type,
                    limit=limit,
                    tags_all=tags_all,
                    tags_any=tags_any,
                    tags_none=tags_none,
                )

        self.update_access_stats(memories)
        return memories

    async def _search_with_graph(
        self,
        *,
        query: str,
        query_embedding: list[float],
        limit: int,
        filters: dict[str, Any],
        project_id: str | None,
        memory_type: str | None,
        tags_all: list[str] | None,
        tags_any: list[str] | None,
        tags_none: list[str] | None,
        half_life: float,
        effective_min_score: float,
    ) -> list[Memory]:
        graph_min_score = self._neo4j_graph_min_score
        rrf_k = self._neo4j_rrf_k

        vector_store = self._require_vector_store()
        qdrant_coro = vector_store.search(
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
        keyword_coro = self._keyword_ranked(query, limit * 2, project_id)

        qdrant_result, graph_result, keyword_result = await asyncio.gather(
            qdrant_coro, graph_coro, keyword_coro, return_exceptions=True
        )

        if isinstance(qdrant_result, BaseException):
            if isinstance(qdrant_result, asyncio.CancelledError):
                raise qdrant_result
            if is_recoverable_vector_store_error(qdrant_result):
                self._log_vector_store_failure(
                    "Qdrant search unavailable; falling back to non-vector results",
                    qdrant_result,
                )
            else:
                logger.warning(f"Qdrant search failed: {qdrant_result}")
            qdrant_results: list[tuple[str, float]] = []
        else:
            qdrant_results = qdrant_result

        if isinstance(graph_result, BaseException):
            logger.warning(f"Graph search failed: {graph_result}")
            graph_ranked: list[str] = []
        else:
            graph_ranked = graph_result

        if isinstance(keyword_result, BaseException):
            logger.debug(f"Keyword search failed: {keyword_result}")
            keyword_ranked: list[str] = []
        else:
            keyword_ranked = keyword_result

        qdrant_score_map = dict(qdrant_results)
        qdrant_ranked = [mid for mid, _ in qdrant_results]

        rrf_lists = [rl for rl in (qdrant_ranked, graph_ranked, keyword_ranked) if rl]
        if len(rrf_lists) > 1:
            ranking_score_map = self.rrf_scores(*rrf_lists, k=rrf_k)
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
                ranking_score_map = self.rrf_scores(merged_ids, k=rrf_k)
        else:
            merged_ids = []
            rrf_applied = False
            ranking_score_map = {}

        return self._build_results(
            merged_ids=merged_ids,
            ranking_score_map=ranking_score_map,
            qdrant_score_map=qdrant_score_map,
            qdrant_set=set(qdrant_ranked),
            keyword_set=set(keyword_ranked),
            graph_set=set(graph_ranked),
            rrf_applied=rrf_applied,
            project_id=project_id,
            memory_type=memory_type,
            tags_all=tags_all,
            tags_any=tags_any,
            tags_none=tags_none,
            half_life=half_life,
            effective_min_score=effective_min_score,
            limit=limit,
        )

    async def _search_qdrant_keyword(
        self,
        *,
        query: str,
        query_embedding: list[float],
        limit: int,
        filters: dict[str, Any],
        project_id: str | None,
        memory_type: str | None,
        tags_all: list[str] | None,
        tags_any: list[str] | None,
        tags_none: list[str] | None,
        half_life: float,
        effective_min_score: float,
    ) -> list[Memory]:
        rrf_k = self._rrf_k

        vector_store = self._require_vector_store()
        qdrant_coro = vector_store.search(
            query_embedding,
            limit=limit * 2,
            filters=filters or None,
        )
        keyword_coro = self._keyword_ranked(query, limit * 2, project_id)

        qdrant_result, keyword_result = await asyncio.gather(
            qdrant_coro, keyword_coro, return_exceptions=True
        )

        if isinstance(qdrant_result, BaseException):
            if isinstance(qdrant_result, asyncio.CancelledError):
                raise qdrant_result
            if is_recoverable_vector_store_error(qdrant_result):
                self._log_vector_store_failure(
                    "Qdrant search unavailable; falling back to keyword results",
                    qdrant_result,
                )
            else:
                logger.warning(f"Qdrant search failed: {qdrant_result}")
            qdrant_results: list[tuple[str, float]] = []
        else:
            qdrant_results = qdrant_result

        if isinstance(keyword_result, BaseException):
            logger.debug(f"Keyword search failed: {keyword_result}")
            keyword_ranked: list[str] = []
        else:
            keyword_ranked = keyword_result

        qdrant_ranked = [mid for mid, _ in qdrant_results]
        qdrant_score_map = dict(qdrant_results)

        if qdrant_ranked and keyword_ranked:
            ranking_score_map = self.rrf_scores(qdrant_ranked, keyword_ranked, k=rrf_k)
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
        elif keyword_ranked:
            merged_ids = keyword_ranked
            ranking_score_map = self.rrf_scores(keyword_ranked, k=rrf_k)
            rrf_applied = False
        else:
            merged_ids = []
            ranking_score_map = {}
            rrf_applied = False

        return self._build_results(
            merged_ids=merged_ids,
            ranking_score_map=ranking_score_map,
            qdrant_score_map=qdrant_score_map,
            qdrant_set=set(qdrant_ranked),
            keyword_set=set(keyword_ranked),
            graph_set=None,
            rrf_applied=rrf_applied,
            project_id=project_id,
            memory_type=memory_type,
            tags_all=tags_all,
            tags_any=tags_any,
            tags_none=tags_none,
            half_life=half_life,
            effective_min_score=effective_min_score,
            limit=limit,
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
        scored: list[tuple[Memory, float, float | None]] = []
        memories_by_id = {
            mem.id: mem for mem in self._storage.get_memories(merged_ids, project_id=project_id)
        }

        for memory_id in merged_ids:
            mem = memories_by_id.get(memory_id)
            if mem is None:
                try:
                    mem = self._storage.get_memory(memory_id, project_id=project_id)
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

            if (
                effective_min_score > 0
                and similarity is not None
                and similarity < effective_min_score
            ):
                continue

            sources = []
            if memory_id in qdrant_set:
                sources.append("semantic")
            if graph_set and memory_id in graph_set:
                sources.append("graph")
            if memory_id in keyword_set:
                sources.append("keyword")

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

    async def _search_graph_for_memories(
        self,
        query_embedding: list[float],
        limit: int = 10,
        min_score: float = 0.5,
        project_id: str | None = None,
    ) -> list[str]:
        """Search Neo4j graph for memory IDs via entity vector similarity."""
        kg_service = self._require_kg_service()
        entity_results = await kg_service.search_entities_by_vector(
            query_embedding=query_embedding,
            limit=limit,
            min_score=min_score,
            project_id=project_id,
        )

        if not entity_results:
            return []

        direct_memory_ids: list[str] = []
        entity_keys: list[str] = []
        for result in entity_results:
            entity_keys.append(result["entity_key"])
            for mid in result.get("memory_ids", []):
                if mid not in direct_memory_ids:
                    direct_memory_ids.append(mid)

        traversed_memory_ids = await kg_service.find_related_memory_ids(
            entity_keys=entity_keys,
            max_hops=2,
            limit=limit,
            project_id=project_id,
        )

        seen = set(direct_memory_ids)
        merged = list(direct_memory_ids)
        for mid in traversed_memory_ids:
            if mid not in seen:
                seen.add(mid)
                merged.append(mid)

        return merged[:limit]

    async def _keyword_ranked(
        self,
        query: str,
        limit: int,
        project_id: str | None,
    ) -> list[str]:
        """Run keyword search and return ranked memory IDs for RRF merge."""
        results = await self._run_storage(self._keyword_search, query, limit, project_id)
        return [mem_id for mem_id, _ in results]

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
        keyword_results = await self._run_storage(
            self._keyword_search, query, limit * 2, project_id
        )
        if not keyword_results:
            return []

        memories: list[Memory] = []
        for mem_id, score in keyword_results:
            try:
                mem = await self._run_storage(self._storage.get_memory, mem_id)
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
            mem.search_via = "keyword"
            memories.append(mem)
            if len(memories) >= limit:
                break
        return memories

    def update_access_stats(self, memories: list[Memory]) -> None:
        """Update access count and time for memories (debounced)."""
        if not memories:
            return

        now = datetime.now(UTC)
        debounce_seconds = getattr(self._config, "access_debounce_seconds", 60)

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
                self._storage.update_access_stats(memory.id, now.isoformat())
            except Exception as e:
                if "malformed" in str(e):
                    logger.warning(
                        f"Failed to update access stats for {memory.id}: {e} "
                        "(likely FTS trigger issue — see memory FTS repair docs)"
                    )
                else:
                    logger.warning(f"Failed to update access stats for {memory.id}: {e}")

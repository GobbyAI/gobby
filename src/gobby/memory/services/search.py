"""Search service for memory retrieval (vector + graph + keyword + RRF)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.memory.scoring import temporal_decay
from gobby.memory.vectorstore import is_recoverable_vector_store_error, memory_project_scope_filter
from gobby.storage.memories import LocalMemoryManager, Memory

if TYPE_CHECKING:
    from gobby.config.persistence import MemoryConfig
    from gobby.memory.services.knowledge_graph import KnowledgeGraphService
    from gobby.memory.vectorstore import VectorStore

logger = logging.getLogger(__name__)

DEFAULT_SEARCH_LIMIT = 10
_USER_SOURCE_BOOST = 1.2
_GRAPH_EXPANSION_ENTITY_SEED_LIMIT = 8
_GRAPH_RELATED_EXPANSION_TIMEOUT_SECONDS = 2.0
# Recall expander (#17104): a memory the vector index missed, surfaced by an entity it
# mentions that matched the query, enters the similarity axis at its entity-match cosine
# discounted by this factor. The discount reflects the indirection (entity match, not a
# direct document match) and keeps graph-only hits conservative, so a strong real
# semantic hit always outranks them. Both values are cosines, so the larger always wins
# and semantic-first is preserved for every hit carrying a real similarity score.
_GRAPH_SYNTHETIC_SIM_DISCOUNT = 0.9
# A CO_OCCURS-traversed memory is one structural hop removed from a direct entity match,
# so its synthetic confidence is the seed entity cosine attenuated by this factor.
_GRAPH_TRAVERSAL_CONFIDENCE_FACTOR = 0.9

# Backfill against soft-delete top-k poisoning (#17162). SQL hydration is the source of
# truth for visibility: ranked candidates come from Qdrant/graph, which retain soft-hidden
# rows until purge, so hidden IDs eat result slots after ``_build_results`` drops them.
# We over-fetch ``limit * _OVERFETCH_FACTOR`` candidates, and if the active result count
# falls short while a source still has more to give, re-fetch with a geometrically larger
# candidate pool up to ``_MAX_BACKFILL_ROUNDS`` extra rounds. With no hidden rows the first
# round already fills ``limit`` and no backfill runs, so the common path pays nothing.
_OVERFETCH_FACTOR = 2
_BACKFILL_GROWTH = 2
_MAX_BACKFILL_ROUNDS = 3


@dataclass(frozen=True)
class SearchDebugHit:
    """Returned hit features captured for observational search telemetry."""

    memory_id: str
    rank: int
    search_via: str | None
    similarity: float | None
    raw_semantic_score: float | None
    temporal_decay_factor: float | None
    ranking_score: float | None
    ranking_mode: str | None
    graph_score: float | None


@dataclass(frozen=True)
class SearchDebugSnapshot:
    """Diagnostic ranking snapshot emitted after a search path materializes results."""

    merged_ids: list[str]
    returned_ids: list[str]
    ranking_score_map: dict[str, float]
    rrf_applied: bool
    query: str = ""
    project_id: str | None = None
    session_id: str | None = None
    recall_request_id: str | None = None
    caller: str = "memory.search"
    graph_score_map: dict[str, float] = field(default_factory=dict)
    returned_hits: list[SearchDebugHit] = field(default_factory=list)
    graph_synthetic_similarity_discount: float = _GRAPH_SYNTHETIC_SIM_DISCOUNT


@dataclass
class _Candidates:
    """One round of merged ranked candidates feeding ``_build_results``.

    ``exhausted`` is True when no contributing source returned a full page at the
    requested candidate count, meaning a larger fetch cannot surface new IDs and
    backfill should stop.
    """

    merged_ids: list[str]
    ranking_score_map: dict[str, float]
    qdrant_score_map: dict[str, float]
    qdrant_ranked: list[str]
    keyword_ranked: list[str]
    rrf_applied: bool
    graph_ranked: list[str] = field(default_factory=list)
    graph_score_map: dict[str, float] | None = None
    exhausted: bool = True


class SearchService:
    """Encapsulates memory search across Qdrant, FalkorDB graph, and keyword search."""

    def __init__(
        self,
        *,
        storage: LocalMemoryManager,
        vector_store: VectorStore | None,
        embed_fn: Callable[..., Any] | None,
        kg_service: KnowledgeGraphService | None,
        keyword_search: Callable[[str, int, str | None], list[tuple[str, float]]],
        config: MemoryConfig,
        falkordb_graph_search: bool,
        falkordb_graph_min_score: float,
        rrf_k: int,
        falkordb_rrf_k: int,
        vector_store_failure_logger: Callable[[str, BaseException], None],
        run_db: Callable[..., Awaitable[Any]] | None = None,
        search_debug_sink: Callable[[SearchDebugSnapshot], None] | None = None,
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
        *,
        session_id: str | None = None,
        recall_request_id: str | None = None,
        caller: str = "memory.search",
    ) -> list[Memory]:
        """Retrieve memories via VectorStore + optional FalkorDB graph search."""
        if query and self._vector_store and self._embed_fn:
            from gobby.search.keywords import extract_keywords

            embed_query = extract_keywords(query) or query
            query_embedding = await self._embed_fn(embed_query, is_query=True)
            half_life = getattr(self._config, "temporal_decay_half_life_days", 30.0)
            effective_min_score = min_score if min_score is not None else 0.0

            filters = memory_project_scope_filter(project_id)

            use_graph = self._kg_service is not None and self._falkordb_graph_search

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
                    session_id=session_id,
                    recall_request_id=recall_request_id,
                    caller=caller,
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
                    session_id=session_id,
                    recall_request_id=recall_request_id,
                    caller=caller,
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
        filters: Any,
        project_id: str | None,
        memory_type: str | None,
        tags_all: list[str] | None,
        tags_any: list[str] | None,
        tags_none: list[str] | None,
        half_life: float,
        effective_min_score: float,
        session_id: str | None = None,
        recall_request_id: str | None = None,
        caller: str = "memory.search",
    ) -> list[Memory]:
        graph_min_score = self._falkordb_graph_min_score
        rrf_k = self._falkordb_rrf_k
        vector_store = self._require_vector_store()

        async def _collect(candidate_limit: int) -> _Candidates:
            qdrant_coro = vector_store.search(
                query_embedding,
                limit=candidate_limit,
                filters=filters or None,
            )
            graph_coro = self._search_graph_scored(
                query_embedding=query_embedding,
                limit=candidate_limit,
                min_score=graph_min_score,
                project_id=project_id,
            )
            keyword_coro = self._keyword_ranked(query, candidate_limit, project_id)
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
                graph_scored: list[tuple[str, float]] = []
            else:
                graph_scored = graph_result

            graph_ranked = [memory_id for memory_id, _ in graph_scored]
            graph_score_map = dict(graph_scored)

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

            return _Candidates(
                merged_ids=merged_ids,
                ranking_score_map=ranking_score_map,
                qdrant_score_map=qdrant_score_map,
                qdrant_ranked=qdrant_ranked,
                keyword_ranked=keyword_ranked,
                rrf_applied=rrf_applied,
                graph_ranked=graph_ranked,
                graph_score_map=graph_score_map,
                exhausted=(
                    len(qdrant_results) < candidate_limit
                    and len(graph_scored) < candidate_limit
                    and len(keyword_ranked) < candidate_limit
                ),
            )

        def _build(candidates: _Candidates) -> list[Memory]:
            return self._build_results(
                merged_ids=candidates.merged_ids,
                ranking_score_map=candidates.ranking_score_map,
                qdrant_score_map=candidates.qdrant_score_map,
                qdrant_set=set(candidates.qdrant_ranked),
                keyword_set=set(candidates.keyword_ranked),
                graph_set=set(candidates.graph_ranked),
                graph_score_map=candidates.graph_score_map,
                rrf_applied=candidates.rrf_applied,
                project_id=project_id,
                memory_type=memory_type,
                tags_all=tags_all,
                tags_any=tags_any,
                tags_none=tags_none,
                half_life=half_life,
                effective_min_score=effective_min_score,
                limit=limit,
            )

        results, candidates = await self._collect_active_results(
            limit=limit, collect=_collect, build=_build
        )
        self._emit_search_debug(
            query=query,
            project_id=project_id,
            session_id=session_id,
            recall_request_id=recall_request_id,
            caller=caller,
            merged_ids=candidates.merged_ids,
            returned=results,
            ranking_score_map=candidates.ranking_score_map,
            rrf_applied=candidates.rrf_applied,
            graph_score_map=candidates.graph_score_map,
        )
        return results

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
        session_id: str | None = None,
        recall_request_id: str | None = None,
        caller: str = "memory.search",
    ) -> list[Memory]:
        rrf_k = self._rrf_k
        vector_store = self._require_vector_store()

        async def _collect(candidate_limit: int) -> _Candidates:
            qdrant_coro = vector_store.search(
                query_embedding,
                limit=candidate_limit,
                filters=filters or None,
            )
            keyword_coro = self._keyword_ranked(query, candidate_limit, project_id)
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

            return _Candidates(
                merged_ids=merged_ids,
                ranking_score_map=ranking_score_map,
                qdrant_score_map=qdrant_score_map,
                qdrant_ranked=qdrant_ranked,
                keyword_ranked=keyword_ranked,
                rrf_applied=rrf_applied,
                exhausted=(
                    len(qdrant_results) < candidate_limit and len(keyword_ranked) < candidate_limit
                ),
            )

        def _build(candidates: _Candidates) -> list[Memory]:
            return self._build_results(
                merged_ids=candidates.merged_ids,
                ranking_score_map=candidates.ranking_score_map,
                qdrant_score_map=candidates.qdrant_score_map,
                qdrant_set=set(candidates.qdrant_ranked),
                keyword_set=set(candidates.keyword_ranked),
                graph_set=None,
                rrf_applied=candidates.rrf_applied,
                project_id=project_id,
                memory_type=memory_type,
                tags_all=tags_all,
                tags_any=tags_any,
                tags_none=tags_none,
                half_life=half_life,
                effective_min_score=effective_min_score,
                limit=limit,
            )

        results, candidates = await self._collect_active_results(
            limit=limit, collect=_collect, build=_build
        )
        self._emit_search_debug(
            query=query,
            project_id=project_id,
            session_id=session_id,
            recall_request_id=recall_request_id,
            caller=caller,
            merged_ids=candidates.merged_ids,
            returned=results,
            ranking_score_map=candidates.ranking_score_map,
            rrf_applied=candidates.rrf_applied,
        )
        return results

    async def _collect_active_results(
        self,
        *,
        limit: int,
        collect: Callable[[int], Awaitable[_Candidates]],
        build: Callable[[_Candidates], list[Memory]],
    ) -> tuple[list[Memory], _Candidates]:
        """Over-fetch ranked candidates and backfill until ``limit`` active results.

        ``collect(candidate_limit)`` fetches and merges ranked candidates from the
        configured sources; ``build`` hydrates them active-only via ``_build_results``,
        which drops soft-hidden rows. When the active result count falls short and a
        source still has candidates to give (``not exhausted``), the pool grows
        geometrically and we retry, bounded by ``_MAX_BACKFILL_ROUNDS``. With no hidden
        rows the first round fills ``limit`` and no extra fetch runs.
        """
        candidate_limit = max(limit, 1) * _OVERFETCH_FACTOR
        candidates = await collect(candidate_limit)
        results = build(candidates)
        rounds = 0
        while len(results) < limit and not candidates.exhausted and rounds < _MAX_BACKFILL_ROUNDS:
            rounds += 1
            candidate_limit *= _BACKFILL_GROWTH
            candidates = await collect(candidate_limit)
            results = build(candidates)
        return results, candidates

    def _emit_search_debug(
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
        graph_score_map: dict[str, float] | None = None,
    ) -> None:
        if self._search_debug_sink is None:
            return

        graph_scores = dict(graph_score_map or {})
        snapshot = SearchDebugSnapshot(
            merged_ids=list(merged_ids),
            returned_ids=[mem.id for mem in returned],
            ranking_score_map=dict(ranking_score_map),
            rrf_applied=rrf_applied,
            query=query,
            project_id=project_id,
            session_id=session_id,
            recall_request_id=recall_request_id,
            caller=caller,
            graph_score_map=graph_scores,
            returned_hits=[
                SearchDebugHit(
                    memory_id=mem.id,
                    rank=rank,
                    search_via=mem.search_via,
                    similarity=mem.similarity,
                    raw_semantic_score=mem.raw_semantic_score,
                    temporal_decay_factor=mem.temporal_decay_factor,
                    ranking_score=mem.ranking_score,
                    ranking_mode=mem.ranking_mode,
                    graph_score=graph_scores.get(mem.id),
                )
                for rank, mem in enumerate(returned)
            ],
        )
        try:
            self._search_debug_sink(snapshot)
        except Exception:
            logger.debug("Search debug sink failed", exc_info=True)

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
            synthetic_similarity = False
            if raw_semantic_score is not None:
                similarity = raw_semantic_score
                if mem.source_type == "user":
                    similarity *= _USER_SOURCE_BOOST
                decay_factor = temporal_decay(mem.updated_at, half_life)
                similarity *= decay_factor
            elif graph_score_map is not None:
                # Recall expander (#17104): the vector index missed this memory, but an
                # entity it mentions matched the query. Place it on the similarity axis
                # at a discounted entity-match cosine so a confident graph hit can
                # displace a weak semantic hit -- never a higher-similarity one, since
                # both are cosines and the larger wins. Without this, a graph-only hit
                # has similarity=None, sorts below every semantic hit, and is truncated;
                # measurement showed that backfill yields zero recall lift (#17104).
                graph_confidence = graph_score_map.get(memory_id)
                if graph_confidence is not None:
                    decay_factor = temporal_decay(mem.updated_at, half_life)
                    similarity = graph_confidence * _GRAPH_SYNTHETIC_SIM_DISCOUNT * decay_factor
                    synthetic_similarity = True

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
            if synthetic_similarity:
                mem.ranking_mode = "graph_synthetic"
            elif rrf_applied:
                mem.ranking_mode = "rrf"
            elif raw_semantic_score is not None:
                mem.ranking_mode = "semantic_only"
            else:
                mem.ranking_mode = "nonsemantic_fallback"

            scored.append((mem, mem.ranking_score, similarity))

        # Semantic-first ordering on the cosine axis. similarity is the quality signal:
        # real document cosines for semantic hits, and discounted entity-match cosines
        # for graph-only recall-expander hits (#17104). Because both are cosines, the
        # larger always wins -- a graph-only hit can fill a slot a weak semantic hit
        # would have taken, but can never displace a higher-similarity semantic hit.
        # RRF (item[1]) is only a tiebreak. Making RRF rank primary (attempted in
        # #17102) regressed the default graph_search=True path -- a low-RRF graph hit
        # buried a high-similarity result -- and was reverted in #17105.
        scored.sort(
            key=lambda item: (
                item[2] is not None,
                item[2] if item[2] is not None else float("-inf"),
                item[1],
            ),
            reverse=True,
        )
        return [mem for mem, _, _ in scored[:limit]]

    async def _search_graph_scored(
        self,
        query_embedding: list[float],
        limit: int = 10,
        min_score: float = 0.5,
        project_id: str | None = None,
    ) -> list[tuple[str, float]]:
        """Search FalkorDB graph for memory IDs, each scored by entity-match confidence.

        Confidence is a cosine on the same axis as vector similarity: for a directly
        mentioned memory it is the max cosine of the query to the entities that surfaced
        it; for a CO_OCCURS-traversed memory it is the seed entity cosine attenuated by
        one structural hop (``_GRAPH_TRAVERSAL_CONFIDENCE_FACTOR``). ``_build_results``
        uses it to place graph-only hits on the similarity axis as a recall expander.
        Ordering matches the IDs-only contract: direct hits first (in entity-match
        order, deduplicated), then traversed hits.
        """
        kg_service = self._require_kg_service()
        entity_results = await kg_service.search_entities_by_vector(
            query_embedding=query_embedding,
            limit=limit,
            min_score=min_score,
            project_id=project_id,
        )

        if not entity_results:
            return []

        confidence: dict[str, float] = {}
        direct_memory_ids: list[str] = []
        entity_keys: list[str] = []
        seen_entity_keys: set[str] = set()
        seed_max_score = 0.0
        for result in entity_results:
            entity_key = result.get("entity_key")
            entity_score = float(result.get("score") or 0.0)
            if (
                entity_key
                and entity_key not in seen_entity_keys
                and len(entity_keys) < _GRAPH_EXPANSION_ENTITY_SEED_LIMIT
            ):
                seen_entity_keys.add(entity_key)
                entity_keys.append(entity_key)
                seed_max_score = max(seed_max_score, entity_score)
            for mid in result.get("memory_ids", []):
                if mid not in direct_memory_ids:
                    direct_memory_ids.append(mid)
                if entity_score > confidence.get(mid, 0.0):
                    confidence[mid] = entity_score

        traversed_memory_ids: list[str] = []
        if entity_keys:
            try:
                traversed_memory_ids = await asyncio.wait_for(
                    kg_service.find_related_memory_ids(
                        entity_keys=entity_keys,
                        max_hops=1,
                        limit=limit,
                        project_id=project_id,
                    ),
                    timeout=_GRAPH_RELATED_EXPANSION_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.warning(
                    "Graph related-memory expansion timed out after %.1fs; "
                    "returning direct graph hits",
                    _GRAPH_RELATED_EXPANSION_TIMEOUT_SECONDS,
                )
            except Exception as e:
                logger.warning("Graph related-memory expansion failed: %s", e)

        traversed_confidence = seed_max_score * _GRAPH_TRAVERSAL_CONFIDENCE_FACTOR
        seen = set(direct_memory_ids)
        merged = list(direct_memory_ids)
        for mid in traversed_memory_ids:
            if mid not in seen:
                seen.add(mid)
                merged.append(mid)
            if traversed_confidence > confidence.get(mid, 0.0):
                confidence[mid] = traversed_confidence

        return [(mid, confidence.get(mid, 0.0)) for mid in merged[:limit]]

    async def _search_graph_for_memories(
        self,
        query_embedding: list[float],
        limit: int = 10,
        min_score: float = 0.5,
        project_id: str | None = None,
    ) -> list[str]:
        """Search FalkorDB graph for memory IDs via entity vector similarity.

        Thin IDs-only view over :meth:`_search_graph_scored` for callers (the RRF merge,
        the memory facade) that only need the ranked list, not per-hit confidence.
        """
        scored = await self._search_graph_scored(
            query_embedding=query_embedding,
            limit=limit,
            min_score=min_score,
            project_id=project_id,
        )
        return [memory_id for memory_id, _ in scored]

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

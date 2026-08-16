"""Vector, graph, and keyword search-path orchestration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol, cast

from gobby.memory.services._search_models import _Candidates
from gobby.memory.services._search_rrf import rrf_scores
from gobby.memory.vectorstore import is_recoverable_vector_store_error
from gobby.storage.memories import Memory

if TYPE_CHECKING:
    from gobby.memory.services._search_graph import GraphScoredResult
    from gobby.memory.vectorstore import VectorStore

logger = logging.getLogger(__name__)
_HYBRID_QDRANT_TIMEOUT_SECONDS = 10.0


def _qdrant_hits_or_empty(
    result: object,
    *,
    service: SearchPathHost,
    caller: str,
    project_id: str | None,
    candidate_limit: int,
    path: str,
    recoverable_message: str,
) -> list[tuple[str, float]]:
    """Normalize a gathered Qdrant result without taking the store down on timeout."""
    if not isinstance(result, BaseException):
        return cast(list[tuple[str, float]], result)
    if isinstance(result, asyncio.CancelledError):
        raise result
    if isinstance(result, TimeoutError):
        logger.info(
            "Qdrant search timed out; falling back to non-vector results",
            extra={
                "caller": caller,
                "project_id": project_id,
                "limit": candidate_limit,
                "path": path,
                "error": str(result),
            },
        )
        return []
    if is_recoverable_vector_store_error(result):
        service._log_vector_store_failure(recoverable_message, result)
        return []
    logger.warning(
        "Qdrant search failed",
        extra={
            "caller": caller,
            "project_id": project_id,
            "limit": candidate_limit,
            "path": path,
            "error": str(result),
        },
        exc_info=result,
    )
    return []


class SearchPathHost(Protocol):
    """SearchService surface used by extracted search-path helpers."""

    _log_vector_store_failure: Callable[[str, BaseException], None]

    def _require_vector_store(self) -> VectorStore: ...

    async def _search_graph_scored(
        self,
        query_embedding: list[float],
        limit: int = 10,
        min_score: float = 0.5,
        project_id: str | None = None,
        include_global: bool = True,
    ) -> GraphScoredResult: ...

    async def _keyword_ranked(
        self,
        query: str,
        limit: int,
        project_id: str | None,
        include_global: bool = True,
    ) -> list[str]: ...

    async def _collect_active_results(
        self,
        *,
        limit: int,
        collect: Callable[[int], Awaitable[_Candidates]],
        build: Callable[[_Candidates], list[Memory]],
    ) -> tuple[list[Memory], _Candidates]: ...

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
    ) -> list[Memory]: ...

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
        graph_score_map: dict[str, float] | None = None,
        graph_component_map: dict[str, dict[str, float | None]] | None = None,
    ) -> None: ...


async def search_with_graph(
    service: SearchPathHost,
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
    graph_min_score: float,
    rrf_k: int,
    session_id: str | None = None,
    recall_request_id: str | None = None,
    caller: str = "memory.search",
    include_global: bool = True,
) -> list[Memory]:
    """Run vector, graph, and keyword search, then materialize active memories."""
    vector_store = service._require_vector_store()

    async def _collect(candidate_limit: int) -> _Candidates:
        qdrant_coro = vector_store.search(
            query_embedding,
            limit=candidate_limit,
            filters=filters or None,
            timeout=_HYBRID_QDRANT_TIMEOUT_SECONDS,
        )
        graph_coro = service._search_graph_scored(
            query_embedding=query_embedding,
            limit=candidate_limit,
            min_score=graph_min_score,
            project_id=project_id,
            include_global=include_global,
        )
        keyword_coro = service._keyword_ranked(
            query,
            candidate_limit,
            project_id,
            include_global=include_global,
        )
        qdrant_result, graph_result, keyword_result = await asyncio.gather(
            qdrant_coro, graph_coro, keyword_coro, return_exceptions=True
        )

        qdrant_results = _qdrant_hits_or_empty(
            qdrant_result,
            service=service,
            caller=caller,
            project_id=project_id,
            candidate_limit=candidate_limit,
            path="qdrant_graph_keyword",
            recoverable_message=("Qdrant search unavailable; falling back to non-vector results"),
        )

        if isinstance(graph_result, BaseException):
            if isinstance(graph_result, asyncio.CancelledError):
                raise graph_result
            logger.warning(
                "Graph search failed",
                extra={
                    "caller": caller,
                    "project_id": project_id,
                    "limit": candidate_limit,
                    "path": "qdrant_graph_keyword",
                    "error": str(graph_result),
                },
                exc_info=graph_result,
            )
            graph_scored: list[tuple[str, float]] = []
            graph_component_map: dict[str, dict[str, float | None]] = {}
        else:
            graph_scored = graph_result.scored
            graph_component_map = graph_result.component_map

        graph_ranked = [memory_id for memory_id, _ in graph_scored]
        graph_score_map = dict(graph_scored)

        if isinstance(keyword_result, BaseException):
            if isinstance(keyword_result, asyncio.CancelledError):
                raise keyword_result
            logger.debug(
                "Keyword search failed",
                extra={
                    "caller": caller,
                    "project_id": project_id,
                    "limit": candidate_limit,
                    "path": "qdrant_graph_keyword",
                    "error": str(keyword_result),
                },
                exc_info=keyword_result,
            )
            keyword_ranked: list[str] = []
        else:
            keyword_ranked = keyword_result

        qdrant_score_map = dict(qdrant_results)
        qdrant_ranked = [memory_id for memory_id, _ in qdrant_results]

        rrf_lists = [ranked for ranked in (qdrant_ranked, graph_ranked, keyword_ranked) if ranked]
        if len(rrf_lists) > 1:
            ranking_score_map = rrf_scores(*rrf_lists, k=rrf_k)
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
                ranking_score_map = rrf_scores(merged_ids, k=rrf_k)
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
            graph_component_map=graph_component_map,
            exhausted=(
                len(qdrant_results) < candidate_limit
                and len(graph_scored) < candidate_limit
                and len(keyword_ranked) < candidate_limit
            ),
        )

    def _build(candidates: _Candidates) -> list[Memory]:
        return service._build_results(
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

    results, candidates = await service._collect_active_results(
        limit=limit, collect=_collect, build=_build
    )
    await service._emit_search_debug(
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
        graph_component_map=candidates.graph_component_map,
    )
    return results


async def search_qdrant_keyword(
    service: SearchPathHost,
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
    rrf_k: int,
    session_id: str | None = None,
    recall_request_id: str | None = None,
    caller: str = "memory.search",
    include_global: bool = True,
) -> list[Memory]:
    """Run vector plus keyword search, then materialize active memories."""
    vector_store = service._require_vector_store()

    async def _collect(candidate_limit: int) -> _Candidates:
        qdrant_coro = vector_store.search(
            query_embedding,
            limit=candidate_limit,
            filters=filters or None,
            timeout=_HYBRID_QDRANT_TIMEOUT_SECONDS,
        )
        keyword_coro = service._keyword_ranked(
            query,
            candidate_limit,
            project_id,
            include_global=include_global,
        )
        qdrant_result, keyword_result = await asyncio.gather(
            qdrant_coro, keyword_coro, return_exceptions=True
        )

        qdrant_results = _qdrant_hits_or_empty(
            qdrant_result,
            service=service,
            caller=caller,
            project_id=project_id,
            candidate_limit=candidate_limit,
            path="qdrant_keyword",
            recoverable_message="Qdrant search unavailable; falling back to keyword results",
        )

        if isinstance(keyword_result, BaseException):
            if isinstance(keyword_result, asyncio.CancelledError):
                raise keyword_result
            logger.debug(
                "Keyword search failed",
                extra={
                    "caller": caller,
                    "project_id": project_id,
                    "limit": candidate_limit,
                    "path": "qdrant_keyword",
                    "error": str(keyword_result),
                },
                exc_info=keyword_result,
            )
            keyword_ranked: list[str] = []
        else:
            keyword_ranked = keyword_result

        qdrant_ranked = [memory_id for memory_id, _ in qdrant_results]
        qdrant_score_map = dict(qdrant_results)

        if qdrant_ranked and keyword_ranked:
            ranking_score_map = rrf_scores(qdrant_ranked, keyword_ranked, k=rrf_k)
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
            ranking_score_map = rrf_scores(keyword_ranked, k=rrf_k)
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
        return service._build_results(
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

    results, candidates = await service._collect_active_results(
        limit=limit, collect=_collect, build=_build
    )
    await service._emit_search_debug(
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

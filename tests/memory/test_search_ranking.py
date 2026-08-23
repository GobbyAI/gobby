"""Focused tests for SearchService materialization ranking."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.recall_constants import RecallConstants
from gobby.memory.services._search_graph import GraphScoredResult
from gobby.memory.services._search_keyword import KeywordSearch
from gobby.memory.services.search import SearchDebugHit, SearchDebugSnapshot, SearchService
from gobby.storage.memories import Memory

pytestmark = pytest.mark.unit


class _Storage:
    def __init__(self, memory_ids: list[str]) -> None:
        self._memory_ids = memory_ids

    def _memory(self, memory_id: str) -> Memory:
        now = datetime.now(UTC).isoformat()
        if memory_id not in self._memory_ids:
            raise ValueError(memory_id)
        return Memory(
            id=memory_id,
            memory_type="fact",
            content=memory_id,
            created_at=now,
            updated_at=now,
            source_type="agent",
            tags=[],
        )

    def get_memories(self, memory_ids: list[str], scope: Any = None) -> list[Memory]:
        return [self._memory(memory_id) for memory_id in memory_ids]

    def get_memory(self, memory_id: str, scope: Any = None) -> Memory:
        return self._memory(memory_id)

    def list_memories(self, **_kwargs: Any) -> list[Memory]:
        """The queryless branch of `search()` lists instead of ranking."""
        return [self._memory(memory_id) for memory_id in self._memory_ids]

    def update_access_stats(self, memory_id: str, accessed_at: str) -> None:
        return None


class _VectorStore:
    def __init__(self, results: list[tuple[str, float]]) -> None:
        self._results = results

    async def search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        filters: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> list[tuple[str, float]]:
        return self._results[:limit]


class _FilteringStorage:
    """Storage that mirrors active-only hydration: hidden IDs are silently dropped.

    ``get_memories`` returns rows only for ``active_ids`` (as production's
    ``visibility="active"`` default does), and ``get_memory`` raises ``ValueError`` for
    hidden IDs so ``_build_results``' per-ID fallback skips them.
    """

    def __init__(self, active_ids: list[str]) -> None:
        self._active = set(active_ids)

    def _memory(self, memory_id: str) -> Memory:
        if memory_id not in self._active:
            raise ValueError(memory_id)
        now = datetime.now(UTC).isoformat()
        return Memory(
            id=memory_id,
            memory_type="fact",
            content=memory_id,
            created_at=now,
            updated_at=now,
            source_type="agent",
            tags=[],
        )

    def get_memories(self, memory_ids: list[str], scope: Any = None) -> list[Memory]:
        return [self._memory(mid) for mid in memory_ids if mid in self._active]

    def get_memory(self, memory_id: str, scope: Any = None) -> Memory:
        return self._memory(memory_id)

    def update_access_stats(self, memory_id: str, accessed_at: str) -> None:
        return None


class _CountingVectorStore:
    """VectorStore that records how many over-fetch rounds backfill triggered."""

    def __init__(self, results: list[tuple[str, float]]) -> None:
        self._results = results
        self.calls: list[int] = []

    async def search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        filters: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> list[tuple[str, float]]:
        self.calls.append(limit)
        return self._results[:limit]


def _service(
    memory_ids: list[str],
    *,
    vector_results: list[tuple[str, float]] | None = None,
    vector_store: Any = None,
    storage: Any = None,
    keyword_search: KeywordSearch | None = None,
    search_debug_sink: Callable[[SearchDebugSnapshot], None] | None = None,
    falkordb_graph_search: bool = False,
    recall_constants: RecallConstants | None = None,
    embed_fn: Callable[..., Any] | None = None,
) -> SearchService:
    async def _embed(text: str, is_query: bool = False) -> list[float]:
        return [1.0, 0.0]

    return SearchService(
        storage=storage or _Storage(memory_ids),  # type: ignore[arg-type]
        vector_store=vector_store or _VectorStore(vector_results or []),  # type: ignore[arg-type]
        embed_fn=embed_fn or _embed,
        kg_service=object() if falkordb_graph_search else None,  # type: ignore[arg-type]
        keyword_search=keyword_search
        or (lambda query, limit, project_id, *, include_global=True: []),
        config=MemoryConfig(),
        falkordb_graph_search=falkordb_graph_search,
        falkordb_graph_min_score=0.0,
        rrf_k=60,
        falkordb_rrf_k=60,
        vector_store_failure_logger=lambda message, error: None,
        run_db=None,
        search_debug_sink=search_debug_sink,
        recall_constants=recall_constants,
    )


def test_build_results_keeps_semantic_primary_even_when_rrf_applied() -> None:
    # Regression guard for #17105: a high-similarity semantic hit must outrank a
    # higher-RRF graph-only hit even when RRF is applied. RRF/graph/keyword lists
    # expand recall and break ties; they must never displace a strong semantic result
    # from the top-K (making ranking_score primary regressed the default search path).
    service = _service(["semantic", "graph"])

    results = service._build_results(
        # Input order is graph-first to prove ordering is decided by the sort, not input.
        merged_ids=["graph", "semantic"],
        ranking_score_map={"semantic": 0.01, "graph": 0.05},
        qdrant_score_map={"semantic": 0.99},
        qdrant_set={"semantic"},
        keyword_set=set(),
        graph_set={"graph"},
        rrf_applied=True,
        project_id=None,
        memory_type=None,
        tags_all=None,
        tags_any=None,
        tags_none=None,
        half_life=0.0,
        effective_min_score=0.0,
        limit=2,
    )

    # Semantic-first: the 0.99-similarity hit beats the higher-RRF (0.05) graph-only hit.
    assert [mem.id for mem in results] == ["semantic", "graph"]
    assert [mem.ranking_mode for mem in results] == ["rrf", "rrf"]
    assert results[0].search_via == "semantic"


def test_build_results_graph_only_hit_displaces_weak_semantic_via_synthetic_similarity() -> None:
    # #17104 mechanism: a graph-only hit (the vector index missed it) whose mentioned
    # entity strongly matched the query is placed on the similarity axis at a discounted
    # entity-match cosine, so it can take a top-K slot a weak semantic hit would have had.
    # limit=2 forces an explicit displacement decision. Measurement showed the prior
    # backfill behavior (synthetic=None -> sorts last -> truncated) gave zero recall lift.
    service = _service(["strong-semantic", "weak-semantic", "graph-only"])

    results = service._build_results(
        merged_ids=["strong-semantic", "weak-semantic", "graph-only"],
        ranking_score_map={"strong-semantic": 0.01, "weak-semantic": 0.01, "graph-only": 0.5},
        qdrant_score_map={"strong-semantic": 0.95, "weak-semantic": 0.10},
        qdrant_set={"strong-semantic", "weak-semantic"},
        keyword_set=set(),
        graph_set={"graph-only"},
        graph_score_map={"graph-only": 0.8},  # entity cosine 0.8 -> synthetic 0.8*0.9=0.72
        rrf_applied=True,
        project_id=None,
        memory_type=None,
        tags_all=None,
        tags_any=None,
        tags_none=None,
        half_life=0.0,
        effective_min_score=0.0,
        limit=2,
    )

    # strong-semantic (0.95) keeps slot 1; the graph-only synthetic (0.72) outranks the
    # weak semantic hit (0.10) for slot 2. The higher-similarity semantic hit is never
    # displaced -- both are cosines and the larger wins.
    assert [mem.id for mem in results] == ["strong-semantic", "graph-only"]
    assert results[0].search_via == "semantic"
    assert results[0].ranking_mode == "rrf"
    assert results[1].search_via == "graph"
    assert results[1].ranking_mode == "graph_synthetic"
    assert results[1].similarity is not None
    assert abs(results[1].similarity - 0.72) < 1e-9


def test_build_results_graph_only_hit_never_outranks_higher_similarity_semantic() -> None:
    # Invariant guard: even a maximally confident graph-only hit (entity cosine 1.0 ->
    # synthetic 0.9) must sit below a higher-similarity semantic hit. With room for all
    # three it slots strictly by its synthetic cosine: below the 0.95 hit, above the 0.40.
    service = _service(["top-semantic", "low-semantic", "graph-only"])

    results = service._build_results(
        merged_ids=["graph-only", "top-semantic", "low-semantic"],
        ranking_score_map={"graph-only": 0.9, "top-semantic": 0.01, "low-semantic": 0.01},
        qdrant_score_map={"top-semantic": 0.95, "low-semantic": 0.40},
        qdrant_set={"top-semantic", "low-semantic"},
        keyword_set=set(),
        graph_set={"graph-only"},
        graph_score_map={"graph-only": 1.0},  # synthetic 1.0*0.9 = 0.90
        rrf_applied=True,
        project_id=None,
        memory_type=None,
        tags_all=None,
        tags_any=None,
        tags_none=None,
        half_life=0.0,
        effective_min_score=0.0,
        limit=3,
    )

    assert [mem.id for mem in results] == ["top-semantic", "graph-only", "low-semantic"]
    assert results[0].search_via == "semantic"
    assert results[0].ranking_mode == "rrf"
    assert results[1].search_via == "graph"
    assert results[1].ranking_mode == "graph_synthetic"


@pytest.mark.asyncio
async def test_search_with_graph_propagates_graph_cancellation() -> None:
    service = _service(
        ["semantic"],
        vector_results=[("semantic", 0.9)],
        falkordb_graph_search=True,
    )

    async def cancelled_graph(**_kwargs: Any) -> list[tuple[str, float]]:
        raise asyncio.CancelledError()

    service._search_graph_scored = cancelled_graph  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await service._search_with_graph(
            query="semantic",
            query_embedding=[1.0, 0.0],
            limit=1,
            filters=None,
            project_id=None,
            memory_type=None,
            tags_all=None,
            tags_any=None,
            tags_none=None,
            half_life=0.0,
            effective_min_score=0.0,
        )


@pytest.mark.asyncio
async def test_search_with_graph_propagates_keyword_cancellation() -> None:
    service = _service(
        ["semantic"],
        vector_results=[("semantic", 0.9)],
        falkordb_graph_search=True,
    )

    async def cancelled_keyword(
        _query: str, _limit: int, _project_id: str | None, include_global: bool = True
    ) -> list[str]:
        raise asyncio.CancelledError()

    service._keyword_ranked = cancelled_keyword  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await service._search_with_graph(
            query="semantic",
            query_embedding=[1.0, 0.0],
            limit=1,
            filters=None,
            project_id=None,
            memory_type=None,
            tags_all=None,
            tags_any=None,
            tags_none=None,
            half_life=0.0,
            effective_min_score=0.0,
        )


@pytest.mark.asyncio
async def test_qdrant_keyword_search_propagates_keyword_cancellation() -> None:
    service = _service(["semantic"], vector_results=[("semantic", 0.9)])

    async def cancelled_keyword(
        _query: str, _limit: int, _project_id: str | None, include_global: bool = True
    ) -> list[str]:
        raise asyncio.CancelledError()

    service._keyword_ranked = cancelled_keyword  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await service._search_qdrant_keyword(
            query="semantic",
            query_embedding=[1.0, 0.0],
            limit=1,
            filters=None,
            project_id=None,
            memory_type=None,
            tags_all=None,
            tags_any=None,
            tags_none=None,
            half_life=0.0,
            effective_min_score=0.0,
        )


def test_build_results_preserves_semantic_primary_order_without_rrf() -> None:
    service = _service(["low-semantic", "high-semantic", "keyword"])

    results = service._build_results(
        merged_ids=["low-semantic", "keyword", "high-semantic"],
        ranking_score_map={"low-semantic": 99.0, "keyword": 100.0, "high-semantic": 1.0},
        qdrant_score_map={"low-semantic": 0.2, "high-semantic": 0.9},
        qdrant_set={"low-semantic", "high-semantic"},
        keyword_set={"keyword"},
        graph_set=None,
        rrf_applied=False,
        project_id=None,
        memory_type=None,
        tags_all=None,
        tags_any=None,
        tags_none=None,
        half_life=0.0,
        effective_min_score=0.0,
        limit=3,
    )

    assert [mem.id for mem in results] == ["high-semantic", "low-semantic", "keyword"]
    assert [mem.ranking_mode for mem in results] == [
        "semantic_only",
        "semantic_only",
        "nonsemantic_fallback",
    ]


async def test_qdrant_keyword_path_emits_debug_snapshot() -> None:
    snapshots: list[SearchDebugSnapshot] = []
    recall_constants = RecallConstants(
        half_life_days=30.0,
        graph_synthetic_discount=0.9,
        cooccur_alpha=0.5,
        cooccur_support_cap=5,
        source="fitted",
        provenance="decision-digest-123",
    )
    service = _service(
        ["semantic"],
        vector_results=[("semantic", 0.9)],
        search_debug_sink=snapshots.append,
        recall_constants=recall_constants,
    )

    results = await service._search_qdrant_keyword(
        query="query",
        query_embedding=[1.0, 0.0],
        limit=1,
        filters={},
        project_id=None,
        memory_type=None,
        tags_all=None,
        tags_any=None,
        tags_none=None,
        half_life=0.0,
        effective_min_score=0.0,
        session_id="session-1",
        recall_request_id="request-1",
        caller="memory.recall",
    )

    assert [mem.id for mem in results] == ["semantic"]
    assert snapshots == [
        SearchDebugSnapshot(
            merged_ids=["semantic"],
            returned_ids=["semantic"],
            ranking_score_map={"semantic": 0.9},
            rrf_applied=False,
            query="query",
            session_id="session-1",
            recall_request_id="request-1",
            caller="memory.recall",
            constants_provenance="decision-digest-123",
            returned_hits=[
                SearchDebugHit(
                    memory_id="semantic",
                    rank=0,
                    search_via="semantic",
                    similarity=0.9,
                    raw_semantic_score=0.9,
                    temporal_decay_factor=1.0,
                    ranking_score=0.9,
                    ranking_mode="semantic_only",
                    graph_score=None,
                    content_hash="3784070fe3e7e3de5f0ec08eadfa10acbaa0f543916b1ab2c68f371924ff7db3",
                )
            ],
        )
    ]


async def test_graph_path_emits_debug_snapshot(monkeypatch: Any) -> None:
    snapshots: list[SearchDebugSnapshot] = []
    service = _service(
        ["semantic", "graph"],
        vector_results=[("semantic", 0.9)],
        search_debug_sink=snapshots.append,
        falkordb_graph_search=True,
    )

    async def graph_search(**kwargs: Any) -> GraphScoredResult:
        return GraphScoredResult(
            scored=[("graph", 0.05)],
            component_map={
                "graph": {
                    "edge_cosine": 0.8,
                    "edge_support_norm": 0.4,
                    "edge_weight_blend": 0.6,
                    "edge_decay_factor": 1.0,
                }
            },
        )

    monkeypatch.setattr(service, "_search_graph_scored", graph_search)
    results = await service._search_with_graph(
        query="query",
        query_embedding=[1.0, 0.0],
        limit=2,
        filters={},
        project_id=None,
        memory_type=None,
        tags_all=None,
        tags_any=None,
        tags_none=None,
        half_life=0.0,
        effective_min_score=0.0,
    )

    assert [mem.id for mem in results] == ["semantic", "graph"]
    assert len(snapshots) == 1
    assert snapshots[0].merged_ids == ["semantic", "graph"]
    assert snapshots[0].returned_ids == ["semantic", "graph"]
    assert snapshots[0].rrf_applied is True
    assert snapshots[0].query == "query"
    assert snapshots[0].graph_score_map == {"graph": 0.05}
    assert snapshots[0].graph_component_map == {
        "graph": {
            "edge_cosine": 0.8,
            "edge_support_norm": 0.4,
            "edge_weight_blend": 0.6,
            "edge_decay_factor": 1.0,
        }
    }
    assert snapshots[0].returned_hits[1].memory_id == "graph"
    assert snapshots[0].returned_hits[1].ranking_mode == "graph_synthetic"
    assert snapshots[0].returned_hits[1].graph_score == 0.05


@pytest.mark.asyncio
async def test_debug_sink_failure_does_not_change_results() -> None:
    sink_called = False

    def failing_sink(snapshot: SearchDebugSnapshot) -> None:
        nonlocal sink_called
        sink_called = True
        raise RuntimeError("diagnostic sink failed")

    service = _service(["semantic"], search_debug_sink=failing_sink)

    await service._emit_search_debug(
        query="query",
        project_id=None,
        session_id=None,
        recall_request_id=None,
        caller="memory.search",
        merged_ids=["semantic"],
        returned=[],
        ranking_score_map={},
        rrf_applied=False,
    )
    assert sink_called is True


@pytest.mark.asyncio
async def test_search_backfills_until_limit_active_results() -> None:
    """Soft-hidden IDs eat the first over-fetch page; backfill recovers active results.

    The vector store ranks four hidden rows ahead of four active ones. The first round
    (``limit * 2`` candidates) hydrates to nothing, so backfill grows the candidate pool
    until ``limit`` active rows survive hydration (#17162).
    """
    hidden = ["h1", "h2", "h3", "h4"]
    active = ["a1", "a2", "a3", "a4"]
    ranked = hidden + active
    vector_store = _CountingVectorStore(
        [(mid, 0.95 - index * 0.05) for index, mid in enumerate(ranked)]
    )
    service = _service([], vector_store=vector_store, storage=_FilteringStorage(active))

    results = await service.search("query", limit=2)

    assert [memory.id for memory in results] == ["a1", "a2"]
    # Round 0 over-fetched 4 (all hidden), round 1 grew to 8 and filled the limit.
    assert vector_store.calls == [4, 8]


@pytest.mark.asyncio
async def test_search_stops_backfill_when_sources_exhausted() -> None:
    """Backfill halts (no infinite loop) once a source returns fewer than requested."""
    vector_store = _CountingVectorStore([("h1", 0.9), ("a1", 0.8)])
    service = _service([], vector_store=vector_store, storage=_FilteringStorage(["a1"]))

    results = await service.search("query", limit=5)

    assert [memory.id for memory in results] == ["a1"]
    # First page already returned fewer than the 10 requested -> exhausted, no retry.
    assert vector_store.calls == [10]


@pytest.mark.asyncio
async def test_search_no_backfill_when_first_page_fills() -> None:
    """The common path (no hidden rows) fetches a single page and never backfills."""
    active = ["a1", "a2", "a3", "a4"]
    vector_store = _CountingVectorStore(
        [(mid, 0.95 - index * 0.05) for index, mid in enumerate(active)]
    )
    service = _service([], vector_store=vector_store, storage=_FilteringStorage(active))

    results = await service.search("query", limit=2)

    assert [memory.id for memory in results] == ["a1", "a2"]
    assert vector_store.calls == [4]


def _fallback_service(
    memory_ids: list[str],
    *,
    keyword_results: list[tuple[str, float]],
    search_debug_sink: Callable[[SearchDebugSnapshot], None] | None = None,
) -> SearchService:
    """SearchService with no vector store/embed_fn, so search() takes _keyword_fallback."""
    return SearchService(
        storage=_Storage(memory_ids),  # type: ignore[arg-type]
        vector_store=None,
        embed_fn=None,
        kg_service=None,
        keyword_search=lambda query, limit, project_id, *, include_global=True: keyword_results,
        config=MemoryConfig(),
        falkordb_graph_search=False,
        falkordb_graph_min_score=0.0,
        rrf_k=60,
        falkordb_rrf_k=60,
        vector_store_failure_logger=lambda message, error: None,
        run_db=None,
        search_debug_sink=search_debug_sink,
    )


@pytest.mark.asyncio
async def test_keyword_fallback_emits_debug_snapshot_with_join_keys() -> None:
    """Fallback searches are never silent: one event per completed search (#17491)."""
    snapshots: list[SearchDebugSnapshot] = []
    service = _fallback_service(
        ["kw"],
        keyword_results=[("kw", 0.7)],
        search_debug_sink=snapshots.append,
    )

    results = await service.search(
        "query",
        limit=1,
        session_id="session-1",
        recall_request_id="request-1",
        caller="memory.recall",
    )

    assert [mem.id for mem in results] == ["kw"]
    assert results[0].search_via == "keyword"
    assert snapshots == [
        SearchDebugSnapshot(
            merged_ids=["kw"],
            returned_ids=["kw"],
            ranking_score_map={"kw": 0.7},
            rrf_applied=False,
            query="query",
            session_id="session-1",
            recall_request_id="request-1",
            caller="memory.recall",
            returned_hits=[
                SearchDebugHit(
                    memory_id="kw",
                    rank=0,
                    search_via="keyword",
                    similarity=0.7,
                    raw_semantic_score=None,
                    temporal_decay_factor=None,
                    ranking_score=None,
                    ranking_mode=None,
                    graph_score=None,
                    content_hash="103c54b6c5b1ad282520a33d86320b77259e797cabe194b9200fb23d965561a3",
                )
            ],
        )
    ]


@pytest.mark.asyncio
async def test_keyword_fallback_emits_debug_snapshot_when_empty() -> None:
    """A fallback search with zero hits still emits its event."""
    snapshots: list[SearchDebugSnapshot] = []
    service = _fallback_service(
        [],
        keyword_results=[],
        search_debug_sink=snapshots.append,
    )

    results = await service.search(
        "query",
        limit=1,
        session_id="session-1",
        recall_request_id="request-1",
        caller="memory.recall",
    )

    assert results == []
    assert len(snapshots) == 1
    assert snapshots[0].caller == "memory.recall"
    assert snapshots[0].session_id == "session-1"
    assert snapshots[0].recall_request_id == "request-1"


@pytest.mark.asyncio
async def test_search_with_graph_qdrant_timeout_is_info_soft_miss(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: Any,
) -> None:
    service = _service(
        ["keyword-hit"],
        vector_results=[("semantic", 0.9)],
        falkordb_graph_search=True,
        keyword_search=lambda query, limit, project_id, *, include_global=True: [
            ("keyword-hit", 1.0)
        ],
    )

    async def timeout_search(
        query_embedding: list[float],
        limit: int = 10,
        filters: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> list[tuple[str, float]]:
        raise TimeoutError("deadline")

    async def empty_graph(**_kwargs: Any) -> GraphScoredResult:
        return GraphScoredResult()

    monkeypatch.setattr(service._require_vector_store(), "search", timeout_search)
    monkeypatch.setattr(service, "_search_graph_scored", empty_graph)

    with caplog.at_level(logging.INFO, logger="gobby.memory.services._search_paths"):
        results = await service._search_with_graph(
            query="keyword-hit",
            query_embedding=[1.0, 0.0],
            limit=1,
            filters=None,
            project_id=None,
            memory_type=None,
            tags_all=None,
            tags_any=None,
            tags_none=None,
            half_life=0.0,
            effective_min_score=0.0,
        )

    assert [memory.id for memory in results] == ["keyword-hit"]
    messages = [record.getMessage() for record in caplog.records]
    assert any("Qdrant search timed out" in message for message in messages)
    assert not any(
        record.levelno >= logging.WARNING and "Qdrant search failed" in record.getMessage()
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# 2.1 — split query representations across the search legs
# ---------------------------------------------------------------------------

# A deliberately conversational prompt: long enough for YAKE to fire and noisy
# enough to clear the extractor's noise threshold, so the YAKE-derived embedding
# text is provably different from the raw string.
_NOISY_PROMPT = "hey could you maybe take a look at the webhook handler thing please"


def _recorded_search_service(
    *,
    embedded: list[tuple[str, bool]],
    keyword_queries: list[str],
) -> SearchService:
    """SearchService whose embed and BM25 legs record the text each one received."""

    async def _embed(text: str, is_query: bool = False) -> list[float]:
        embedded.append((text, is_query))
        return [1.0, 0.0]

    def _keyword(
        query: str,
        limit: int,
        project_id: str | None,
        *,
        include_global: bool = True,
    ) -> list[tuple[str, float]]:
        keyword_queries.append(query)
        return []

    return _service(
        ["m1"],
        vector_results=[("m1", 0.9)],
        embed_fn=_embed,
        keyword_search=_keyword,
    )


@pytest.mark.asyncio
async def test_embed_text_absent_preserves_yake_path() -> None:
    """2.1.2: omitting `embed_text` leaves the YAKE-derived embedding untouched.

    Passing the new keyword explicitly as ``None`` is the same contract as omitting
    it, so both spellings must keep the pre-2.1 behavior for every existing caller.
    """
    from gobby.search.keywords import extract_keywords

    expected = extract_keywords(_NOISY_PROMPT) or _NOISY_PROMPT
    assert expected != _NOISY_PROMPT, "fixture must be noisy enough for YAKE to rewrite"

    embedded: list[tuple[str, bool]] = []
    keyword_queries: list[str] = []
    service = _recorded_search_service(embedded=embedded, keyword_queries=keyword_queries)

    await service.search(_NOISY_PROMPT, limit=1)
    await service.search(_NOISY_PROMPT, limit=1, embed_text=None)

    assert [text for text, _ in embedded] == [expected, expected]
    assert all(is_query for _, is_query in embedded)
    assert keyword_queries == [_NOISY_PROMPT, _NOISY_PROMPT]


@pytest.mark.asyncio
async def test_embed_text_present_is_embedded_verbatim() -> None:
    """2.1.1: a supplied `embed_text` is embedded as-is, with YAKE skipped."""
    embedded: list[tuple[str, bool]] = []
    keyword_queries: list[str] = []
    service = _recorded_search_service(embedded=embedded, keyword_queries=keyword_queries)

    await service.search("webhook handler", limit=1, embed_text=_NOISY_PROMPT)

    assert [text for text, _ in embedded] == [_NOISY_PROMPT]
    # The BM25 leg keeps the term-bag query; only the vector leg sees the prose.
    assert keyword_queries == ["webhook handler"]


@pytest.mark.asyncio
async def test_embed_text_is_ignored_without_a_query() -> None:
    """`query` still gates the hybrid path: no query means no embedding at all."""
    embedded: list[tuple[str, bool]] = []
    keyword_queries: list[str] = []
    service = _recorded_search_service(embedded=embedded, keyword_queries=keyword_queries)

    await service.search(None, limit=1, embed_text=_NOISY_PROMPT)

    assert embedded == []
    assert keyword_queries == []


def test_search_and_facade_declare_embed_text_as_optional_keyword() -> None:
    """The seam is opt-in on both surfaces, so no existing caller changes."""
    import inspect

    from gobby.memory.facade import MemoryManagerFacadeMethods

    for func in (SearchService.search, MemoryManagerFacadeMethods.search_memories):
        parameter = inspect.signature(func).parameters["embed_text"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, func.__qualname__
        assert parameter.default is None, func.__qualname__


@pytest.mark.asyncio
async def test_facade_threads_embed_text_to_the_search_service() -> None:
    """The facade forwards `embed_text` rather than dropping it on the floor."""
    from gobby.memory.facade import MemoryManagerFacadeMethods

    calls: list[dict[str, Any]] = []

    class _RecordingSearchService:
        async def search(self, **kwargs: Any) -> list[Memory]:
            calls.append(kwargs)
            return []

    facade = MemoryManagerFacadeMethods()
    facade._search_service = cast(Any, _RecordingSearchService())

    await facade.search_memories(query="webhook handler", embed_text=_NOISY_PROMPT)
    await facade.search_memories(query="webhook handler")

    assert [call["embed_text"] for call in calls] == [_NOISY_PROMPT, None]

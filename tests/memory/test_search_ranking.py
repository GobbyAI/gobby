"""Focused tests for SearchService materialization ranking."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.services._search_graph import GraphScoredResult
from gobby.memory.services._search_keyword import KeywordSearch
from gobby.memory.services.search import SearchService
from gobby.storage.memories import Memory
from gobby.storage.memories_models import MemoryType

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

    async def score_ids(
        self,
        query_embedding: list[float],
        ids: list[str],
        timeout: float | None = None,
    ) -> dict[str, float]:
        """This double holds vectors only for what its window returns."""
        return {}


def _service(
    memory_ids: list[str],
    *,
    vector_results: list[tuple[str, float]] | None = None,
    vector_store: Any = None,
    storage: Any = None,
    keyword_search: KeywordSearch | None = None,
    falkordb_graph_search: bool = False,
    embed_fn: Callable[..., Any] | None = None,
) -> SearchService:
    async def _embed(text: str, is_query: bool = False) -> list[float]:
        return [1.0, 0.0]

    return SearchService(
        storage=cast(Any, storage or _Storage(memory_ids)),
        vector_store=cast(Any, vector_store or _VectorStore(vector_results or [])),
        embed_fn=embed_fn or _embed,
        kg_service=cast(Any, object()) if falkordb_graph_search else None,
        keyword_search=keyword_search
        or (lambda query, limit, project_id, *, include_global=True: []),
        config=MemoryConfig(),
        falkordb_graph_search=falkordb_graph_search,
        falkordb_graph_min_score=0.0,
        rrf_k=60,
        falkordb_rrf_k=60,
        vector_store_failure_logger=lambda message, error: None,
        run_db=None,
    )


def test_build_results_keeps_top_semantic_hit_first_when_rrf_applied() -> None:
    # Regression guard for #17105: the highest-similarity semantic hit keeps first
    # place over a higher-RRF graph-only hit even when RRF is applied. The fused order
    # can lift a hit as far as second; it never takes the top slot from the best
    # semantic match (making ranking_score primary regressed the default search path).
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

    # The 0.99-similarity hit leads; the higher-RRF (0.05) graph-only hit follows it.
    assert [mem.id for mem in results] == ["semantic", "graph"]
    assert [mem.ranking_mode for mem in results] == ["rrf", "rrf"]
    assert results[0].search_via == "semantic"


def test_build_results_lifts_the_top_fused_hit_to_second() -> None:
    # #22410: a memory every search confirmed ranked last of eight on similarity alone.
    # The fused order alternates with the similarity order, so its leader lands second,
    # ahead of a higher-similarity hit that only the semantic search found.
    service = _service(["similar", "middling", "confirmed"])

    results = service._build_results(
        merged_ids=["confirmed", "middling", "similar"],
        ranking_score_map={"similar": 0.015, "middling": 0.016, "confirmed": 0.048},
        qdrant_score_map={"similar": 0.80, "middling": 0.70, "confirmed": 0.60},
        qdrant_set={"similar", "middling", "confirmed"},
        keyword_set={"confirmed"},
        graph_set={"confirmed"},
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

    assert [mem.id for mem in results] == ["similar", "confirmed", "middling"]


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

    # strong-semantic (0.95) keeps slot 1; the graph-only hit takes slot 2 from the weak
    # semantic hit (0.10) on both orders: its synthetic cosine (0.72) and its fused score
    # are each the larger. The top-similarity hit is never displaced.
    assert [mem.id for mem in results] == ["strong-semantic", "graph-only"]
    assert results[0].search_via == "semantic"
    assert results[0].ranking_mode == "rrf"
    assert results[1].search_via == "graph"
    assert results[1].ranking_mode == "graph_synthetic"
    assert results[1].similarity is not None
    assert abs(results[1].similarity - 0.72) < 1e-9


def test_build_results_graph_only_hit_never_outranks_the_top_similarity_semantic() -> None:
    # Invariant guard: even a maximally confident graph-only hit (entity cosine 1.0 ->
    # synthetic 0.9) with the highest fused score must sit below the top-similarity
    # semantic hit. With room for all three it lands below the 0.95 hit, above the 0.40.
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


def test_build_results_leads_with_the_similarity_order_without_rrf() -> None:
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


class _RescoreRecordingStore:
    """Records every over-fetch round and every ``score_ids`` batch (#20874)."""

    def __init__(self, results: list[tuple[str, float]], scores: dict[str, float]) -> None:
        self._results = results
        self._scores = scores
        self.search_calls: list[int] = []
        self.score_calls: list[list[str]] = []

    async def search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        filters: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> list[tuple[str, float]]:
        self.search_calls.append(limit)
        return self._results[:limit]

    async def score_ids(
        self,
        query_embedding: list[float],
        ids: list[str],
        timeout: float | None = None,
    ) -> dict[str, float]:
        self.score_calls.append(list(ids))
        return {
            memory_id: self._scores[memory_id] for memory_id in ids if memory_id in self._scores
        }


@pytest.mark.asyncio
async def test_backfill_rounds_do_not_rescore_ids_already_scored() -> None:
    """A backfill round pays ``score_ids`` only for ids no earlier round asked about.

    Every round re-merges the same keyword ids from its wider window, so before
    #20874 each of them went back to ``score_ids`` every round. Round 0 asks
    about both keyword hits -- kw-a has a stored vector, kw-b has none -- and
    round 1 must issue no ``score_ids`` call at all: the memo replays kw-a's
    cosine into the round's map and remembers kw-b is scoreless.
    """
    hidden = [f"h{index}" for index in range(6)]
    vector_store = _RescoreRecordingStore(
        [(mid, 0.95 - index * 0.01) for index, mid in enumerate(hidden)],
        scores={"kw-a": 0.8},
    )
    keyword_ids = ["kw-a", "kw-b"]
    service = _service(
        [],
        vector_store=vector_store,
        storage=_FilteringStorage(keyword_ids),
        keyword_search=lambda query, limit, project_id, *, include_global=True: [
            (memory_id, 1.0) for memory_id in keyword_ids
        ],
    )

    results = await service.search("query", limit=3)

    # Two rounds actually ran: the hidden window starved round 0's hydration.
    assert vector_store.search_calls == [6, 12]
    assert vector_store.score_calls == [["kw-a", "kw-b"]]
    by_id = {memory.id: memory for memory in results}
    # Round 1 built kw-a from the memo's replayed cosine, not a second fetch.
    assert by_id["kw-a"].raw_semantic_score == 0.8
    # A keyword hit with no stored vector stays unscored rather than re-asked.
    assert by_id["kw-b"].similarity is None


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

# A deliberately conversational prompt: the kind of text a keyword extractor would
# rewrite, so a test that sees it embedded unchanged proves nothing rewrote it.
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
async def test_query_is_embedded_verbatim() -> None:
    """No supplied `embed_text` embeds the query exactly as written (#22489).

    Three spellings mean "nothing supplied" and must all embed the query:
    omitting the keyword, passing it as ``None``, and passing an empty string. The
    empty string matters because a caller's query builder can legitimately produce
    one, and embedding it verbatim would hand the vector leg a meaningless vector
    while silently discarding the query the caller actually had.
    """
    expected = _NOISY_PROMPT

    embedded: list[tuple[str, bool]] = []
    keyword_queries: list[str] = []
    service = _recorded_search_service(embedded=embedded, keyword_queries=keyword_queries)

    await service.search(_NOISY_PROMPT, limit=1)
    await service.search(_NOISY_PROMPT, limit=1, embed_text=None)
    await service.search(_NOISY_PROMPT, limit=1, embed_text="")

    assert [text for text, _ in embedded] == [expected, expected, expected]
    assert all(is_query for _, is_query in embedded)
    assert keyword_queries == [_NOISY_PROMPT] * 3


@pytest.mark.asyncio
async def test_embed_text_overrides_query_for_embedding() -> None:
    """A supplied `embed_text` is embedded as-is, in place of the query."""
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


class _AgedStorage:
    """Storage whose memories carry the ages the decay axis is measured against."""

    def __init__(self, ages_in_days: dict[str, float]) -> None:
        self._ages = ages_in_days

    def _memory(self, memory_id: str) -> Memory:
        if memory_id not in self._ages:
            raise ValueError(memory_id)
        updated = datetime.now(UTC) - timedelta(days=self._ages[memory_id])
        return Memory(
            id=memory_id,
            memory_type=MemoryType.FACT,
            content=memory_id,
            created_at=updated,
            updated_at=updated,
            source_type="agent",
            tags=[],
        )

    def get_memories(self, memory_ids: list[str], scope: Any = None) -> list[Memory]:
        return [self._memory(memory_id) for memory_id in memory_ids]

    def get_memory(self, memory_id: str, scope: Any = None) -> Memory:
        return self._memory(memory_id)

    def update_access_stats(self, memory_id: str, accessed_at: str) -> None:
        return None


def test_the_search_floor_tests_the_undecayed_score() -> None:
    """#20858 Defect A: `min_score` gated the decayed score, so it read as recency.

    `similarity` is `cosine * user_boost * temporal_decay`, so thresholding it made
    the search floor unsatisfiable for an ordinary memory: at the live corpus median
    age of 25.9 days the decay factor is 0.549, which demanded `cosine >= 1.002` at
    the 0.55 floor. Everything aged past the median was cut before the selection gate
    could judge it, and null-similarity keyword hits -- exempt because the guard reads
    `similarity is not None` -- filled the slots it vacated.
    """
    service = _service([], storage=_AgedStorage({"aged-strong": 30.0, "fresh-weak": 0.0}))

    results = service._build_results(
        merged_ids=["aged-strong", "fresh-weak"],
        ranking_score_map={"aged-strong": 0.01, "fresh-weak": 0.01},
        # One half-life old, so decay is exactly 0.5 and the decayed score is 0.45 --
        # under the floor on the old axis, over it on the axis that means relevance.
        qdrant_score_map={"aged-strong": 0.90, "fresh-weak": 0.50},
        qdrant_set={"aged-strong", "fresh-weak"},
        keyword_set=set(),
        graph_set=set(),
        rrf_applied=True,
        project_id=None,
        memory_type=None,
        tags_all=None,
        tags_any=None,
        tags_none=None,
        half_life=30.0,
        effective_min_score=0.55,
        limit=5,
    )

    # The aged hit is admitted on its 0.90 cosine; the fresh 0.50 hit is still cut,
    # so this is a change of axis and not a blanket loosening.
    assert [mem.id for mem in results] == ["aged-strong"]
    # Ranking keeps the decayed value: age still orders results, it just no longer
    # decides eligibility.
    admitted = results[0]
    assert admitted.similarity is not None
    assert abs(admitted.similarity - 0.45) < 1e-6
    assert admitted.raw_semantic_score == 0.90


def test_the_search_floor_keeps_an_aged_graph_only_hit_eligible() -> None:
    """The recall expander (#17104) has to survive the same axis correction.

    A graph-only hit carries a synthetic cosine and no raw score, so reading the raw
    score at the floor would have deleted the expander outright -- the same trap
    #20831 avoided at the selection gate. Dividing the decay back out keeps it.
    """
    service = _service([], storage=_AgedStorage({"graph-only": 60.0}))

    results = service._build_results(
        merged_ids=["graph-only"],
        ranking_score_map={"graph-only": 0.5},
        qdrant_score_map={},
        qdrant_set=set(),
        keyword_set=set(),
        graph_set={"graph-only"},
        # Entity cosine 0.70 discounted to 0.63; two half-lives old, so the decayed
        # value is 0.1575 and only the undecayed axis can admit it.
        graph_score_map={"graph-only": 0.70},
        rrf_applied=False,
        project_id=None,
        memory_type=None,
        tags_all=None,
        tags_any=None,
        tags_none=None,
        half_life=30.0,
        effective_min_score=0.55,
        limit=5,
    )

    assert [mem.id for mem in results] == ["graph-only"]
    assert results[0].ranking_mode == "graph_synthetic"
    assert results[0].raw_semantic_score is None
    assert results[0].similarity is not None
    assert abs(results[0].similarity - 0.1575) < 1e-6


def test_a_graph_expander_hit_is_admitted_on_confidence_not_its_real_cosine() -> None:
    """The expander's whole value is the low-cosine, high-confidence hit.

    Since #20858 `_score_unwindowed_candidates` fills `qdrant_score_map` for any
    candidate the collection can score, so a graph-found memory stopped taking
    the synthetic branch and met the cosine floor on its real score. Anything
    the expander finds that also clears that floor, the vector leg already
    found -- so gating it on cosine is equivalent to switching #17104 off. The
    admission axis is graph confidence; the real cosine ranks (#20873).
    """
    service = _service([], storage=_AgedStorage({"expander-find": 0.0}))

    results = service._build_results(
        merged_ids=["expander-find"],
        ranking_score_map={"expander-find": 0.5},
        # The reviewer's worked example: confidence 0.80 against a real cosine of
        # 0.40. Both a non-empty score map and a non-zero floor, which is the
        # combination no test covered.
        qdrant_score_map={"expander-find": 0.40},
        qdrant_set=set(),
        keyword_set=set(),
        graph_set={"expander-find"},
        graph_score_map={"expander-find": 0.80},
        rrf_applied=False,
        project_id=None,
        memory_type=None,
        tags_all=None,
        tags_any=None,
        tags_none=None,
        half_life=30.0,
        effective_min_score=0.55,
        limit=5,
    )

    assert [mem.id for mem in results] == ["expander-find"]
    admitted = results[0]
    assert admitted.graph_confidence == 0.80
    # Ranking keeps #20858's improvement: the real cosine stands in for the
    # invented one, it just no longer decides eligibility.
    assert admitted.raw_semantic_score == 0.40
    assert admitted.similarity is not None
    assert abs(admitted.similarity - 0.40) < 1e-6


def test_a_graph_expander_hit_below_the_confidence_floor_is_dropped() -> None:
    """Confidence gates; it does not exempt. A weak entity match is still cut.

    0.512 is the p10 of the measured 2026-08 confidence distribution, so this
    is a value the expander really produces rather than one chosen to fail.
    """
    service = _service([], storage=_AgedStorage({"weak-link": 0.0}))

    results = service._build_results(
        merged_ids=["weak-link"],
        ranking_score_map={"weak-link": 0.5},
        qdrant_score_map={"weak-link": 0.40},
        qdrant_set=set(),
        keyword_set=set(),
        graph_set={"weak-link"},
        graph_score_map={"weak-link": 0.512},
        rrf_applied=False,
        project_id=None,
        memory_type=None,
        tags_all=None,
        tags_any=None,
        tags_none=None,
        half_life=30.0,
        effective_min_score=0.55,
        limit=5,
    )

    assert results == []


def test_a_graph_hit_the_vector_leg_also_found_is_gated_on_its_cosine() -> None:
    """Confidence admits only what the vector leg missed.

    A memory in `qdrant_set` was returned by the semantic leg's own window, so
    it is a semantic hit that the graph also happens to mention. Letting its
    entity confidence rescue a sub-floor cosine would widen the semantic axis
    under cover of the expander, which is not what #20873 decided.
    """
    service = _service([], storage=_AgedStorage({"both-legs": 0.0}))

    results = service._build_results(
        merged_ids=["both-legs"],
        ranking_score_map={"both-legs": 0.5},
        qdrant_score_map={"both-legs": 0.40},
        qdrant_set={"both-legs"},
        keyword_set=set(),
        graph_set={"both-legs"},
        graph_score_map={"both-legs": 0.95},
        rrf_applied=False,
        project_id=None,
        memory_type=None,
        tags_all=None,
        tags_any=None,
        tags_none=None,
        half_life=30.0,
        effective_min_score=0.55,
        limit=5,
    )

    assert results == []


class _ScoringVectorStore:
    """A vector store that can score any stored id, not only its own top-N.

    The narrow `search` window is the point: it returns the top hits the semantic
    leg would fetch, while `score_ids` answers for anything else the collection
    holds -- which is what Qdrant can actually do.
    """

    def __init__(
        self,
        results: list[tuple[str, float]],
        stored: dict[str, float],
    ) -> None:
        self._results = results
        self._stored = stored
        self.scored_ids: list[list[str]] = []

    async def search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        filters: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> list[tuple[str, float]]:
        return self._results[:limit]

    async def score_ids(
        self,
        query_embedding: list[float],
        ids: list[str],
        timeout: float | None = None,
    ) -> dict[str, float]:
        self.scored_ids.append(list(ids))
        return {
            memory_id: self._stored[memory_id] for memory_id in ids if memory_id in self._stored
        }


async def test_a_graph_hit_the_semantic_window_missed_keeps_its_real_cosine(
    monkeypatch: Any,
) -> None:
    """#20858 Defect B: a knowable cosine was replaced by a fabricated, lower one.

    The semantic leg asks Qdrant for `limit * _OVERFETCH_FACTOR` by raw cosine, so a
    memory ranked below that window arrives with `raw_semantic_score` erased even
    though the collection holds its vector. `build_results` then invented a
    graph-synthetic similarity for it. Measured on the reproduction memory, the
    invention (0.5258) was lower than its real decayed similarity (0.6002), and the
    erased cosine is what made it permanently injection-ineligible under #20831.
    """
    store = _ScoringVectorStore(
        results=[("in-window", 0.90)],
        # Outside the semantic window, but the collection can still score it.
        stored={"in-window": 0.90, "below-window": 0.64},
    )
    service = _service(
        ["in-window", "below-window"],
        vector_store=store,
        falkordb_graph_search=True,
    )

    async def graph_search(**_kwargs: Any) -> GraphScoredResult:
        return GraphScoredResult(scored=[("below-window", 0.70)])

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

    by_id = {mem.id: mem for mem in results}
    recovered = by_id["below-window"]
    assert recovered.raw_semantic_score == 0.64
    assert recovered.similarity == 0.64
    # `graph_synthetic` now means what it says -- no vector to score -- so a memory
    # Qdrant can score is never labelled with it.
    assert recovered.ranking_mode == "rrf"
    # Provenance is unchanged: the semantic leg did not surface it, and saying it did
    # would misreport which leg found the memory.
    assert recovered.search_via == "graph"
    # Only the ids the semantic leg missed are rescored.
    assert store.scored_ids == [["below-window"]]


async def test_a_graph_hit_with_no_vector_stays_graph_synthetic(monkeypatch: Any) -> None:
    """The recall expander (#17104) still owns memories the vector index has not seen."""
    store = _ScoringVectorStore(results=[("in-window", 0.90)], stored={"in-window": 0.90})
    service = _service(["in-window", "unembedded"], vector_store=store, falkordb_graph_search=True)

    async def graph_search(**_kwargs: Any) -> GraphScoredResult:
        return GraphScoredResult(scored=[("unembedded", 0.80)])

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

    by_id = {mem.id: mem for mem in results}
    unembedded = by_id["unembedded"]
    assert unembedded.raw_semantic_score is None
    assert unembedded.ranking_mode == "graph_synthetic"
    assert unembedded.similarity is not None
    assert abs(unembedded.similarity - 0.72) < 1e-9


async def test_a_rescored_single_leg_hit_is_not_labelled_semantic_only(
    monkeypatch: Any,
) -> None:
    """`semantic_only` names the leg that found the memory, not the score it has.

    Rescoring merged candidates (#20858) broke the old shorthand where carrying a
    cosine implied the semantic leg surfaced it, so the label reads `qdrant_set`.
    """
    store = _ScoringVectorStore(results=[], stored={"graph-found": 0.64})
    service = _service(["graph-found"], vector_store=store, falkordb_graph_search=True)

    async def graph_search(**_kwargs: Any) -> GraphScoredResult:
        return GraphScoredResult(scored=[("graph-found", 0.70)])

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

    # Only the graph leg returned anything, so RRF never ran and the old branch
    # would have called this hit `semantic_only` on the strength of its new cosine.
    assert [mem.id for mem in results] == ["graph-found"]
    assert results[0].raw_semantic_score == 0.64
    assert results[0].search_via == "graph"
    assert results[0].ranking_mode == "nonsemantic_fallback"


async def test_a_failed_semantic_leg_is_not_given_a_second_timeout(monkeypatch: Any) -> None:
    """Rescoring must not double the worst case of a search that already failed.

    The rescore runs after the three legs, so a vector store that just spent its
    whole timeout failing would otherwise be handed another one -- and the search
    already has its graph and keyword candidates in hand.
    """

    class _FailingStore(_ScoringVectorStore):
        async def search(
            self,
            query_embedding: list[float],
            limit: int = 10,
            filters: dict[str, str] | None = None,
            timeout: float | None = None,
        ) -> list[tuple[str, float]]:
            raise TimeoutError("qdrant timed out")

    store = _FailingStore(results=[], stored={"graph-found": 0.64})
    service = _service(["graph-found"], vector_store=store, falkordb_graph_search=True)

    async def graph_search(**_kwargs: Any) -> GraphScoredResult:
        return GraphScoredResult(scored=[("graph-found", 0.80)])

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

    assert store.scored_ids == []
    # The graph leg's own answer still stands, on its synthetic cosine.
    assert [mem.id for mem in results] == ["graph-found"]
    assert results[0].ranking_mode == "graph_synthetic"


# --------------------------------------------------------------------------- #21010: undecayed axis + collapse


def test_build_results_ranks_on_undecayed_similarity_with_age_as_tiebreak() -> None:
    # "old" carries the stronger cosine but is 60 days stale; at a 30-day half-life
    # its decayed similarity (0.85 * 0.25) falls under "fresh" (0.70). Ranking on
    # the decayed value buried the better match under a newer weaker one (#21010).
    service = _service([], storage=_AgedStorage({"old": 60.0, "fresh": 0.0}))

    results = service._build_results(
        merged_ids=["fresh", "old"],
        ranking_score_map={"fresh": 0.02, "old": 0.01},
        qdrant_score_map={"old": 0.85, "fresh": 0.70},
        qdrant_set={"old", "fresh"},
        keyword_set=set(),
        graph_set=None,
        rrf_applied=False,
        project_id=None,
        memory_type=None,
        tags_all=None,
        tags_any=None,
        tags_none=None,
        half_life=30.0,
        effective_min_score=0.0,
        limit=2,
    )

    assert [mem.id for mem in results] == ["old", "fresh"]
    assert results[0].similarity is not None and results[0].similarity < 0.70


def test_build_results_uses_decayed_similarity_only_to_break_undecayed_ties() -> None:
    service = _service([], storage=_AgedStorage({"old": 60.0, "fresh": 0.0}))

    results = service._build_results(
        merged_ids=["old", "fresh"],
        ranking_score_map={"fresh": 0.01, "old": 0.02},
        qdrant_score_map={"old": 0.80, "fresh": 0.80},
        qdrant_set={"old", "fresh"},
        keyword_set=set(),
        graph_set=None,
        rrf_applied=False,
        project_id=None,
        memory_type=None,
        tags_all=None,
        tags_any=None,
        tags_none=None,
        half_life=30.0,
        effective_min_score=0.0,
        limit=2,
    )

    assert [mem.id for mem in results] == ["fresh", "old"]


def test_build_results_collapses_near_duplicates_before_the_limit_cut() -> None:
    service = _service(["a", "b", "c"])
    vectors = {"a": [1.0, 0.0], "b": [0.999, 0.04], "c": [0.0, 1.0]}

    results = service._build_results(
        merged_ids=["a", "b", "c"],
        ranking_score_map={"a": 0.03, "b": 0.02, "c": 0.01},
        qdrant_score_map={"a": 0.90, "b": 0.85, "c": 0.80},
        qdrant_set={"a", "b", "c"},
        keyword_set=set(),
        graph_set=None,
        rrf_applied=False,
        project_id=None,
        memory_type=None,
        tags_all=None,
        tags_any=None,
        tags_none=None,
        half_life=0.0,
        effective_min_score=0.0,
        limit=2,
        candidate_vectors=vectors,
    )

    # "b" folds into "a" instead of taking the second slot, so "c" is returned.
    assert [mem.id for mem in results] == ["a", "c"]
    assert results[0].collapsed_duplicates == ["b"]
    assert results[1].collapsed_duplicates is None


def test_build_results_keeps_distinct_vectors_and_hits_without_vectors() -> None:
    service = _service(["a", "b", "c"])
    # a/b sit at cosine ~0.707, far under the 0.92 fold threshold; "c" has no vector.
    vectors = {"a": [1.0, 0.0], "b": [0.7, 0.7]}

    results = service._build_results(
        merged_ids=["a", "b", "c"],
        ranking_score_map={"a": 0.03, "b": 0.02, "c": 0.01},
        qdrant_score_map={"a": 0.90, "b": 0.85, "c": 0.80},
        qdrant_set={"a", "b", "c"},
        keyword_set=set(),
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
        candidate_vectors=vectors,
    )

    assert [mem.id for mem in results] == ["a", "b", "c"]
    assert all(mem.collapsed_duplicates is None for mem in results)


class _VectorStoreWithVectors(_VectorStore):
    def __init__(
        self,
        results: list[tuple[str, float]],
        vectors: dict[str, list[float]] | Exception,
    ) -> None:
        super().__init__(results)
        self._vectors = vectors
        self.get_vectors_calls: list[list[str]] = []

    async def get_vectors(
        self, ids: list[str], *, timeout: float | None = None
    ) -> dict[str, list[float]]:
        self.get_vectors_calls.append(list(ids))
        if isinstance(self._vectors, Exception):
            raise self._vectors
        return self._vectors


@pytest.mark.asyncio
async def test_search_fetches_stored_vectors_once_and_collapses_duplicates() -> None:
    store = _VectorStoreWithVectors(
        [("a", 0.9), ("b", 0.85), ("c", 0.8)],
        {"a": [1.0, 0.0], "b": [0.999, 0.04], "c": [0.0, 1.0]},
    )
    service = _service(["a", "b", "c"], vector_store=store)

    results = await service.search(query="anything", limit=2)

    assert [mem.id for mem in results] == ["a", "c"]
    assert results[0].collapsed_duplicates == ["b"]
    assert store.get_vectors_calls == [["a", "b", "c"]]


@pytest.mark.asyncio
async def test_search_returns_uncollapsed_when_vector_fetch_fails() -> None:
    store = _VectorStoreWithVectors([("a", 0.9), ("b", 0.85)], RuntimeError("retrieve down"))
    service = _service(["a", "b"], vector_store=store)

    results = await service.search(query="anything", limit=2)

    assert [mem.id for mem in results] == ["a", "b"]
    assert all(mem.collapsed_duplicates is None for mem in results)


@pytest.mark.asyncio
async def test_search_returns_uncollapsed_when_store_cannot_serve_vectors() -> None:
    service = _service(["a", "b"], vector_results=[("a", 0.9), ("b", 0.85)])

    results = await service.search(query="anything", limit=2)

    assert [mem.id for mem in results] == ["a", "b"]
    assert all(mem.collapsed_duplicates is None for mem in results)


class _CacheThreadingKg:
    """KG service stand-in that records the rows_cache threaded into each call."""

    def __init__(self) -> None:
        self.caches: list[Any] = []

    async def search_entities_by_vector(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{"entity_key": "seed", "name": "seed", "score": 0.9, "memory_ids": []}]

    async def find_related_memory_ids(self, **kwargs: Any) -> Any:
        from gobby.memory.services.knowledge_graph.reader import RelatedMemoryTraversal

        self.caches.append(kwargs.get("rows_cache"))
        return RelatedMemoryTraversal()


@pytest.mark.asyncio
async def test_search_backfill_threads_one_expansion_cache_across_rounds() -> None:
    """F4 (#22910): backfill threads one request-scoped expansion cache.

    The vector window hides the active rows until the pool doubles, so two
    backfill rounds run. Both rounds must receive the same memo so the real
    reader reuses the invariant hop rows instead of re-issuing them, and the
    materialized result identity and order stay unchanged.
    """
    hidden = [f"h{index}" for index in range(6)]
    active = ["a1", "a2", "a3"]
    vector_store = _CountingVectorStore(
        [(mid, 0.95 - index * 0.01) for index, mid in enumerate(hidden + active)]
    )
    kg = _CacheThreadingKg()
    service = _service(
        [],
        vector_store=vector_store,
        storage=_FilteringStorage(active),
        falkordb_graph_search=True,
    )
    service.kg_service = cast(Any, kg)

    results = await service.search("query", limit=3)

    assert vector_store.calls == [6, 12]
    assert len(kg.caches) == 2
    assert kg.caches[0] is not None
    assert kg.caches[0] is kg.caches[1]
    assert [memory.id for memory in results] == ["a1", "a2", "a3"]


def test_undecay_round_trips_equal_raw_cosines_to_one_score() -> None:
    """#22910 found work: undecay must not leak division noise into the order.

    Two candidates with the same raw cosine but very different ages recover
    undecayed scores that differ only by floating-point round-trip error. That
    value is both the floor's input and the primary ordering key, so the noise
    let a genuine tie invert on wall-clock and could jitter a floor decision. The
    recovered score must be identical for an exact tie.
    """
    from datetime import UTC, datetime, timedelta

    from gobby.memory.scoring import temporal_decay, undecay

    now = datetime.now(UTC)
    half_life = 30.0
    raw = 0.9
    scores = []
    for age in (timedelta(days=1), timedelta(days=90)):
        decay = temporal_decay(now - age, half_life)
        scores.append(undecay(raw * decay, decay))

    assert scores[0] == scores[1]


def test_order_results_keeps_earlier_hit_on_equal_undecayed_scores() -> None:
    """#22910 found work: an exact undecayed tie must keep input order."""
    from gobby.memory.services._search_ranking import HitScores, order_results

    hits = ["first", "second"]
    scores = {
        "first": HitScores(1.08, 0.977, 0.5),
        "second": HitScores(1.08, 0.125, 0.5),
    }

    assert order_results(hits, lambda hit: scores[hit]) == ["first", "second"]


def test_collapse_near_duplicates_matches_pairwise_cosine_reference() -> None:
    """F3 (#22910): hoisting vector norms must not change the clustering.

    The pairwise cosine previously recomputed both sqrt norms for every pair.
    Precomputing each norm once is a pure hoist, so the folded set must be
    identical to a reference that recomputes the norms exactly as before.
    """
    import math

    from gobby.memory.services._search_results import (
        _cosine,
        collapse_near_duplicates,
    )

    def ref_cosine(left: list[float], right: list[float]) -> float:
        dot = sum(a * b for a, b in zip(left, right, strict=False))
        norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
        return dot / norm if norm else 0.0

    def ref_collapse(
        ordered: list[Memory], vectors: dict[str, list[float]], threshold: float
    ) -> list[Memory]:
        kept: list[Memory] = []
        for mem in ordered:
            vector = vectors.get(mem.id)
            representative: Memory | None = None
            if vector is not None:
                for candidate in kept:
                    candidate_vector = vectors.get(candidate.id)
                    if candidate_vector is None:
                        continue
                    if ref_cosine(vector, candidate_vector) >= threshold:
                        representative = candidate
                        break
            if representative is None:
                kept.append(mem)
                continue
            if representative.collapsed_duplicates is None:
                representative.collapsed_duplicates = []
            representative.collapsed_duplicates.append(mem.id)
        return kept

    def _memory(memory_id: str) -> Memory:
        memory = Memory.__new__(Memory)
        object.__setattr__(memory, "id", memory_id)
        object.__setattr__(memory, "collapsed_duplicates", None)
        return memory

    # Two tight clusters plus a singleton, built so several pairs cross 0.92.
    base_a = [1.0, 0.0, 0.0, 0.0]
    base_b = [0.0, 1.0, 0.0, 0.0]
    vectors = {
        "a1": base_a,
        "a2": [0.999, 0.001, 0.0, 0.0],
        "b1": base_b,
        "b2": [0.001, 0.999, 0.0, 0.0],
        "c1": [0.0, 0.0, 1.0, 0.0],
    }
    threshold = 0.92

    ordered = [_memory(mid) for mid in vectors]
    expected = [mem.id for mem in ref_collapse(ordered, vectors, threshold)]

    for memory in ordered:
        object.__setattr__(memory, "collapsed_duplicates", None)
    actual = [mem.id for mem in collapse_near_duplicates(ordered, vectors, threshold)]

    assert actual == expected
    # The hoisted version is bit-identical to the pairwise reference.
    for memory in ordered:
        object.__setattr__(memory, "collapsed_duplicates", None)
    actual_again = [mem.id for mem in collapse_near_duplicates(ordered, vectors, threshold)]
    assert actual_again == expected
    assert _cosine(base_a, [0.999, 0.001, 0.0, 0.0]) >= threshold

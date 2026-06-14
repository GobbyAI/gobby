"""Focused tests for SearchService materialization ranking."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from gobby.config.persistence import MemoryConfig
from gobby.memory.services.search import SearchDebugSnapshot, SearchService
from gobby.storage.memories import Memory


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

    def get_memories(self, memory_ids: list[str], project_id: str | None = None) -> list[Memory]:
        return [self._memory(memory_id) for memory_id in memory_ids]

    def get_memory(self, memory_id: str, project_id: str | None = None) -> Memory:
        return self._memory(memory_id)

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
    ) -> list[tuple[str, float]]:
        return self._results[:limit]


def _service(
    memory_ids: list[str],
    *,
    vector_results: list[tuple[str, float]] | None = None,
    keyword_search: Callable[[str, int, str | None], list[tuple[str, float]]] | None = None,
    search_debug_sink: Callable[[SearchDebugSnapshot], None] | None = None,
    falkordb_graph_search: bool = False,
) -> SearchService:
    return SearchService(
        storage=_Storage(memory_ids),  # type: ignore[arg-type]
        vector_store=_VectorStore(vector_results or []),  # type: ignore[arg-type]
        embed_fn=lambda text, is_query=False: [1.0, 0.0],
        kg_service=object() if falkordb_graph_search else None,  # type: ignore[arg-type]
        keyword_search=keyword_search or (lambda query, limit, project_id: []),
        config=MemoryConfig(),
        falkordb_graph_search=falkordb_graph_search,
        falkordb_graph_min_score=0.0,
        rrf_k=60,
        falkordb_rrf_k=60,
        vector_store_failure_logger=lambda message, error: None,
        run_db=None,
        search_debug_sink=search_debug_sink,
    )


def test_build_results_uses_rrf_score_as_primary_when_rrf_applied() -> None:
    service = _service(["semantic", "graph"])

    results = service._build_results(
        merged_ids=["semantic", "graph"],
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

    assert [mem.id for mem in results] == ["graph", "semantic"]
    assert [mem.ranking_mode for mem in results] == ["rrf", "rrf"]
    assert results[0].search_via == "graph"


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
    service = _service(
        ["semantic"],
        vector_results=[("semantic", 0.9)],
        search_debug_sink=snapshots.append,
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
    )

    assert [mem.id for mem in results] == ["semantic"]
    assert snapshots == [
        SearchDebugSnapshot(
            merged_ids=["semantic"],
            returned_ids=["semantic"],
            ranking_score_map={"semantic": 0.9},
            rrf_applied=False,
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

    async def graph_search(**kwargs: Any) -> list[str]:
        return ["graph"]

    monkeypatch.setattr(service, "_search_graph_for_memories", graph_search)
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


def test_debug_sink_failure_does_not_change_results() -> None:
    sink_called = False

    def failing_sink(snapshot: SearchDebugSnapshot) -> None:
        nonlocal sink_called
        sink_called = True
        raise RuntimeError("diagnostic sink failed")

    service = _service(["semantic"], search_debug_sink=failing_sink)

    service._emit_search_debug(
        merged_ids=["semantic"],
        returned=[],
        ranking_score_map={},
        rrf_applied=False,
    )
    assert sink_called is True

"""Exercise real traversal and search paths with changing weighted frontiers."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, cast

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.services.knowledge_graph.reader import KnowledgeGraphReader
from gobby.memory.services.search import SearchService
from gobby.storage.memories_models import Memory, MemoryType

pytestmark = pytest.mark.unit
STAMP = "2026-09-01T00:00:00+00:00"


class GraphRows:
    """Graph boundary fake that rejects unsupported queries."""

    def __init__(self) -> None:
        self.edges: dict[str, list[dict[str, Any]]] = {}
        self.mentions: dict[str, list[str]] = {}
        self.frontiers: list[tuple[str, ...]] = []

    def edge(self, source: str, target: str, weight: float, support: int) -> None:
        self.edges.setdefault(source, []).append(
            {
                "source_key": source,
                "related_entity_key": target,
                "edge_weight": weight,
                "raw_weight": weight,
                "edge_support": support,
                "updated_at": STAMP,
            }
        )
        self.mentions.setdefault(target, [f"memory-{target}"])

    async def query(
        self, cypher: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        assert params is not None
        if "related_entity_key" in cypher:
            frontier = tuple(params["source_keys"])
            self.frontiers.append(frontier)
            return [dict(row) for key in frontier for row in self.edges.get(key, [])]
        if "m.memory_id IN $memory_ids" in cypher:
            return [
                {"entity_key": key, "memory_id": mid}
                for key in params["entity_keys"]
                for mid in self.mentions.get(key, [])
                if mid in params["memory_ids"]
            ]
        if "ORDER BY updated_at DESC LIMIT $limit" in cypher:
            memory_ids = list(
                dict.fromkeys(
                    mid for key in params["entity_keys"] for mid in self.mentions.get(key, [])
                )
            )
            return [
                {"memory_id": mid, "updated_at": STAMP} for mid in memory_ids[: params["limit"]]
            ]
        raise AssertionError(f"Unexpected graph query: {cypher}")


def weighted_tree() -> GraphRows:
    graph = GraphRows()
    for first in range(8):
        key = f"first-{first}"
        graph.edge("seed", key, 0.95 - first * 0.04, first + 1)
        for second in range(4):
            child = f"second-{first}-{second}"
            graph.edge(key, child, 0.85 - second * 0.07, second + 2)
            graph.edge(child, f"third-{first}-{second}", 0.75, 3)
    graph.edge("first-0", "shared", 0.3, 1)
    graph.edge("first-7", "shared", 0.9, 7)
    graph.edge("extra", "extra-first", 0.96, 6)
    graph.edge("extra-first", "extra-second", 0.95, 5)
    graph.edge("extra-second", "extra-third", 0.94, 4)
    return graph


def reader_for(graph: GraphRows) -> KnowledgeGraphReader:
    return KnowledgeGraphReader(cast(Any, graph), None, embedding_dim=4)


@pytest.mark.parametrize("max_hops", [2, 3])
async def test_changed_multihop_frontiers_preserve_admission_and_components(max_hops: int) -> None:
    cached_graph, reference_graph = weighted_tree(), weighted_tree()
    cached, reference = reader_for(cached_graph), reader_for(reference_graph)
    memo: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    admitted_sizes: list[int] = []

    for limit in (2, 4, 16, 32):
        seeds = ["seed", "extra"] if limit == 32 else ["seed"]
        actual = await cached._find_related_entity_keys(
            seeds, max_hops, limit, "project", True, memo
        )
        expected = await reference._find_related_entity_keys(
            seeds, max_hops, limit, "project", True
        )
        assert actual == expected
        keys, components, scores = actual
        assert len(keys) == len(set(keys))
        assert set(keys) == set(components) == set(scores)
        admitted_sizes.append(len(keys))
        actual_memories = await cached.find_related_memory_ids(
            seeds, max_hops, limit, "project", True, rows_cache=memo
        )
        expected_memories = await reference.find_related_memory_ids(
            seeds, max_hops, limit, "project", True
        )
        assert actual_memories == expected_memories
        assert len(actual_memories.memory_ids) == limit
        assert set(actual_memories.component_map) == set(actual_memories.memory_ids)
        if limit >= 16:
            assert components["shared"]["edge_weight_blend"] == 0.9
            assert scores["shared"] == pytest.approx((0.95 - 7 * 0.04) * 0.9)

    assert admitted_sizes[:2] == [8, 16]
    assert admitted_sizes[3] > admitted_sizes[2] > admitted_sizes[1]
    assert Counter(cached_graph.frontiers)[("seed",)] == 1
    assert Counter(reference_graph.frontiers)[("seed",)] == 6
    assert Counter(cached_graph.frontiers)[("seed", "extra")] == 1
    if max_hops == 3:
        third_hops = [
            keys
            for keys in cached_graph.frontiers
            if any(key.startswith("second-") for key in keys)
        ]
        assert len(third_hops) == 2
        assert third_hops[0] != third_hops[1]


class ActiveStorage:
    def __init__(self, active_ids: list[str]) -> None:
        self.active_ids = set(active_ids)

    def get_memory(self, memory_id: str, scope: Any = None) -> Memory:
        if memory_id not in self.active_ids:
            raise ValueError(memory_id)
        return Memory(
            id=memory_id,
            memory_type=MemoryType.FACT,
            content=memory_id,
            created_at=datetime.fromisoformat(STAMP),
            updated_at=datetime.fromisoformat(STAMP),
            source_type="agent",
            tags=[],
        )

    def get_memories(self, memory_ids: list[str], scope: Any = None) -> list[Memory]:
        return [self.get_memory(mid, scope) for mid in memory_ids if mid in self.active_ids]


class VectorWindow:
    def __init__(self) -> None:
        self.limits: list[int] = []

    async def search(
        self, _embedding: list[float], *, limit: int, **_kwargs: Any
    ) -> list[tuple[str, float]]:
        self.limits.append(limit)
        return [(f"hidden-{index}", 0.95) for index in range(min(limit, 40))]

    async def score_ids(
        self, _embedding: list[float], _ids: list[str], **_kwargs: Any
    ) -> dict[str, float]:
        return {}

    async def get_vectors(self, _ids: list[str], **_kwargs: Any) -> dict[str, list[float]]:
        return {}


class ChangingSeeds:
    def __init__(self, graph: GraphRows, *, cache: bool, stable: bool = False) -> None:
        self.reader = reader_for(graph)
        self.cache = cache
        self.stable = stable
        self.traversals: list[Any] = []

    async def search_entities_by_vector(
        self, *, limit: int, **_kwargs: Any
    ) -> list[dict[str, Any]]:
        seeds = (
            ["a", "b", "c"]
            if self.stable
            else (["a"] if limit <= 6 else (["a", "b"] if limit <= 12 else ["a", "b", "c"]))
        )
        return [{"entity_key": key, "score": 0.9, "memory_ids": []} for key in seeds]

    async def find_related_memory_ids(self, **kwargs: Any) -> Any:
        if not self.cache:
            kwargs.pop("rows_cache", None)
        result = await self.reader.find_related_memory_ids(**kwargs)
        self.traversals.append(result)
        return result


def search_fixture(
    *, cache: bool, stable: bool = False
) -> tuple[SearchService, GraphRows, ChangingSeeds, VectorWindow]:
    graph = GraphRows()
    for source, target, weight, support in [
        ("a", "first", 0.8, 2),
        ("b", "second", 0.7, 3),
        ("c", "third", 0.6, 4),
    ]:
        graph.edge(source, target, weight, support)
    if stable:
        graph.mentions["first"] = [f"blocked-{index}" for index in range(20)] + ["memory-first"]
    kg, vectors = ChangingSeeds(graph, cache=cache, stable=stable), VectorWindow()

    async def embed(_text: str, **_kwargs: Any) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0]

    service = SearchService(
        storage=cast(
            Any, ActiveStorage(["memory-first", "memory-second", "memory-third", "memory-new"])
        ),
        vector_store=cast(Any, vectors),
        embed_fn=embed,
        kg_service=cast(Any, kg),
        keyword_search=lambda _query, _limit, _project, **_kwargs: [],
        config=MemoryConfig(temporal_decay_half_life_days=0),
        falkordb_graph_search=True,
        falkordb_graph_min_score=0.0,
        rrf_k=60,
        falkordb_rrf_k=60,
        vector_store_failure_logger=lambda _message, _error: None,
    )
    return service, graph, kg, vectors


async def test_full_backfill_changed_seeds_preserve_results_ranking_components_and_stop() -> None:
    cached, graph, kg, vectors = search_fixture(cache=True)
    reference, reference_graph, reference_kg, reference_vectors = search_fixture(cache=False)
    actual = await cached.search("short title", project_id="project", limit=3)
    expected = await reference.search("short title", project_id="project", limit=3)
    assert actual == expected
    assert [memory.id for memory in actual] == ["memory-first", "memory-second", "memory-third"]
    assert vectors.limits == reference_vectors.limits == [6, 12, 24]
    assert kg.traversals == reference_kg.traversals
    assert graph.frontiers == reference_graph.frontiers == [("a",), ("a", "b"), ("a", "b", "c")]
    assert all(memory.search_via == "graph" for memory in actual)
    assert all(memory.ranking_mode == "graph_synthetic" for memory in actual)


async def test_full_backfill_stable_seeds_reuse_rows_without_changing_results_or_stop() -> None:
    cached, graph, kg, vectors = search_fixture(cache=True, stable=True)
    reference, reference_graph, reference_kg, reference_vectors = search_fixture(
        cache=False, stable=True
    )
    actual = await cached.search("short title", project_id="project", limit=3)
    expected = await reference.search("short title", project_id="project", limit=3)
    assert actual == expected
    assert [memory.id for memory in actual] == ["memory-first", "memory-second", "memory-third"]
    assert vectors.limits == reference_vectors.limits == [6, 12, 24]
    assert kg.traversals == reference_kg.traversals
    assert graph.frontiers == [("a", "b", "c")]
    assert reference_graph.frontiers == [("a", "b", "c")] * 3


async def test_next_search_sees_changed_edges_and_mentions_with_same_frontier() -> None:
    service, graph, kg, vectors = search_fixture(cache=True)
    first = await service.search("short title", project_id="project", limit=1)
    assert [memory.id for memory in first] == ["memory-first"]
    assert kg.traversals[-1].component_map["memory-first"]["edge_weight_blend"] == 0.8
    graph.edges["a"] = []
    graph.edge("a", "new", 0.5, 6)
    second = await service.search("short title", project_id="project", limit=1)
    assert [memory.id for memory in second] == ["memory-new"]
    assert kg.traversals[-1].component_map["memory-new"]["edge_weight_blend"] == 0.5
    assert (
        kg.traversals[-1].component_map["memory-new"]["edge_support_norm"]
        != kg.traversals[0].component_map["memory-first"]["edge_support_norm"]
    )
    assert graph.frontiers == [("a",), ("a",)]
    assert vectors.limits == [2, 2]

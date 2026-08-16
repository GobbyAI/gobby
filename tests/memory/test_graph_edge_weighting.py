"""Offline unit tests for derived traversal-support edges and edge weighting.

These exercise edge math and Cypher shape -- not vector storage -- so they use a
recording fake FalkorDB and deterministic embeddings (no Qdrant). The weighted
traversal itself is covered end-to-end by the integration recall benchmark.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.persistence import MemoryKnowledgeGraphConfig
from gobby.mcp_proxy import semantic_search as semantic_search_mod
from gobby.memory.falkor_client import FalkorClient
from gobby.memory.services.knowledge_graph import service as service_mod
from gobby.memory.services.knowledge_graph import writer as writer_mod
from gobby.memory.services.knowledge_graph.clustering import ClusterRunResult
from gobby.memory.services.knowledge_graph.reader import (
    _STRUCTURAL_RELATIONSHIP_TYPES,
    KnowledgeGraphReader,
    _edge_timestamp_to_iso,
)
from gobby.memory.services.knowledge_graph.writer import (
    COOCCUR_ALPHA,
    COOCCUR_SUPPORT_CAP,
    KnowledgeGraphWriter,
    cooccurrence_weight,
)
from gobby.search import similarity as similarity_mod
from gobby.search.backends import embedding as embedding_mod

pytestmark = pytest.mark.unit

Responder = Callable[[str, dict[str, Any] | None], list[dict[str, Any]]] | None


class RecordingFalkor:
    """Minimal async FalkorDB stand-in that records queries and routes responses."""

    def __init__(self, responder: Responder = None) -> None:
        self.queries: list[tuple[str, dict[str, Any] | None]] = []
        self._responder = responder

    async def query(
        self, cypher: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.queries.append((cypher, params))
        if self._responder is not None:
            return self._responder(cypher, params)
        return []

    def find(self, needle: str) -> list[tuple[str, dict[str, Any] | None]]:
        return [(c, p) for c, p in self.queries if needle in c]


class _StubFalkorDB:
    """No-op falkordb.asyncio.FalkorDB stub; query() is mocked per-test."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def select_graph(self, name: str) -> Any:
        return None


def _falkor_client(monkeypatch: pytest.MonkeyPatch) -> FalkorClient:
    """Construct a real FalkorClient backed by a fake falkordb module."""
    fake_package = types.ModuleType("falkordb")
    fake_asyncio = types.ModuleType("falkordb.asyncio")
    fake_asyncio.__dict__["FalkorDB"] = _StubFalkorDB
    monkeypatch.setitem(sys.modules, "falkordb", fake_package)
    monkeypatch.setitem(sys.modules, "falkordb.asyncio", fake_asyncio)

    return FalkorClient(host="127.0.0.1", port=16379, password="secret")


# --------------------------------------------------------------------------- #
# Pure weight math                                                            #
# --------------------------------------------------------------------------- #


def test_cooccurrence_weight_blends_cosine_and_saturating_support() -> None:
    # cosine=1.0, support=2, cap=5 -> 0.5*1.0 + 0.5*(2/5) = 0.7
    weight = cooccurrence_weight(1.0, 2, alpha=0.5, cap=5)
    assert weight == pytest.approx(0.7)


def test_cooccurrence_weight_clamps_negative_cosine_to_zero() -> None:
    # Negative similarity must not act as a positive edge weight.
    weight = cooccurrence_weight(-0.8, 0, alpha=0.5, cap=5)
    assert weight == pytest.approx(0.0)


def test_cooccurrence_weight_support_saturates_at_cap() -> None:
    at_cap = cooccurrence_weight(0.0, 5, alpha=0.5, cap=5)
    above_cap = cooccurrence_weight(0.0, 50, alpha=0.5, cap=5)
    assert at_cap == pytest.approx(0.5)
    assert above_cap == pytest.approx(0.5)


def test_cooccurrence_weight_is_bounded_unit_interval() -> None:
    weight = cooccurrence_weight(1.0, 100, alpha=COOCCUR_ALPHA, cap=COOCCUR_SUPPORT_CAP)
    assert 0.0 <= weight <= 1.0


def test_cosine_similarity_is_shared_implementation_not_reimplemented() -> None:
    assert embedding_mod.__dict__["_cosine_similarity"] is similarity_mod.cosine_similarity
    assert writer_mod.__dict__["_cosine_similarity"] is similarity_mod.cosine_similarity
    assert service_mod.__dict__["_cosine_similarity"] is similarity_mod.cosine_similarity
    assert semantic_search_mod.__dict__["_cosine_similarity"] is similarity_mod.cosine_similarity


# --------------------------------------------------------------------------- #
# FalkorClient.merge_relationship default-off boundary                        #
# --------------------------------------------------------------------------- #


async def test_merge_relationship_unweighted_keeps_prior_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _falkor_client(monkeypatch)
    query = AsyncMock(return_value=[])
    monkeypatch.setattr(client, "query", query)

    await client.merge_relationship("a", "b", "RELATED", properties={"foo": "bar"})

    cypher, params = query.call_args.args
    assert "ON CREATE SET r += $props " in cypher
    assert "ON MATCH SET r += $props " in cypher
    # No managed metadata when weight is absent.
    assert "r.count" not in cypher
    assert "r.updated_at" not in cypher
    # Extra caller props still survive via r += $props.
    assert params["props"] == {"foo": "bar"}


async def test_merge_relationship_weighted_upserts_weight_count_and_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _falkor_client(monkeypatch)
    query = AsyncMock(return_value=[])
    monkeypatch.setattr(client, "query", query)

    await client.merge_relationship("a", "b", "RELATED", properties={"weight": 0.7, "foo": "bar"})

    cypher, params = query.call_args.args
    assert "ON CREATE SET r += $props, r.count = 1, r.updated_at = timestamp()" in cypher
    # A pre-existing edge with no count coalesces to 0 then increments to 1 (no error).
    assert "r.count = coalesce(r.count, 0) + 1" in cypher
    assert "r.updated_at = timestamp()" in cypher
    # weight rides in via $props (r += $props), and extra props are preserved.
    assert params["props"]["weight"] == pytest.approx(0.7)
    assert params["props"]["foo"] == "bar"


# --------------------------------------------------------------------------- #
# Writer co-occurrence edges                                                  #
# --------------------------------------------------------------------------- #


def _support_responder(support: int) -> Responder:
    def respond(cypher: str, params: dict[str, Any] | None) -> list[dict[str, Any]]:
        if "count(DISTINCT" in cypher:
            rows = params["pairs"] if params else []
            return [{"a": p["a"], "b": p["b"], "support": support} for p in rows]
        return []

    return respond


async def test_merge_cooccurrence_edges_writes_canonical_weighted_edge() -> None:
    falkor = RecordingFalkor(_support_responder(2))
    writer = KnowledgeGraphWriter(falkor)  # type: ignore[arg-type]
    embeddings = {"a": [1.0, 0.0], "b": [1.0, 0.0]}  # cosine = 1.0

    await writer.merge_cooccurrence_edges([("a", "b")], "proj-1", False, embeddings)

    # Support query pins the FalkorDB dialect form (scoped DISTINCT CASE count).
    support_q = falkor.find("count(DISTINCT")
    assert support_q, "expected a support-count query"
    support_cypher, support_params = support_q[0]
    assert "OPTIONAL MATCH (a)-[:MENTIONED_IN]->(m:Memory)<-[:MENTIONED_IN]-(b)" in support_cypher
    assert "count(DISTINCT CASE WHEN m IS NOT NULL AND" in support_cypher
    assert support_params is not None
    assert support_params["pairs"] == [{"a": "a", "b": "b"}]

    # Write query merges a canonical directed edge with support + blended weight.
    write_q = falkor.find("MERGE (a)-[r:CO_OCCURS]->(b)")
    assert write_q, "expected a CO_OCCURS write query"
    write_cypher, write_params = write_q[0]
    assert "SET r.support = p.support, r.weight = p.weight" in write_cypher
    assert write_params is not None
    row = write_params["rows"][0]
    assert (row["a"], row["b"]) == ("a", "b")  # canonical a < b
    assert row["support"] == 2
    assert row["weight"] == pytest.approx(0.7)  # 0.5*1.0 + 0.5*(2/5)


async def test_merge_cooccurrence_edges_idempotent_support_no_inflation() -> None:
    embeddings = {"a": [1.0, 0.0], "b": [1.0, 0.0]}

    weights: list[float] = []
    supports: list[int] = []
    for _ in range(2):
        falkor = RecordingFalkor(_support_responder(2))
        writer = KnowledgeGraphWriter(falkor)  # type: ignore[arg-type]
        await writer.merge_cooccurrence_edges([("a", "b")], "proj-1", False, embeddings)
        row = falkor.find("MERGE (a)-[r:CO_OCCURS]->(b)")[0][1]["rows"][0]  # type: ignore[index]
        weights.append(row["weight"])
        supports.append(row["support"])

    # Re-run reads the same support and recomputes the same weight (SET, not +=).
    assert supports == [2, 2]
    assert weights[0] == pytest.approx(weights[1])


async def test_merge_cooccurrence_edges_unweighted_omits_weight_property() -> None:
    # Densification-only arm: edges materialized with support but no weight, so
    # traversal coalesces to the neutral 1.0 (isolates densification from weighting).
    falkor = RecordingFalkor(_support_responder(2))
    writer = KnowledgeGraphWriter(falkor)  # type: ignore[arg-type]

    await writer.merge_cooccurrence_edges([("a", "b")], "proj-1", False, {}, weighted=False)

    write_cypher, write_params = falkor.find("MERGE (a)-[r:CO_OCCURS]->(b)")[0]
    assert "SET r.support = p.support, r.updated_at = timestamp()" in write_cypher
    assert "r.weight" not in write_cypher
    assert write_params is not None
    assert "weight" not in write_params["rows"][0]


async def test_merge_cooccurrence_edges_zero_support_deletes_and_skips_write() -> None:
    falkor = RecordingFalkor(_support_responder(0))
    writer = KnowledgeGraphWriter(falkor)  # type: ignore[arg-type]

    await writer.merge_cooccurrence_edges([("a", "b")], "proj-1", False, {"a": [1.0], "b": [1.0]})

    # Zero in-scope shared memory -> no edge written...
    assert not falkor.find("MERGE (a)-[r:CO_OCCURS]->(b)")
    # ...and any stale edge is cleaned up via the same scoped entity match.
    delete_q = falkor.find("DELETE r")
    assert delete_q, "expected a zero-support cleanup delete"
    delete_cypher, delete_params = delete_q[0]
    assert "[r:CO_OCCURS]" in delete_cypher
    assert "a.project_id" in delete_cypher and "b.project_id" in delete_cypher
    assert delete_params is not None
    assert delete_params["rows"] == [{"a": "a", "b": "b"}]


# --------------------------------------------------------------------------- #
# Cleanup exclusion / traversal inclusion                                     #
# --------------------------------------------------------------------------- #


async def test_fetch_existing_relations_uses_index_anchored_directional_queries() -> None:
    falkor = RecordingFalkor()
    writer = KnowledgeGraphWriter(falkor)  # type: ignore[arg-type]

    await writer.fetch_existing_relations(["a", "b"])

    outbound = (
        "UNWIND $keys AS key "
        "MATCH (a:_Entity {entity_key: key}) "
        "MATCH (a)-[r]->(b:_Entity) "
        "WHERE type(r) <> 'CO_OCCURS' "
        "RETURN a.name AS source, type(r) AS rel_type, b.name AS target"
    )
    inbound = (
        "UNWIND $keys AS key "
        "MATCH (b:_Entity {entity_key: key}) "
        "MATCH (a:_Entity)-[r]->(b) "
        "WHERE type(r) <> 'CO_OCCURS' "
        "RETURN a.name AS source, type(r) AS rel_type, b.name AS target"
    )
    assert falkor.queries == [
        (outbound, {"keys": ["a", "b"]}),
        (inbound, {"keys": ["a", "b"]}),
    ]


async def test_fetch_existing_relations_empty_keys_skips_queries() -> None:
    falkor = RecordingFalkor()
    writer = KnowledgeGraphWriter(falkor)  # type: ignore[arg-type]

    relations = await writer.fetch_existing_relations([])

    assert relations == []
    assert falkor.queries == []


async def test_fetch_existing_relations_deduplicates_overlap_in_first_seen_order() -> None:
    duplicate = {"source": "A", "rel_type": "USES", "target": "B"}
    outbound_only = {"source": "A", "rel_type": "LIKES", "target": "C"}
    inbound_only = {"source": "D", "rel_type": "KNOWS", "target": "A"}

    def responder(cypher: str, _params: dict[str, Any] | None) -> list[dict[str, Any]]:
        if "MATCH (a:_Entity {entity_key: key})" in cypher:
            return [duplicate, outbound_only]
        return [duplicate, inbound_only]

    writer = KnowledgeGraphWriter(RecordingFalkor(responder))  # type: ignore[arg-type]

    relations = await writer.fetch_existing_relations(["a", "b"])

    assert relations == [
        {"source": "A", "relationship": "USES", "destination": "B"},
        {"source": "A", "relationship": "LIKES", "destination": "C"},
        {"source": "D", "relationship": "KNOWS", "destination": "A"},
    ]


@pytest.mark.parametrize("failing_query", [1, 2])
async def test_fetch_existing_relations_propagates_directional_failures(
    failing_query: int,
) -> None:
    query_count = 0

    def responder(_cypher: str, _params: dict[str, Any] | None) -> list[dict[str, Any]]:
        nonlocal query_count
        query_count += 1
        if query_count == failing_query:
            raise TimeoutError(f"query {failing_query} timed out")
        return []

    writer = KnowledgeGraphWriter(RecordingFalkor(responder))  # type: ignore[arg-type]

    with pytest.raises(TimeoutError, match=f"query {failing_query} timed out"):
        await writer.fetch_existing_relations(["a"])


def test_cooccurrence_is_traversable_not_structural() -> None:
    # CO_OCCURS must remain traversable: it is NOT in the excluded structural set.
    assert "CO_OCCURS" not in _STRUCTURAL_RELATIONSHIP_TYPES
    assert _STRUCTURAL_RELATIONSHIP_TYPES == ("MENTIONED_IN", "RELATES_TO_CODE")


def test_service_wires_cluster_recall_flags_to_reader() -> None:
    service = service_mod.KnowledgeGraphService(
        falkor_client=RecordingFalkor(),  # type: ignore[arg-type]
        embed_fn=None,
        prompt_loader=MagicMock(),
        llm_service=MagicMock(),
        feature_config=MemoryKnowledgeGraphConfig(),
        cluster_recall_expansion=True,
        cluster_expansion_per_entity=9,
    )

    assert service._reader._cluster_recall_expansion is True
    assert service._reader._cluster_expansion_per_entity == 9


async def test_service_recluster_uses_configured_density_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_recluster_project_entities(
        reader: Any,
        writer: Any,
        project_id: str | None,
        *,
        is_global: bool,
        min_cluster_size: int,
        min_samples: int | None,
    ) -> ClusterRunResult:
        captured["reader"] = reader
        captured["writer"] = writer
        captured["project_id"] = project_id
        captured["is_global"] = is_global
        captured["min_cluster_size"] = min_cluster_size
        captured["min_samples"] = min_samples
        return ClusterRunResult(
            project_id=project_id,
            entity_count=0,
            valid_entity_count=0,
            clustered_entity_count=0,
            noise_count=0,
            invalid_count=0,
            cluster_count=0,
            cluster_ids_by_entity_key={},
            cluster_sizes={},
            invalid_entity_keys=[],
            quality_metrics={},
        )

    monkeypatch.setattr(service_mod, "recluster_project_entities", fake_recluster_project_entities)
    service = service_mod.KnowledgeGraphService(
        falkor_client=RecordingFalkor(),  # type: ignore[arg-type]
        embed_fn=None,
        prompt_loader=MagicMock(),
        llm_service=MagicMock(),
        feature_config=MemoryKnowledgeGraphConfig(),
        cluster_min_cluster_size=7,
        cluster_min_samples=None,
    )

    result = await service.recluster_entities(project_id="project-1")

    assert result.project_id == "project-1"
    assert captured["reader"] is service._reader
    assert captured["writer"] is service._writer
    assert captured["is_global"] is False
    assert captured["min_cluster_size"] == 7
    assert captured["min_samples"] is None


# --------------------------------------------------------------------------- #
# Reader weight/decay-aware traversal                                         #
# --------------------------------------------------------------------------- #


def _reader(
    falkor: RecordingFalkor,
    *,
    graph_edge_decay: bool = False,
    edge_half_life_days: float = 30.0,
    cluster_recall_expansion: bool = False,
    cluster_expansion_per_entity: int = 3,
) -> KnowledgeGraphReader:
    return KnowledgeGraphReader(
        falkor,  # type: ignore[arg-type]
        None,
        embedding_dim=8,
        graph_edge_decay=graph_edge_decay,
        edge_half_life_days=edge_half_life_days,
        cluster_recall_expansion=cluster_recall_expansion,
        cluster_expansion_per_entity=cluster_expansion_per_entity,
    )


def _neighbor_responder(rows: list[dict[str, Any]]) -> Responder:
    def respond(cypher: str, params: dict[str, Any] | None) -> list[dict[str, Any]]:
        if "related_entity_key" not in cypher:
            return []
        source_keys = list((params or {}).get("source_keys") or [])
        if not source_keys:
            return rows
        stamped: list[dict[str, Any]] = []
        for source_key in source_keys:
            for row in rows:
                stamped.append({**row, "source_key": row.get("source_key") or source_key})
        return stamped

    return respond


async def test_traversal_orders_neighbors_by_weight_in_python() -> None:
    rows = [
        {"related_entity_key": "low", "edge_weight": 0.1, "updated_at": None},
        {"related_entity_key": "high", "edge_weight": 0.9, "updated_at": None},
        {"related_entity_key": "mid", "edge_weight": 0.5, "updated_at": None},
    ]
    falkor = RecordingFalkor(_neighbor_responder(rows))
    reader = _reader(falkor)

    related_keys, components_by_key, admission_scores = await reader._find_related_entity_keys(
        ["seed"], max_hops=1, limit=20, project_id=None, include_global=True
    )

    # Ordering is by edge weight DESC in both the Cypher candidate pull and Python cap.
    assert related_keys == ["high", "mid", "low"]
    assert set(components_by_key) == {"high", "mid", "low"}
    assert admission_scores["high"] > admission_scores["mid"] > admission_scores["low"]
    neighbor_queries = falkor.find("related_entity_key")
    assert len(neighbor_queries) == 1
    neighbor_cypher, neighbor_params = neighbor_queries[0]
    assert "UNWIND $source_keys AS source_key" in neighbor_cypher
    assert "coalesce(r.weight, 1.0) AS edge_weight" in neighbor_cypher
    assert "r.weight AS raw_weight" in neighbor_cypher
    assert "r.support AS edge_support" in neighbor_cypher
    assert "r.updated_at AS updated_at" in neighbor_cypher
    assert neighbor_params is not None
    assert neighbor_params["source_keys"] == ["seed"]


async def test_traversal_unweighted_edges_use_neutral_weight() -> None:
    # Edges without a weight come back as coalesce(r.weight, 1.0) = 1.0, so the
    # traversal still returns all neighbors (lexical tie-break on equal weight).
    rows = [
        {"related_entity_key": "beta", "edge_weight": 1.0, "updated_at": None},
        {"related_entity_key": "alpha", "edge_weight": 1.0, "updated_at": None},
    ]
    falkor = RecordingFalkor(_neighbor_responder(rows))
    reader = _reader(falkor)

    related_keys, _, _ = await reader._find_related_entity_keys(
        ["seed"], max_hops=1, limit=20, project_id=None, include_global=True
    )

    assert related_keys == ["alpha", "beta"]


async def test_traversal_batches_one_neighbor_query_per_hop() -> None:
    falkor = RecordingFalkor(
        _neighbor_responder(
            [
                {
                    "source_key": "seed-a",
                    "related_entity_key": "neighbor-a",
                    "edge_weight": 0.8,
                    "updated_at": None,
                },
                {
                    "source_key": "seed-b",
                    "related_entity_key": "neighbor-b",
                    "edge_weight": 0.6,
                    "updated_at": None,
                },
            ]
        )
    )
    reader = _reader(falkor)

    related_keys, _, _ = await reader._find_related_entity_keys(
        ["seed-a", "seed-b"],
        max_hops=1,
        limit=20,
        project_id=None,
        include_global=True,
    )

    assert related_keys == ["neighbor-a", "neighbor-b"]
    neighbor_queries = falkor.find("related_entity_key")
    assert len(neighbor_queries) == 1
    assert neighbor_queries[0][1] is not None
    assert neighbor_queries[0][1]["source_keys"] == ["seed-a", "seed-b"]


async def test_find_related_memory_ids_attributes_edge_components() -> None:
    """Traversal-admitted memories carry the #17096 component breakdown (§3.2)."""

    def respond(cypher: str, params: dict[str, Any] | None) -> list[dict[str, Any]]:
        if "related_entity_key" in cypher:
            return [
                {
                    "source_key": "seed",
                    "related_entity_key": "neighbor",
                    "edge_weight": 0.7,
                    "raw_weight": 0.7,
                    "edge_support": 3,
                    "updated_at": None,
                }
            ]
        if "RETURN DISTINCT m.memory_id" in cypher:
            return [{"memory_id": "memory-1", "updated_at": 1}]
        if "RETURN e.entity_key AS entity_key, m.memory_id AS memory_id" in cypher:
            assert params is not None
            assert params["entity_keys"] == ["neighbor"]
            assert params["memory_ids"] == ["memory-1"]
            return [{"entity_key": "neighbor", "memory_id": "memory-1"}]
        return []

    reader = _reader(RecordingFalkor(respond))

    traversal = await reader.find_related_memory_ids(["seed"], max_hops=1, limit=5)

    assert traversal.memory_ids == ["memory-1"]
    components = traversal.component_map["memory-1"]
    # weight = alpha * cos01 + (1 - alpha) * support_norm with alpha=0.5, cap=5:
    # support_norm = 3/5 = 0.6, cosine = (0.7 - 0.5 * 0.6) / 0.5 = 0.8.
    assert components["edge_weight_blend"] == pytest.approx(0.7)
    assert components["edge_support_norm"] == pytest.approx(3 / COOCCUR_SUPPORT_CAP)
    assert components["edge_cosine"] == pytest.approx(0.8)
    assert components["edge_decay_factor"] == 1.0


async def test_find_related_memory_ids_unweighted_edges_skip_attribution() -> None:
    """Unweighted edges (no r.weight/r.support) yield no components and no extra query."""

    def respond(cypher: str, params: dict[str, Any] | None) -> list[dict[str, Any]]:
        if "related_entity_key" in cypher:
            return [
                {
                    "source_key": "seed",
                    "related_entity_key": "neighbor",
                    "edge_weight": 1.0,
                    "raw_weight": None,
                    "edge_support": None,
                    "updated_at": None,
                }
            ]
        if "RETURN DISTINCT m.memory_id" in cypher:
            return [{"memory_id": "memory-1", "updated_at": 1}]
        return []

    falkor = RecordingFalkor(respond)
    reader = _reader(falkor)

    traversal = await reader.find_related_memory_ids(["seed"], max_hops=1, limit=5)

    assert traversal.memory_ids == ["memory-1"]
    assert traversal.component_map == {}
    assert not falkor.find("RETURN e.entity_key AS entity_key, m.memory_id AS memory_id")


async def test_fetch_project_entity_vectors_uses_project_scope() -> None:
    def respond(cypher: str, params: dict[str, Any] | None) -> list[dict[str, Any]]:
        assert "MATCH (e:_Entity)" in cypher
        assert "e.project_id = $project_id" in cypher
        assert "RETURN e.entity_key AS entity_key" in cypher
        assert params == {"project_id": "project-1"}
        return [{"entity_key": "entity-a", "name": "Entity A", "embedding": [1.0, 0.0]}]

    reader = _reader(RecordingFalkor(respond))

    entities = await reader.fetch_project_entity_vectors(project_id="project-1")

    assert len(entities) == 1
    assert entities[0].entity_key == "entity-a"
    assert entities[0].embedding == [1.0, 0.0]


async def test_write_entity_clusters_sets_labels_and_clears_noise() -> None:
    falkor = RecordingFalkor()
    writer = KnowledgeGraphWriter(falkor)  # type: ignore[arg-type]

    counts = await writer.write_entity_clusters(
        {"entity-a": 0, "entity-b": None, "entity-c": 1},
        project_id="project-1",
        is_global=False,
    )

    assert counts == {"clustered": 2, "noise": 1}
    set_cypher, set_params = falkor.find("SET e.cluster_id = row.cluster_id")[0]
    assert "UNWIND $rows AS row" in set_cypher
    assert "e.project_id = $project_id" in set_cypher
    assert set_params == {
        "rows": [
            {"entity_key": "entity-a", "cluster_id": 0},
            {"entity_key": "entity-c", "cluster_id": 1},
        ],
        "project_id": "project-1",
        "is_global": False,
    }
    clear_cypher, clear_params = falkor.find("REMOVE e.cluster_id")[0]
    assert "UNWIND $entity_keys AS entity_key" in clear_cypher
    assert clear_params == {
        "entity_keys": ["entity-b"],
        "project_id": "project-1",
        "is_global": False,
    }


async def test_cluster_expansion_runs_from_seed_when_traversal_has_no_neighbors() -> None:
    def respond(cypher: str, params: dict[str, Any] | None) -> list[dict[str, Any]]:
        if "related_entity_key" in cypher:
            return []
        if "candidate.cluster_id" in cypher:
            assert params is not None
            assert params["source_keys"] == ["seed"]
            assert params["excluded_keys"] == ["seed"]
            assert params["limit"] == 2
            return [{"entity_key": "cluster-a", "cluster_id": 0}]
        if "RETURN DISTINCT m.memory_id" in cypher:
            assert params is not None
            assert params["entity_keys"] == ["cluster-a"]
            return [{"memory_id": "memory-from-cluster", "updated_at": 1}]
        return []

    reader = _reader(
        RecordingFalkor(respond),
        cluster_recall_expansion=True,
        cluster_expansion_per_entity=2,
    )

    traversal = await reader.find_related_memory_ids(
        ["seed"],
        max_hops=1,
        limit=5,
        project_id="project-1",
    )

    assert traversal.memory_ids == ["memory-from-cluster"]
    # Cluster-expansion entrants carry no admitting-edge components.
    assert traversal.component_map == {}


async def test_cluster_expansion_is_default_off_for_seed_only_traversal() -> None:
    falkor = RecordingFalkor(_neighbor_responder([]))
    reader = _reader(falkor)

    traversal = await reader.find_related_memory_ids(["seed"], max_hops=1, limit=5)

    assert traversal.memory_ids == []
    assert not falkor.find("candidate.cluster_id")


async def test_cluster_expansion_bounds_fanout_and_excludes_seed_related_and_noise() -> None:
    def respond(cypher: str, params: dict[str, Any] | None) -> list[dict[str, Any]]:
        assert "candidate.cluster_id" in cypher
        assert params is not None
        assert params["source_keys"] == ["seed", "related"]
        assert params["excluded_keys"] == ["seed", "related"]
        assert params["limit"] == 2
        return [
            {"entity_key": "seed", "cluster_id": 0},
            {"entity_key": "related", "cluster_id": 0},
            {"entity_key": "noise", "cluster_id": -1},
            {"entity_key": "candidate", "cluster_id": 0},
        ]

    reader = _reader(
        RecordingFalkor(respond),
        cluster_recall_expansion=True,
        cluster_expansion_per_entity=1,
    )

    cluster_keys = await reader._find_cluster_entity_keys(
        ["seed"],
        ["seed"],
        ["related"],
        project_id="project-1",
        include_global=True,
    )

    assert cluster_keys == ["candidate"]


def test_edge_score_applies_decay_only_when_enabled() -> None:
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    old_ms = now_ms - 60 * 86400 * 1000  # 60 days ago

    decay_off = _reader(RecordingFalkor())
    # Decay disabled -> score is the raw weight regardless of age.
    assert decay_off._edge_score(1.0, old_ms) == pytest.approx(1.0)

    decay_on = _reader(RecordingFalkor(), graph_edge_decay=True, edge_half_life_days=1.0)
    score_recent = decay_on._edge_score(1.0, now_ms)
    score_old = decay_on._edge_score(1.0, old_ms)
    # Older edges decay below newer ones during candidate selection.
    assert score_old < score_recent <= 1.0
    # A missing timestamp falls back to the neutral (undecayed) weight.
    assert decay_on._edge_score(1.0, None) == pytest.approx(1.0)


def test_edge_timestamp_to_iso_handles_epoch_ms_string_and_none() -> None:
    iso = _edge_timestamp_to_iso(0)
    assert iso is not None
    assert datetime.fromisoformat(iso).year == 1970
    assert _edge_timestamp_to_iso("2026-01-01T00:00:00+00:00") == "2026-01-01T00:00:00+00:00"
    assert _edge_timestamp_to_iso(None) is None
    assert _edge_timestamp_to_iso(object()) is None

"""Offline unit tests for derived traversal-support edges and edge weighting.

These exercise edge math and Cypher shape -- not vector storage -- so they use a
recording fake FalkorDB and deterministic embeddings (no Qdrant). The weighted
traversal itself is covered end-to-end by the integration recall benchmark.
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gobby.memory.services.knowledge_graph import service as service_mod
from gobby.memory.services.knowledge_graph import writer as writer_mod
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
from gobby.search.backends import embedding as embedding_mod

Responder = Any


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


def _falkor_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Construct a real FalkorClient backed by a fake falkordb module."""
    fake_package = types.ModuleType("falkordb")
    fake_asyncio = types.ModuleType("falkordb.asyncio")
    fake_asyncio.FalkorDB = _StubFalkorDB
    monkeypatch.setitem(sys.modules, "falkordb", fake_package)
    monkeypatch.setitem(sys.modules, "falkordb.asyncio", fake_asyncio)

    from gobby.memory.falkor_client import FalkorClient

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
    assert writer_mod._cosine_similarity is embedding_mod._cosine_similarity
    assert service_mod._cosine_similarity is embedding_mod._cosine_similarity


# --------------------------------------------------------------------------- #
# FalkorClient.merge_relationship default-off boundary                        #
# --------------------------------------------------------------------------- #


async def test_merge_relationship_unweighted_keeps_prior_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _falkor_client(monkeypatch)
    client.query = AsyncMock(return_value=[])

    await client.merge_relationship("a", "b", "RELATED", properties={"foo": "bar"})

    cypher, params = client.query.call_args.args
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
    client.query = AsyncMock(return_value=[])

    await client.merge_relationship(
        "a", "b", "RELATED", properties={"weight": 0.7, "foo": "bar"}
    )

    cypher, params = client.query.call_args.args
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

    await writer.merge_cooccurrence_edges([("a", "b")], None, embeddings)

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
        await writer.merge_cooccurrence_edges([("a", "b")], None, embeddings)
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

    await writer.merge_cooccurrence_edges([("a", "b")], None, {}, weighted=False)

    write_cypher, write_params = falkor.find("MERGE (a)-[r:CO_OCCURS]->(b)")[0]
    assert "SET r.support = p.support, r.updated_at = timestamp()" in write_cypher
    assert "r.weight" not in write_cypher
    assert write_params is not None
    assert "weight" not in write_params["rows"][0]


async def test_merge_cooccurrence_edges_zero_support_deletes_and_skips_write() -> None:
    falkor = RecordingFalkor(_support_responder(0))
    writer = KnowledgeGraphWriter(falkor)  # type: ignore[arg-type]

    await writer.merge_cooccurrence_edges([("a", "b")], None, {"a": [1.0], "b": [1.0]})

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


async def test_fetch_existing_relations_excludes_cooccurrence_from_cleanup() -> None:
    falkor = RecordingFalkor()
    writer = KnowledgeGraphWriter(falkor)  # type: ignore[arg-type]

    await writer.fetch_existing_relations(["a", "b"])

    cypher = falkor.queries[0][0]
    assert "type(r) <> 'CO_OCCURS'" in cypher


def test_cooccurrence_is_traversable_not_structural() -> None:
    # CO_OCCURS must remain traversable: it is NOT in the excluded structural set.
    assert "CO_OCCURS" not in _STRUCTURAL_RELATIONSHIP_TYPES
    assert _STRUCTURAL_RELATIONSHIP_TYPES == ("MENTIONED_IN", "RELATES_TO_CODE")


# --------------------------------------------------------------------------- #
# Reader weight/decay-aware traversal                                         #
# --------------------------------------------------------------------------- #


def _reader(
    falkor: RecordingFalkor,
    *,
    graph_edge_decay: bool = False,
    edge_half_life_days: float = 30.0,
) -> KnowledgeGraphReader:
    return KnowledgeGraphReader(
        falkor,  # type: ignore[arg-type]
        None,
        embedding_dim=8,
        graph_edge_decay=graph_edge_decay,
        edge_half_life_days=edge_half_life_days,
    )


def _neighbor_responder(rows: list[dict[str, Any]]) -> Responder:
    def respond(cypher: str, params: dict[str, Any] | None) -> list[dict[str, Any]]:
        if "related_entity_key" in cypher:
            return rows
        return []

    return respond


async def test_traversal_orders_neighbors_by_weight_in_python() -> None:
    rows = [
        {"related_entity_key": "low", "edge_weight": 0.1, "updated_at": None},
        {"related_entity_key": "high", "edge_weight": 0.9, "updated_at": None},
        {"related_entity_key": "mid", "edge_weight": 0.5, "updated_at": None},
    ]
    falkor = RecordingFalkor(_neighbor_responder(rows))
    reader = _reader(falkor)

    result = await reader._find_related_entity_keys(["seed"], max_hops=1, limit=20, project_id=None)

    # Ordering is by edge weight DESC, applied in Python (not via Cypher).
    assert result == ["high", "mid", "low"]
    neighbor_cypher = falkor.find("related_entity_key")[0][0]
    assert "coalesce(r.weight, 1.0) AS edge_weight" in neighbor_cypher
    assert "r.updated_at AS updated_at" in neighbor_cypher
    assert "ORDER BY" not in neighbor_cypher


async def test_traversal_unweighted_edges_use_neutral_weight() -> None:
    # Edges without a weight come back as coalesce(r.weight, 1.0) = 1.0, so the
    # traversal still returns all neighbors (lexical tie-break on equal weight).
    rows = [
        {"related_entity_key": "beta", "edge_weight": 1.0, "updated_at": None},
        {"related_entity_key": "alpha", "edge_weight": 1.0, "updated_at": None},
    ]
    falkor = RecordingFalkor(_neighbor_responder(rows))
    reader = _reader(falkor)

    result = await reader._find_related_entity_keys(["seed"], max_hops=1, limit=20, project_id=None)

    assert result == ["alpha", "beta"]


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

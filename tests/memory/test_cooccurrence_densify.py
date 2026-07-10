"""Offline unit tests for the non-LLM CO_OCCURS densify/backfill pass.

These exercise pair derivation, batching, and delegation to the write path --
not vector storage -- so they use a recording fake FalkorDB (no Qdrant, no LLM).
Weight math and MERGE/SET semantics are owned by
``KnowledgeGraphWriter.merge_cooccurrence_edges`` (covered in
test_graph_edge_weighting.py); densify tests assert the delegation boundary.
"""

from __future__ import annotations

from typing import Any

import pytest

from gobby.memory.services.knowledge_graph.densify import (
    CooccurrenceDensifyResult,
    densify_cooccurrence,
)
from gobby.memory.services.knowledge_graph.writer import KnowledgeGraphWriter

pytestmark = pytest.mark.unit

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


def _graph_responder(
    memories: dict[str, list[str]],
    embeddings: dict[str, list[float]],
    *,
    support: int = 2,
    edge_counts: list[int] | None = None,
) -> Responder:
    """Route densify + writer queries against a fake MENTIONED_IN structure."""
    counts = list(edge_counts or [0, 0])

    def respond(cypher: str, params: dict[str, Any] | None) -> list[dict[str, Any]]:
        if "count(r) AS edges" in cypher:
            return [{"edges": counts.pop(0) if counts else 0}]
        if "collect(DISTINCT e.entity_key) AS keys" in cypher:
            return [{"memory_id": mid, "keys": keys} for mid, keys in memories.items()]
        if "e.embedding AS embedding" in cypher:
            return [{"entity_key": k, "embedding": v} for k, v in embeddings.items()]
        if "count(DISTINCT" in cypher:
            rows = params["pairs"] if params else []
            return [{"a": p["a"], "b": p["b"], "support": support} for p in rows]
        return []

    return respond


def _merged_pairs(falkor: RecordingFalkor) -> list[tuple[str, str]]:
    """Canonical pairs actually written via the CO_OCCURS MERGE query."""
    pairs: list[tuple[str, str]] = []
    for _, params in falkor.find("MERGE (a)-[r:CO_OCCURS]->(b)"):
        assert params is not None
        pairs.extend((row["a"], row["b"]) for row in params["rows"])
    return pairs


async def _densify(
    falkor: RecordingFalkor,
    *,
    weighted: bool = True,
    batch_size: int = 200,
    max_entities_per_memory: int = 8,
    project_id: str | None = "proj-1",
) -> CooccurrenceDensifyResult:
    writer = KnowledgeGraphWriter(falkor)  # type: ignore[arg-type]
    return await densify_cooccurrence(
        falkor,  # type: ignore[arg-type]
        writer,
        project_id,
        weighted=weighted,
        batch_size=batch_size,
        max_entities_per_memory=max_entities_per_memory,
    )


async def test_derives_canonical_deduped_pairs_across_memories() -> None:
    # b/a shared by two memories -> one canonical (a, b) pair; c only with a.
    falkor = RecordingFalkor(
        _graph_responder(
            {"m1": ["b", "a"], "m2": ["a", "b", "c"]},
            {"a": [1.0, 0.0], "b": [1.0, 0.0], "c": [0.0, 1.0]},
        )
    )

    result = await _densify(falkor)

    assert sorted(_merged_pairs(falkor)) == [("a", "b"), ("a", "c"), ("b", "c")]
    assert result.memories_scanned == 2
    assert result.pairs_total == 3
    assert result.pairs_merged == 3
    assert result.pairs_skipped_no_embedding == 0


async def test_caps_entities_per_memory_on_sorted_keys() -> None:
    # 4 entities with a cap of 2: only the first two sorted keys pair up.
    falkor = RecordingFalkor(
        _graph_responder(
            {"m1": ["d", "c", "b", "a"]},
            {k: [1.0] for k in "abcd"},
        )
    )

    result = await _densify(falkor, max_entities_per_memory=2)

    assert _merged_pairs(falkor) == [("a", "b")]
    assert result.pairs_total == 1


async def test_support_and_weight_flow_through_write_path() -> None:
    # cosine=1.0, support=2 -> weight 0.5*1.0 + 0.5*(2/5) = 0.7 (write-path math).
    falkor = RecordingFalkor(
        _graph_responder({"m1": ["a", "b"]}, {"a": [1.0, 0.0], "b": [1.0, 0.0]}, support=2)
    )

    await _densify(falkor)

    write_cypher, write_params = falkor.find("MERGE (a)-[r:CO_OCCURS]->(b)")[0]
    assert "SET r.support = p.support, r.weight = p.weight" in write_cypher
    assert write_params is not None
    row = write_params["rows"][0]
    assert row["support"] == 2
    assert row["weight"] == pytest.approx(0.7)


async def test_idempotent_rerun_produces_identical_set_writes() -> None:
    rows_per_run: list[list[dict[str, Any]]] = []
    for _ in range(2):
        falkor = RecordingFalkor(
            _graph_responder({"m1": ["a", "b"]}, {"a": [1.0], "b": [1.0]}, support=3)
        )
        await _densify(falkor)
        _, params = falkor.find("MERGE (a)-[r:CO_OCCURS]->(b)")[0]
        assert params is not None
        rows_per_run.append(params["rows"])

    # Rerun re-reads support from the graph and issues the same MERGE+SET rows:
    # no duplicate edges, no count inflation.
    assert rows_per_run[0] == rows_per_run[1]


async def test_weighted_skips_pairs_missing_stored_embeddings() -> None:
    # c has no stored embedding -> (a, c) and (b, c) are skipped, (a, b) merges.
    falkor = RecordingFalkor(_graph_responder({"m1": ["a", "b", "c"]}, {"a": [1.0], "b": [1.0]}))

    result = await _densify(falkor)

    assert _merged_pairs(falkor) == [("a", "b")]
    assert result.pairs_total == 3
    assert result.pairs_merged == 1
    assert result.pairs_skipped_no_embedding == 2
    assert result.entities_with_embedding == 2


async def test_unweighted_merges_all_pairs_without_embeddings() -> None:
    falkor = RecordingFalkor(_graph_responder({"m1": ["a", "b", "c"]}, {}))

    result = await _densify(falkor, weighted=False)

    assert sorted(_merged_pairs(falkor)) == [("a", "b"), ("a", "c"), ("b", "c")]
    assert result.pairs_skipped_no_embedding == 0
    # Densification-only edges carry no weight property (neutral traversal 1.0).
    write_cypher, _ = falkor.find("MERGE (a)-[r:CO_OCCURS]->(b)")[0]
    assert "r.weight" not in write_cypher
    # No embedding fetch in unweighted mode.
    assert not falkor.find("e.embedding AS embedding")


async def test_batches_are_bounded_and_counted() -> None:
    falkor = RecordingFalkor(_graph_responder({"m1": ["a", "b", "c"]}, {k: [1.0] for k in "abc"}))

    result = await _densify(falkor, batch_size=1)

    assert result.batches == 3
    assert len(falkor.find("MERGE (a)-[r:CO_OCCURS]->(b)")) == 3


async def test_empty_graph_is_a_no_op() -> None:
    falkor = RecordingFalkor(_graph_responder({}, {}))

    result = await _densify(falkor)

    assert result.memories_scanned == 0
    assert result.pairs_total == 0
    assert result.batches == 0
    assert not falkor.find("MERGE (a)-[r:CO_OCCURS]->(b)")


async def test_reports_edge_counts_before_and_after() -> None:
    falkor = RecordingFalkor(
        _graph_responder(
            {"m1": ["a", "b"]},
            {"a": [1.0], "b": [1.0]},
            edge_counts=[0, 1],
        )
    )

    result = await _densify(falkor)

    assert result.edges_before == 0
    assert result.edges_after == 1


async def test_queries_are_project_scoped() -> None:
    falkor = RecordingFalkor(_graph_responder({"m1": ["a", "b"]}, {"a": [1.0], "b": [1.0]}))

    await _densify(falkor, project_id="proj-1")

    enum_cypher, enum_params = falkor.find("collect(DISTINCT e.entity_key)")[0]
    assert "e.project_id" in enum_cypher and "m.project_id" in enum_cypher
    assert enum_params == {"project_id": "proj-1"}
    emb_cypher, emb_params = falkor.find("e.embedding AS embedding")[0]
    assert "e.project_id" in emb_cypher
    assert emb_params == {"project_id": "proj-1"}

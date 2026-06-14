"""Integration recall benchmark for derived traversal-support edges + weighting.

This exercises REAL Cypher against an ephemeral FalkorDB graph (the FakeFalkorDB
stub cannot model weighted traversal faithfully). It measures the graph-traversal
recall the decision gate of gobby task #17096 cares about, isolating three effects
with four arms:

    baseline                -> typed edges only, no weights
    cooccurrence_unweighted -> CO_OCCURS materialized, neutral traversal weights
    cooccurrence_weighted   -> typed = cosine, CO_OCCURS = support+cosine blend
    weighted_decay          -> + edge-recency decay during candidate selection

The benchmark drives ``KnowledgeGraphService.add_to_graph`` /
``find_related_memory_ids`` directly with a stubbed (deterministic) extractor and
deterministic embeddings, rather than the full ``search_memories`` facade. That is
deliberate: ``search_memories`` folds graph IDs into an RRF merge alongside vector
and keyword recall (which are unchanged across arms), and at per-project scale
that dilutes the very graph-traversal signal the gate must detect. The spike only
changes which related memory IDs traversal produces, so measuring
``find_related_memory_ids`` isolates the effect cleanly. No Qdrant is needed
because entity vectors live in FalkorDB and the weight cosine is computed in
Python from the deterministic embeddings.

Run:
    GOBBY_TEST_FALKOR_PASSWORD=... GOBBY_TEST_PROTECT=1 \
        uv run pytest tests/memory/test_recall_benchmark.py -m integration -v -s

Connection is read from GOBBY_TEST_FALKOR_HOST (default 127.0.0.1),
GOBBY_TEST_FALKOR_PORT (default 16379), GOBBY_TEST_FALKOR_PASSWORD. The test runs
in a uniquely-named graph and clears it per arm, so it never touches gobby_kg.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pytest

from gobby.config.persistence import MemoryKnowledgeGraphConfig
from gobby.memory.services.knowledge_graph import writer as writer_mod
from gobby.memory.services.knowledge_graph.service import KnowledgeGraphService
from tests.memory._recall_corpus import (
    DIM,
    NUM_CLUSTERS,
    K,
    MemoryDef,
    _seed_keys,
    _Stub,
    _StubExtractor,
    build_corpus,
    make_embed_fn,
)

pytestmark = [pytest.mark.integration]

# --------------------------------------------------------------------------- #
# Metrics                                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class ArmMetrics:
    recall_at_k: float
    mrr: float
    cooccurs_edges: int


async def _evaluate(service: KnowledgeGraphService, corpus: list[MemoryDef]) -> tuple[float, float]:
    by_cluster: dict[int, set[str]] = {}
    for mem in corpus:
        by_cluster.setdefault(mem.cluster, set()).add(mem.memory_id)

    recalls: list[float] = []
    rrs: list[float] = []
    # Query from the real (non-distractor) memories.
    queries = [m for m in corpus if not m.memory_id.endswith("_noise")]
    for mem in queries:
        expected = {mid for mid in by_cluster[mem.cluster] if mid != mem.memory_id}
        expected.discard(f"mem_c{mem.cluster}_noise")
        if not expected:
            continue
        result = await service.find_related_memory_ids(
            _seed_keys(mem), max_hops=2, limit=K, project_id=None
        )
        ranked = [mid for mid in result if mid != mem.memory_id]
        topk = set(ranked[:K])
        recalls.append(len(topk & expected) / len(expected))
        rr = 0.0
        for rank, mid in enumerate(ranked, start=1):
            if mid in expected:
                rr = 1.0 / rank
                break
        rrs.append(rr)

    mean = lambda xs: sum(xs) / len(xs) if xs else 0.0  # noqa: E731
    return mean(recalls), mean(rrs)


async def _run_arm(
    client: Any,
    corpus: list[MemoryDef],
    *,
    graph_edge_weighting: bool,
    materialize_cooccurrence: bool,
    graph_edge_decay: bool,
    edge_half_life_days: float = 30.0,
) -> ArmMetrics:
    await client.query("MATCH (n) DETACH DELETE n")

    by_content = {mem.memory_id: mem for mem in corpus}
    service = KnowledgeGraphService(
        falkor_client=client,
        embed_fn=make_embed_fn(DIM),
        prompt_loader=_Stub(),
        llm_service=_Stub(),
        feature_config=MemoryKnowledgeGraphConfig(),
        vector_store=None,
        embedding_dim=DIM,
        graph_edge_weighting=graph_edge_weighting,
        materialize_cooccurrence=materialize_cooccurrence,
        graph_edge_decay=graph_edge_decay,
        edge_half_life_days=edge_half_life_days,
    )
    service._extractor = _StubExtractor(by_content)  # type: ignore[assignment]

    for mem in corpus:
        result = await service.add_to_graph(
            content=mem.memory_id, memory_id=mem.memory_id, project_id=None
        )
        assert result.status.value in {"success", "partial_failure"}, result

    rows = await client.query("MATCH ()-[r:CO_OCCURS]->() RETURN count(r) AS n")
    cooccurs = int(rows[0]["n"]) if rows else 0

    recall, mrr = await _evaluate(service, corpus)
    return ArmMetrics(recall_at_k=recall, mrr=mrr, cooccurs_edges=cooccurs)


# --------------------------------------------------------------------------- #
# Benchmark                                                                   #
# --------------------------------------------------------------------------- #


async def test_recall_benchmark_arms(monkeypatch: pytest.MonkeyPatch) -> None:
    host = os.environ.get("GOBBY_TEST_FALKOR_HOST", "127.0.0.1")
    port = int(os.environ.get("GOBBY_TEST_FALKOR_PORT", "16379"))
    password = os.environ.get("GOBBY_TEST_FALKOR_PASSWORD")

    from gobby.memory.falkor_client import FalkorClient

    graph_name = f"test_recall_benchmark_{os.getpid()}"
    try:
        client = FalkorClient(host=host, port=port, password=password, graph_name=graph_name)
    except Exception as exc:
        pytest.skip(f"FalkorDB not reachable for integration benchmark: {exc}")
    try:
        if not await client.ping():
            pytest.skip("FalkorDB not reachable for integration benchmark")
        # Pin the support-query dialect early: a DISTINCT CASE count failure must
        # surface here, before the rest of the benchmark depends on it.
        await client.query("MATCH (n) DETACH DELETE n")
        await client.query(
            "MERGE (a:_Entity {entity_key: 'g:_|n:a'}) "
            "MERGE (b:_Entity {entity_key: 'g:_|n:b'}) "
            "MERGE (m:Memory {memory_id: 'sm'}) "
            "MERGE (a)-[:MENTIONED_IN]->(m) MERGE (b)-[:MENTIONED_IN]->(m)"
        )
        dialect = await client.query(
            "MATCH (a:_Entity {entity_key: 'g:_|n:a'}), (b:_Entity {entity_key: 'g:_|n:b'}) "
            "OPTIONAL MATCH (a)-[:MENTIONED_IN]->(m:Memory)<-[:MENTIONED_IN]-(b) "
            "RETURN count(DISTINCT CASE WHEN m IS NOT NULL THEN m END) AS support"
        )
        assert int(dialect[0]["support"]) == 1, dialect
        await client.query("MATCH (n) DETACH DELETE n")

        corpus = build_corpus()

        baseline = await _run_arm(
            client,
            corpus,
            graph_edge_weighting=False,
            materialize_cooccurrence=False,
            graph_edge_decay=False,
        )
        cooc_unweighted = await _run_arm(
            client,
            corpus,
            graph_edge_weighting=False,
            materialize_cooccurrence=True,
            graph_edge_decay=False,
        )

        # Sweep (alpha, cap) for the weighted arm; freeze winners as module constants.
        sweep: dict[tuple[float, int], ArmMetrics] = {}
        for alpha in (0.5, 0.75, 1.0):
            for cap in (3, 5, 10):
                monkeypatch.setattr(writer_mod, "COOCCUR_ALPHA", alpha)
                monkeypatch.setattr(writer_mod, "COOCCUR_SUPPORT_CAP", cap)
                sweep[(alpha, cap)] = await _run_arm(
                    client,
                    corpus,
                    graph_edge_weighting=True,
                    materialize_cooccurrence=True,
                    graph_edge_decay=False,
                )
        best_combo = max(sweep, key=lambda kc: (sweep[kc].recall_at_k, sweep[kc].mrr))
        cooc_weighted = sweep[best_combo]

        # Decay arm uses the winning (alpha, cap); a short half-life makes recency bite.
        monkeypatch.setattr(writer_mod, "COOCCUR_ALPHA", best_combo[0])
        monkeypatch.setattr(writer_mod, "COOCCUR_SUPPORT_CAP", best_combo[1])
        weighted_decay = await _run_arm(
            client,
            corpus,
            graph_edge_weighting=True,
            materialize_cooccurrence=True,
            graph_edge_decay=True,
            edge_half_life_days=7.0,
        )

        # ----------------------------------------------------------------- #
        # Report (captured with -s; record in task #17096)                  #
        # ----------------------------------------------------------------- #
        print("\n=== Recall benchmark (gobby #17096) ===")
        print(f"corpus: {len(corpus)} memories, {NUM_CLUSTERS} clusters, recall@{K}")

        def line(label: str, m: ArmMetrics) -> str:
            return (
                f"{label:<26} recall@{K}={m.recall_at_k:.3f}  "
                f"MRR={m.mrr:.3f}  CO_OCCURS={m.cooccurs_edges}"
            )

        print(line("baseline", baseline))
        print(line("cooccurrence_unweighted", cooc_unweighted))
        print(line(f"cooccurrence_weighted{best_combo}", cooc_weighted))
        print(line("weighted_decay", weighted_decay))
        print("--- weighted sweep grid (alpha, cap) ---")
        for (alpha, cap), m in sweep.items():
            print(f"  alpha={alpha:<4} cap={cap:<3} recall@{K}={m.recall_at_k:.3f} MRR={m.mrr:.3f}")

        print("--- decision gate ---")
        print(f"  densify helps:   {cooc_unweighted.recall_at_k > baseline.recall_at_k}")
        print(f"  weighting helps: {cooc_weighted.recall_at_k > cooc_unweighted.recall_at_k}")
        print(f"  decay helps:     {weighted_decay.recall_at_k > cooc_weighted.recall_at_k}")

        # ----------------------------------------------------------------- #
        # Harness assertions (the gate decision is recorded, not asserted)  #
        # ----------------------------------------------------------------- #
        for m in (baseline, cooc_unweighted, cooc_weighted, weighted_decay):
            assert 0.0 <= m.recall_at_k <= 1.0
            assert 0.0 <= m.mrr <= 1.0
        # Densification must create CO_OCCURS edges and recover cross-memory recall
        # that the typed-only baseline cannot.
        assert baseline.cooccurs_edges == 0
        assert cooc_unweighted.cooccurs_edges > 0
        assert cooc_unweighted.recall_at_k > baseline.recall_at_k
        assert cooc_weighted.recall_at_k >= cooc_unweighted.recall_at_k
    finally:
        try:
            await client.query("MATCH (n) DETACH DELETE n")
        finally:
            await client.close()

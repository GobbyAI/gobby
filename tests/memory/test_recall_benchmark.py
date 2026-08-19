"""Integration recall benchmark: synthetic traversal arms + labeled-data fit.

Two complementary evaluation sides live here (#17197):

- **Synthetic corpus arms (recall-side / false-negative eval).** The
  hub-and-spoke corpus has full ground truth, so it can measure what
  traversal *fails to retrieve*. This is the only side that can make recall
  claims; it is retained unchanged below.
- **Labeled-data fit (precision-side ONLY).** Real usefulness labels exist
  only for memories that were actually injected, so they can say "what we
  injected was (not) useful" — never what we failed to retrieve. The labeled
  arm (``_run_labeled_fit``) fits and evaluates on real labeled rows from the
  promoted hub tables via ``gobby.memory.recall_fit``: pairwise objective
  with IPS injection-position propensity weighting, never-retrieved memories
  treated as unlabeled (not negative), per-project partial-pooling split, and
  replay over the FULL ranking path (the ``SearchService`` blend ordering
  incl. temporal decay and ``ranking_mode``), not just
  ``find_related_memory_ids``.

The synthetic side exercises REAL Cypher against an ephemeral FalkorDB graph
(the FakeFalkorDB stub cannot model weighted traversal faithfully). It measures
the graph-traversal recall the decision gate of gobby task #17096 cares about,
isolating three effects with four arms:

    baseline                -> typed edges only, no weights
    cooccurrence_unweighted -> CO_OCCURS materialized, neutral traversal weights
    cooccurrence_weighted   -> typed = cosine, CO_OCCURS = support+cosine blend
    weighted_decay          -> + edge-recency decay during candidate selection
    cluster_expansion       -> stored HDBSCAN _Entity.cluster_id expansion

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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from gobby.config.persistence import MemoryKnowledgeGraphConfig
from gobby.memory.recall_fit import (
    LabeledFitReport,
    default_replay_grid,
    fit_and_evaluate,
    replay_row_from_signal_row,
    split_request_ids_per_project,
)
from gobby.memory.recall_refit import (
    GateCohort,
    build_ship_audit_sample,
    run_ship_gate_from_store,
    static_replay_params,
)
from gobby.memory.services.knowledge_graph import writer as writer_mod
from gobby.memory.services.knowledge_graph.service import KnowledgeGraphService
from gobby.memory.shadow_relevance import SHADOW_PROTOCOL_VERSION
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.recall_signals import RecallSignalStore
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
from tests.memory.recall_benchmark_cleanup import drop_recall_benchmark_graph

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = [pytest.mark.integration]

# --------------------------------------------------------------------------- #
# Metrics                                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class ArmMetrics:
    recall_at_k: float
    mrr: float
    cooccurs_edges: int
    cluster_count: int = 0
    clustered_entities: int = 0


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
            _seed_keys(mem), max_hops=2, limit=K, project_id=PERSONAL_PROJECT_ID
        )
        ranked = [mid for mid in result.memory_ids if mid != mem.memory_id]
        topk = set(ranked[:K])
        recalls.append(len(topk & expected) / len(expected))
        rr = 0.0
        for rank, mid in enumerate(ranked, start=1):
            if mid in expected:
                rr = 1.0 / rank
                break
        rrs.append(rr)

    def mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    return mean(recalls), mean(rrs)


async def _run_arm(
    client: Any,
    corpus: list[MemoryDef],
    *,
    graph_edge_weighting: bool,
    materialize_cooccurrence: bool,
    graph_edge_decay: bool,
    edge_half_life_days: float = 30.0,
    cluster_recall_expansion: bool = False,
    recluster_entities: bool = False,
    cluster_expansion_per_entity: int = 3,
    cluster_min_cluster_size: int = 5,
    cluster_min_samples: int | None = 2,
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
        cluster_recall_expansion=cluster_recall_expansion,
        cluster_expansion_per_entity=cluster_expansion_per_entity,
        cluster_min_cluster_size=cluster_min_cluster_size,
        cluster_min_samples=cluster_min_samples,
    )
    service._extractor = _StubExtractor(by_content)  # type: ignore[assignment]

    for mem in corpus:
        result = await service.add_to_graph(
            content=mem.memory_id, memory_id=mem.memory_id, project_id=PERSONAL_PROJECT_ID
        )
        assert result.status.value in {"success", "partial_failure"}, result

    cluster_count = 0
    clustered_entities = 0
    if recluster_entities:
        cluster_result = await service.recluster_entities(project_id=PERSONAL_PROJECT_ID)
        cluster_count = cluster_result.cluster_count
        clustered_entities = cluster_result.clustered_entity_count

    rows = await client.query("MATCH ()-[r:CO_OCCURS]->() RETURN count(r) AS n")
    cooccurs = int(rows[0]["n"]) if rows else 0

    recall, mrr = await _evaluate(service, corpus)
    return ArmMetrics(
        recall_at_k=recall,
        mrr=mrr,
        cooccurs_edges=cooccurs,
        cluster_count=cluster_count,
        clustered_entities=clustered_entities,
    )


# --------------------------------------------------------------------------- #
# Labeled-data fit arm (precision-side; #17197)                               #
# --------------------------------------------------------------------------- #

_LABEL_TS = "2026-07-09T12:00:00+00:00"
_LOGGED_HALF_LIFE = 30.0


def _labeled_hit(memory_id: str, *, rank: int, raw: float, decay: float) -> dict[str, object]:
    return {
        "memory_id": memory_id,
        "rank": rank,
        "search_via": "semantic",
        "similarity": raw * decay,
        "raw_semantic_score": raw,
        "temporal_decay_factor": decay,
        "ranking_score": 1.0 - rank * 0.1,
        "ranking_mode": "semantic_only",
        "graph_score": None,
        "edge_cosine": None,
        "edge_support_norm": None,
        "edge_weight_blend": None,
        "edge_decay_factor": None,
    }


def _seed_labeled_signal_rows(store: RecallSignalStore) -> None:
    """Plant real hub rows where the logged ranking inverts usefulness.

    Per request: the useful memory is recent but lower-raw-scored (0.6 * 0.9),
    the not-useful one is stale but higher-raw-scored (0.8 * 0.7 -> logged
    winner), plus one unlabeled hit that only feeds propensity denominators.
    Any replayed half-life shorter than the logged 30d flips every request,
    so the fit must beat the logged-params baseline on the holdout.
    """
    for project in ("proj-fit-a", "proj-fit-b"):
        for i in range(4):
            request_id = f"{project}-req-{i}"
            session_id = f"sess-{project}"
            store.insert_signal_event(
                {
                    "schema_version": 3,
                    "timestamp": _LABEL_TS,
                    "session_id": session_id,
                    "recall_request_id": request_id,
                    "project_id": project,
                    "caller": "memory.recall",
                    "query": f"benchmark query {i}",
                    "merged_ids": ["mem-bad", "mem-useful", "mem-unlabeled"],
                    "returned_ids": ["mem-bad", "mem-useful", "mem-unlabeled"],
                    "rrf_applied": False,
                    "graph_synthetic_similarity_discount": None,
                    "ranking_score_map": {},
                    "graph_score_map": {},
                    "weighting": {"temporal_decay_half_life_days": _LOGGED_HALF_LIFE},
                    "hits": [
                        _labeled_hit("mem-bad", rank=0, raw=0.8, decay=0.7),
                        _labeled_hit("mem-useful", rank=1, raw=0.6, decay=0.9),
                        _labeled_hit("mem-unlabeled", rank=2, raw=0.5, decay=0.8),
                    ],
                }
            )
            store.record_injection_outcomes(
                [
                    {
                        "session_id": session_id,
                        "recall_request_id": request_id,
                        "memory_id": memory_id,
                        "project_id": project,
                        "outcome": "injected",
                        "injection_position": position,
                        "injection_group": "context",
                        "caller": "memory.recall",
                    }
                    for position, memory_id in enumerate(["mem-useful", "mem-bad", "mem-unlabeled"])
                ]
            )
            for memory_id, useful in (("mem-useful", True), ("mem-bad", False)):
                store.insert_usefulness_label(
                    {
                        "session_id": session_id,
                        "recall_request_id": request_id,
                        "memory_id": memory_id,
                        "project_id": project,
                        "label_source": "ablation",
                        "judge_useful": useful,
                        "judge_protocol_version": "benchmark-ablation-v1",
                        "position_randomized": False,
                        "length_controlled": False,
                        "labeled_at": _LABEL_TS,
                    }
                )


_SHADOW_LABEL_SOURCE = "digest_shadow"
_SHADOW_JUDGE_MODEL = "benchmark-shadow-judge"
_SHADOW_JUDGE_CONFIG = "benchmark-shadow-config-v1"
_SHADOW_REGIME = "[false,false,false,false]"
_SHADOW_DATA_CUTOFF = datetime(2026, 7, 10, 12, tzinfo=UTC)
_SHADOW_COMPLETION_CUTOFF = datetime(2026, 7, 10, 13, tzinfo=UTC)


def _shadow_hit(
    memory_id: str,
    *,
    rank: int,
    raw: float,
    decay: float,
) -> dict[str, object]:
    hit = _labeled_hit(memory_id, rank=rank, raw=raw, decay=decay)
    hit["content_hash"] = f"content-{memory_id}"
    return hit


def _seed_shadow_gate_rows(store: RecallSignalStore) -> GateCohort:
    """Seed a complete fenced shadow cohort plus its bound 50-unit audit."""
    for index in range(120):
        project = f"proj-shadow-{index % 2}"
        request_id = f"shadow-request-{index:03d}"
        session_id = f"shadow-session-{index % 2}"
        hits = [
            _shadow_hit("mem-bad", rank=0, raw=0.5, decay=0.95),
            _shadow_hit("mem-useful", rank=1, raw=0.75, decay=0.6),
        ]
        store.insert_signal_event(
            {
                "schema_version": 4,
                "timestamp": _LABEL_TS,
                "session_id": session_id,
                "recall_request_id": request_id,
                "project_id": project,
                "caller": "memory.recall",
                "query": f"shadow benchmark query {index}",
                "merged_ids": ["mem-bad", "mem-useful"],
                "returned_ids": ["mem-bad", "mem-useful"],
                "rrf_applied": False,
                "graph_synthetic_similarity_discount": None,
                "ranking_score_map": {},
                "graph_score_map": {},
                "weighting": {"temporal_decay_half_life_days": _LOGGED_HALF_LIFE},
                "hits": hits,
            }
        )
        claim_token = store.claim_shadow_request(
            session_id,
            request_id,
            label_source=_SHADOW_LABEL_SOURCE,
            judge_protocol_version=SHADOW_PROTOCOL_VERSION,
        )
        assert claim_token is not None
        presented = [
            {
                "neutral_key": f"M{position + 1}",
                "memory_id": str(hit["memory_id"]),
                "order_index": position,
                "excerpt": str(hit["memory_id"]),
                "content_hash": str(hit["content_hash"]),
            }
            for position, hit in enumerate(hits)
        ]
        labels = [
            {
                "session_id": session_id,
                "recall_request_id": request_id,
                "memory_id": memory_id,
                "project_id": project,
                "label_source": _SHADOW_LABEL_SOURCE,
                "judge_useful": useful,
                "judge_confidence": 0.99,
                "judge_model": _SHADOW_JUDGE_MODEL,
                "judge_protocol_version": SHADOW_PROTOCOL_VERSION,
                "position_randomized": True,
                "length_controlled": True,
                "labeled_at": _LABEL_TS,
            }
            for memory_id, useful in (("mem-bad", False), ("mem-useful", True))
        ]
        snapshot = {
            "recall_request_id": request_id,
            "label_source": _SHADOW_LABEL_SOURCE,
            "judge_protocol_version": SHADOW_PROTOCOL_VERSION,
            "system_prompt": "benchmark shadow rubric",
            "query_text": f"shadow benchmark query {index}",
            "presented": presented,
            "prompt_hash": f"prompt-{request_id}",
            "judge_model": _SHADOW_JUDGE_MODEL,
            "judge_config_fingerprint": _SHADOW_JUDGE_CONFIG,
            "created_at": _LABEL_TS,
        }
        assert store.insert_usefulness_labels_atomic(labels, snapshot, claim_token) is True

    # Snapshot completion time is DB-owned (DEFAULT now()); backdate it so the
    # fenced-cohort cutoff queries keep selecting the seeded rows.
    store.db.execute(
        "UPDATE recall_shadow_prompt_snapshot SET created_at = %s WHERE label_source = %s",
        (datetime.fromisoformat(_LABEL_TS), _SHADOW_LABEL_SOURCE),
    )

    cohort = GateCohort(
        label_source=_SHADOW_LABEL_SOURCE,
        candidate_scope="full",
        judge_protocol_version=SHADOW_PROTOCOL_VERSION,
        weighting_regime_key=_SHADOW_REGIME,
        judge_model_key=_SHADOW_JUDGE_MODEL,
        judge_config_fingerprint=_SHADOW_JUDGE_CONFIG,
        data_cutoff=_SHADOW_DATA_CUTOFF,
        completion_cutoff=_SHADOW_COMPLETION_CUTOFF,
    )
    request_rows = store.shadow_cohort_query(
        "fitting",
        label_source=cohort.label_source,
        judge_protocol_version=cohort.judge_protocol_version,
        judge_model_key=cohort.judge_model_key,
        judge_config_fingerprint=cohort.judge_config_fingerprint,
        weighting_regime_key=cohort.weighting_regime_key,
        data_cutoff=cohort.data_cutoff,
        completion_cutoff=cohort.completion_cutoff,
        limit=1_000,
    )
    train_request_ids, _holdout_request_ids = split_request_ids_per_project(
        [
            (
                str(row["project_id"]) if row.get("project_id") is not None else None,
                str(row["recall_request_id"]),
            )
            for row in request_rows
        ]
    )
    training_rows = store.fetch_shadow_replay_rows(
        label_source=cohort.label_source,
        candidate_scope=cohort.candidate_scope,
        judge_protocol_version=cohort.judge_protocol_version,
        weighting_regime_key=cohort.weighting_regime_key,
        judge_model_key=cohort.judge_model_key,
        judge_config_fingerprint=cohort.judge_config_fingerprint,
        data_cutoff=cohort.data_cutoff,
        completion_cutoff=cohort.completion_cutoff,
        project_id=None,
        limit=1_000,
        request_ids=sorted(train_request_ids),
    )
    sample = build_ship_audit_sample(
        training_rows,
        cohort=cohort,
        train_request_ids=train_request_ids,
    )
    verdicts = [
        {
            "request_id": target.request_id,
            "memory_id": target.memory_id,
            "prompt_hash": target.prompt_hash,
            "human_verdict": target.judge_useful,
            "reviewer": "benchmark-reviewer",
            "created_at": _LABEL_TS,
        }
        for target in sample.targets
    ]
    assert len(verdicts) == 50
    assert (
        store.insert_audit_verdicts(
            verdicts,
            cohort_digest=sample.cohort_digest,
            sample_digest=sample.sample_digest,
        )
        == 50
    )
    return cohort


def _run_labeled_fit(store: RecallSignalStore, *, label_source: str) -> LabeledFitReport:
    """The labeled counterpart of ``_run_arm``: fit + holdout-eval on real rows.

    Loads every injected hit (labeled or not) for one label stream from the
    hub join, replays the full ranking path under a parameter grid, and
    returns the fitted-vs-logged-baseline comparison #17198's ship gate needs.
    """
    rows = [
        replay_row_from_signal_row(row)
        for row in store.fetch_replay_rows(label_source=label_source)
    ]
    return fit_and_evaluate(rows, default_replay_grid())


# --------------------------------------------------------------------------- #
# Benchmark                                                                   #
# --------------------------------------------------------------------------- #


async def test_recall_benchmark_arms(monkeypatch: pytest.MonkeyPatch) -> None:
    host = os.environ.get("GOBBY_TEST_FALKOR_HOST", "127.0.0.1")
    port = int(os.environ.get("GOBBY_TEST_FALKOR_PORT", "16379"))
    password = os.environ.get("GOBBY_TEST_FALKOR_PASSWORD")
    if not password:
        pytest.skip("GOBBY_TEST_FALKOR_PASSWORD is unset")

    from gobby.memory.falkor_client import FalkorClient

    graph_name = f"test_recall_benchmark_{os.getpid()}"
    client = FalkorClient(host=host, port=port, password=password, graph_name=graph_name)
    try:
        if not await client.ping():
            pytest.fail("FalkorDB not reachable for integration benchmark")
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
        cluster_expansion = await _run_arm(
            client,
            corpus,
            graph_edge_weighting=False,
            materialize_cooccurrence=False,
            graph_edge_decay=False,
            cluster_recall_expansion=True,
            recluster_entities=True,
        )

        # Sweep HDBSCAN (min_cluster_size, min_samples) on the live corpus —
        # clustering changes retrieval candidate sets, which logged-hit replay
        # cannot express, so #17198 tunes these here (judge-independent planted
        # ground truth) instead of in the offline gate. (5, 2) is the static
        # pair and reuses the cluster_expansion arm above.
        cluster_sweep: dict[tuple[int, int | None], ArmMetrics] = {(5, 2): cluster_expansion}
        for mcs, ms in ((3, 1), (8, 3)):
            cluster_sweep[(mcs, ms)] = await _run_arm(
                client,
                corpus,
                graph_edge_weighting=False,
                materialize_cooccurrence=False,
                graph_edge_decay=False,
                cluster_recall_expansion=True,
                recluster_entities=True,
                cluster_min_cluster_size=mcs,
                cluster_min_samples=ms,
            )
        best_cluster = max(
            cluster_sweep,
            key=lambda pair: (cluster_sweep[pair].recall_at_k, cluster_sweep[pair].mrr),
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
                f"MRR={m.mrr:.3f}  CO_OCCURS={m.cooccurs_edges}  "
                f"clusters={m.cluster_count}/{m.clustered_entities}"
            )

        print(line("baseline", baseline))
        print(line("cooccurrence_unweighted", cooc_unweighted))
        print(line("cluster_expansion", cluster_expansion))
        print(line(f"cooccurrence_weighted{best_combo}", cooc_weighted))
        print(line("weighted_decay", weighted_decay))
        print("--- weighted sweep grid (alpha, cap) ---")
        for (alpha, cap), m in sweep.items():
            print(f"  alpha={alpha:<4} cap={cap:<3} recall@{K}={m.recall_at_k:.3f} MRR={m.mrr:.3f}")

        print("--- cluster sweep grid (min_cluster_size, min_samples) — #17198 ---")
        for (mcs, ms), m in cluster_sweep.items():
            print(
                f"  mcs={mcs:<3} ms={ms!s:<4} recall@{K}={m.recall_at_k:.3f} "
                f"MRR={m.mrr:.3f} clusters={m.cluster_count}/{m.clustered_entities}"
            )

        print("--- decision gate ---")
        print(f"  densify helps:   {cooc_unweighted.recall_at_k > baseline.recall_at_k}")
        print(f"  cluster helps:   {cluster_expansion.recall_at_k > baseline.recall_at_k}")
        print(f"  weighting helps: {cooc_weighted.recall_at_k > cooc_unweighted.recall_at_k}")
        print(f"  decay helps:     {weighted_decay.recall_at_k > cooc_weighted.recall_at_k}")
        static_cluster = cluster_sweep[(5, 2)]
        cluster_beats_static = best_cluster != (5, 2) and (
            cluster_sweep[best_cluster].recall_at_k,
            cluster_sweep[best_cluster].mrr,
        ) > (static_cluster.recall_at_k, static_cluster.mrr)
        print(f"  cluster params beat static (5, 2): {cluster_beats_static} (best={best_cluster})")

        # ----------------------------------------------------------------- #
        # Harness assertions (the gate decision is recorded, not asserted)  #
        # ----------------------------------------------------------------- #
        for m in (baseline, cooc_unweighted, cluster_expansion, cooc_weighted, weighted_decay):
            assert 0.0 <= m.recall_at_k <= 1.0
            assert 0.0 <= m.mrr <= 1.0
        # Densification must create CO_OCCURS edges and recover cross-memory recall
        # that the typed-only baseline cannot.
        assert baseline.cooccurs_edges == 0
        assert cooc_unweighted.cooccurs_edges > 0
        assert cooc_unweighted.recall_at_k > baseline.recall_at_k
        assert cluster_expansion.cluster_count > 0
        assert cluster_expansion.clustered_entities > 0
        assert cluster_expansion.recall_at_k > baseline.recall_at_k
        assert cooc_weighted.recall_at_k >= cooc_unweighted.recall_at_k
        # Cluster-param sweep is a harness: every arm must produce sane
        # metrics; which pair wins is recorded, not asserted.
        for m in cluster_sweep.values():
            assert 0.0 <= m.recall_at_k <= 1.0
            assert 0.0 <= m.mrr <= 1.0
            assert m.cluster_count > 0
    finally:
        try:
            await drop_recall_benchmark_graph(client, graph_name)
        finally:
            await client.close()


def test_recall_benchmark_labeled_fit(temp_db: HubDatabase) -> None:
    """Precision-side benchmark: fit/eval on real labeled rows from hub tables.

    Exercises the full labeled pipeline against the REAL promoted tables
    (hits ⋈ injected outcomes ⋈ requests, LEFT JOIN ablation labels): planted
    rows where the logged ranking inverts usefulness, a per-project
    partial-pooling split, IPS position-propensity weighting with unlabeled
    rows in the denominators, and replay over the SearchService blend
    ordering (temporal decay + ranking_mode), not find_related_memory_ids.
    """
    store = RecallSignalStore(temp_db)
    _seed_labeled_signal_rows(store)

    report = _run_labeled_fit(store, label_source="ablation")

    print("\n=== Labeled recall fit (gobby #17197) ===")
    print(
        f"rows={report.rows_total} labeled={report.rows_labeled} "
        f"train_requests={report.train_requests} eval_requests={report.eval_requests}"
    )
    print(f"fitted pooled: {report.fitted.pooled}")
    print(f"project pairs: {report.fitted.project_pairs}")
    print(
        f"holdout: baseline={report.baseline_eval.accuracy:.3f} "
        f"fitted={report.fitted_eval.accuracy:.3f} "
        f"(pairs={report.fitted_eval.pair_count})"
    )

    # All seeded rows come back through the join; unlabeled rows are present
    # (propensity denominators) but never form pairs.
    assert report.rows_total == 24
    assert report.rows_labeled == 16
    assert report.train_requests == 4
    assert report.eval_requests == 4

    # The fit recovers a shorter half-life and must beat the logged-params
    # baseline on the per-project holdout (the #17198 gate comparison).
    assert report.fitted.pooled.half_life_days == 7.0
    assert report.baseline_eval.accuracy == pytest.approx(0.0)
    assert report.fitted_eval.accuracy == pytest.approx(1.0)
    assert set(report.fitted_eval.per_project) == {"proj-fit-a", "proj-fit-b"}
    for accuracy in report.fitted_eval.per_project.values():
        assert 0.0 <= accuracy <= 1.0

    # Label streams stay separable: asking for judge labels finds the same
    # feature rows but zero labels — ablation labels never leak into that fit.
    judge_rows = [
        replay_row_from_signal_row(row) for row in store.fetch_replay_rows(label_source="llm_judge")
    ]
    assert len(judge_rows) == 24
    assert all(row.judge_useful is None for row in judge_rows)

    # Full-ranking-path features drove the replay (not graph traversal).
    fit_rows = [
        replay_row_from_signal_row(row) for row in store.fetch_replay_rows(label_source="ablation")
    ]
    assert all(row.ranking_mode == "semantic_only" for row in fit_rows)
    assert all(row.temporal_decay_factor is not None for row in fit_rows)

    # Production ship-gate path over complete shadow labels, immutable prompt
    # snapshots, bound audit verdicts, and one atomic holdout reservation.
    cohort = _seed_shadow_gate_rows(store)
    gate_args = {
        "label_source": cohort.label_source,
        "candidate_scope": cohort.candidate_scope,
        "judge_protocol_version": cohort.judge_protocol_version,
        "weighting_regime_key": cohort.weighting_regime_key,
        "judge_model_key": cohort.judge_model_key,
        "judge_config_fingerprint": cohort.judge_config_fingerprint,
        "data_cutoff": cohort.data_cutoff,
        "completion_cutoff": cohort.completion_cutoff,
    }

    decision = run_ship_gate_from_store(store, **gate_args)
    repeated = run_ship_gate_from_store(store, **gate_args)

    print(f"ship gate: ship={decision.ship} reasons={list(decision.reasons)}")
    assert decision.report.fitted.pooled == replace(static_replay_params(), half_life_days=60.0)
    assert decision.audit_ok is True
    assert decision.sufficient_data is True
    assert decision.beats_static is True
    assert decision.guard_ok is True
    assert decision.ship is True
    assert repeated.to_record() == decision.to_record()
    with pytest.raises(ValueError, match="fit_settings_digest"):
        run_ship_gate_from_store(store, **gate_args, shrinkage_requests=20.0)

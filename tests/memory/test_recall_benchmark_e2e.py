"""End-to-end recall gate: graph traversal lift reaches SearchService.search results.

#17104 follow-up. Measurement on the shared mean-of-entity-embeddings corpus
(``_recall_corpus.py``) showed two things: graph-only "backfill" (a ``similarity=None``
hit sorting last) yields *zero* recall lift through ``_build_results``, and naive
synthetic similarity net-regresses there because the cross-cluster noise that creates
vector headroom is the *same* noise that pollutes graph entity-matching -- headroom and
graph cleanliness are coupled in that corpus, so no calibration separates them.

This benchmark uses a purpose-built corpus that DECOUPLES a memory's VECTOR presence
from its GRAPH structure: each memory's Qdrant vector is authored explicitly, independent
of its entity embeddings. That models the real recall-expansion case the #17104 mechanism
targets -- a memory whose document embedding misses the query but which mentions an entity
that matched it. Three roles per cluster:

    anchor  -- relevant; high query cosine; the vector index finds it.
    hidden  -- relevant; ~zero query cosine (the vector index misses it), but it mentions
               the cluster hub entity, so graph entity-search surfaces it. This is the
               recall the mechanism must recover into top-K via synthetic similarity.
    decoy   -- NOT relevant; moderate query cosine (it occupies a vector top-K slot); its
               entities are private (never the hub), so graph never surfaces it.

graph-off ranks anchors+decoys and misses every hidden memory. graph-on must lift the
hidden memories past the decoys WITHOUT displacing the higher-similarity anchors --
semantic-first holds because a hidden hit's discounted entity cosine stays below an
anchor's real document cosine. The gate asserts graph-on recall@K beats the graph-off
baseline, with at least one newly included relevant id carrying ``graph`` in
``search_via`` and no MRR regression on the returned order.

Run:
GOBBY_TEST_FALKOR_PASSWORD="$(
uv run python -c 'from pathlib import Path
from gobby.cli.installers.compose_env import resolve_compose_runtime
runtime = resolve_compose_runtime(Path.home() / ".gobby", profiles=("falkordb",))
print(runtime.environment["GOBBY_FALKORDB_PASSWORD"])'
)" \\
GOBBY_TEST_PROTECT=1 uv run pytest tests/memory/test_recall_benchmark_e2e.py \\
-m integration -v -s

Connection is read from GOBBY_TEST_FALKOR_HOST (default 127.0.0.1),
GOBBY_TEST_FALKOR_PORT (default 16379), GOBBY_TEST_FALKOR_PASSWORD. The test runs in a
uniquely-named graph and clears it per arm, so it never touches gobby_kg.
"""

from __future__ import annotations

import os
import re
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

import pytest

from gobby.config.persistence import MemoryConfig, MemoryKnowledgeGraphConfig
from gobby.memory.services.knowledge_graph.service import KnowledgeGraphService
from gobby.memory.services.search import SearchDebugSnapshot, SearchService
from gobby.memory.vectorstore import VectorStore
from gobby.storage.memories import Memory
from gobby.storage.projects import PERSONAL_PROJECT_ID
from tests.memory._recall_corpus import _Stub, _StubExtractor
from tests.memory.recall_benchmark_cleanup import drop_recall_benchmark_graph

pytestmark = [pytest.mark.integration]

# --------------------------------------------------------------------------- #
# Corpus geometry                                                             #
# --------------------------------------------------------------------------- #

DIM = 32
NCLUST = 4
ANCHORS_PER_CLUSTER = 2
HIDDEN_PER_CLUSTER = 2
DECOYS_PER_CLUSTER = 4
K = 4

_GRAPH_MIN_SCORE = 0.5
_ANCHOR_WEIGHT = 1.0
_DECOY_WEIGHT = 0.3
_CLUSTER_SPREAD = 0.15  # every anchor/decoy loads a little of every cluster axis so that
# cross-cluster memories out-rank a hidden memory in the vector index, pushing hidden out
# of the top-(limit*2) candidate set entirely -- the only way it becomes a graph-only hit.

_CLUSTER_AXIS_BASE = 1  # cluster c -> axis (1 + c); axes 1..NCLUST
_HIDDEN_AXIS_BASE = 1 + NCLUST  # hidden private axes start here (disjoint from clusters)
_PRIVATE_AXIS_BASE = 16  # off-cluster entity axes 16..DIM-1 (never match a cluster query)

_MEMORY_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "gobby-memory-recall-e2e-17104")


def _cluster_axis(cluster: int) -> int:
    return _CLUSTER_AXIS_BASE + cluster


def _query_text(cluster: int) -> str:
    # Single token so extract_keywords() returns None and the text passes through to
    # embed_fn unchanged; the "hubaxis<c>" marker is what embed_fn keys on.
    return f"hubaxis{cluster}"


def _make_e2e_embed_fn() -> Callable[..., Awaitable[list[float]]]:
    """Entity/query embeddings. ``hubaxis<c>`` -> the cluster axis; anything else -> a
    private axis >= 16 that no cluster query can match (so only hubs surface in graph)."""
    pattern = re.compile(r"hubaxis(\d+)")

    async def embed(name: str, is_query: bool = False) -> list[float]:
        vec = [0.0] * DIM
        match = pattern.search(name)
        if match:
            vec[_cluster_axis(int(match.group(1)))] = 1.0
        else:
            span = DIM - _PRIVATE_AXIS_BASE
            axis = _PRIVATE_AXIS_BASE + (int(sha256(name.encode()).hexdigest(), 16) % span)
            vec[axis] = 1.0
        return vec

    return embed


@dataclass(frozen=True)
class _E2EMemory:
    memory_id: str
    cluster: int
    role: str  # "anchor" | "hidden" | "decoy"
    relevant: bool
    entities: list[str]
    typed_pairs: list[tuple[str, str]]
    vector: list[float]  # explicit Qdrant vector, decoupled from entity embeddings


def _cluster_vector(cluster: int, weight: float) -> list[float]:
    vec = [0.0] * DIM
    for c in range(NCLUST):
        vec[_cluster_axis(c)] = weight if c == cluster else _CLUSTER_SPREAD
    return vec


def _hidden_vector(global_index: int) -> list[float]:
    vec = [0.0] * DIM
    vec[_HIDDEN_AXIS_BASE + global_index] = 1.0  # pure private axis -> ~0 cosine to query
    return vec


def build_e2e_corpus() -> list[_E2EMemory]:
    memories: list[_E2EMemory] = []
    hidden_index = 0
    for c in range(NCLUST):
        hub = _query_text(c)
        for a in range(ANCHORS_PER_CLUSTER):
            mid = f"c{c}_anchor_{a}"
            spoke = f"spoke_{mid}"
            memories.append(
                _E2EMemory(
                    memory_id=mid,
                    cluster=c,
                    role="anchor",
                    relevant=True,
                    entities=[hub, spoke],
                    typed_pairs=[(hub, spoke)],
                    vector=_cluster_vector(c, _ANCHOR_WEIGHT),
                )
            )
        for h in range(HIDDEN_PER_CLUSTER):
            mid = f"c{c}_hidden_{h}"
            spoke = f"spoke_{mid}"
            memories.append(
                _E2EMemory(
                    memory_id=mid,
                    cluster=c,
                    role="hidden",
                    relevant=True,
                    entities=[hub, spoke],
                    typed_pairs=[(hub, spoke)],
                    vector=_hidden_vector(hidden_index),
                )
            )
            hidden_index += 1
        for d in range(DECOYS_PER_CLUSTER):
            mid = f"c{c}_decoy_{d}"
            e0, e1 = f"decoy_{mid}_0", f"decoy_{mid}_1"
            memories.append(
                _E2EMemory(
                    memory_id=mid,
                    cluster=c,
                    role="decoy",
                    relevant=False,
                    entities=[e0, e1],
                    typed_pairs=[(e0, e1)],
                    vector=_cluster_vector(c, _DECOY_WEIGHT),
                )
            )
    return memories


def _stable_memory_id(memory_id: str) -> str:
    return str(uuid.uuid5(_MEMORY_ID_NAMESPACE, memory_id))


# --------------------------------------------------------------------------- #
# Harness                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _ArmConfig:
    name: str
    falkordb_graph_search: bool
    graph_edge_weighting: bool
    materialize_cooccurrence: bool
    cluster_recall_expansion: bool = False
    recluster_entities: bool = False
    cluster_expansion_per_entity: int = 3


@dataclass
class _ArmResult:
    production_recall: float
    production_mrr: float
    rrf_recall: float
    rrf_mrr: float
    search_via_counts: Counter[str]
    ranking_mode_counts: Counter[str]
    topk_by_query: dict[str, list[str]]
    search_via_by_query: dict[str, dict[str, str]]
    lifted_relevant_graph_ids: dict[str, list[str]] = field(default_factory=dict)


class _MemoryStorage:
    def __init__(self, corpus: list[_E2EMemory], id_map: dict[str, str]) -> None:
        self._by_stable_id = {id_map[mem.memory_id]: mem for mem in corpus}

    def _memory(self, memory_id: str) -> Memory:
        mem = self._by_stable_id.get(memory_id)
        if mem is None:
            raise ValueError(memory_id)
        now = datetime.now(UTC).isoformat()
        # Fresh object every call: _build_results mutates search_via/similarity/ranking_*
        # on the returned objects, so sharing instances would bleed evidence across arms.
        return Memory(
            id=memory_id,
            memory_type="fact",
            content=mem.memory_id,
            created_at=now,
            updated_at=now,
            source_type="agent",
            tags=[],
        )

    def get_memories(
        self,
        memory_ids: list[str],
        scope: object = None,
        *,
        visibility: str = "active",
    ) -> list[Memory]:
        out: list[Memory] = []
        for memory_id in memory_ids:
            mem = self._by_stable_id.get(memory_id)
            if mem is not None:
                out.append(self._memory(memory_id))
        return out

    def get_memory(
        self,
        memory_id: str,
        scope: object = None,
        *,
        visibility: str = "active",
    ) -> Memory:
        return self._memory(memory_id)

    def update_access_stats(self, memory_id: str, accessed_at: str) -> None:
        return None


def _score_ranked(ranked: list[str], expected: set[str]) -> tuple[float, float]:
    if not expected:
        return 0.0, 0.0
    topk = set(ranked[:K])
    recall = len(topk & expected) / len(expected)
    rr = 0.0
    for rank, memory_id in enumerate(ranked, start=1):
        if memory_id in expected:
            rr = 1.0 / rank
            break
    return recall, rr


async def _build_service(
    *,
    client: Any,
    tmp_path: Any,
    arm: _ArmConfig,
    corpus: list[_E2EMemory],
    id_map: dict[str, str],
    snapshots: list[SearchDebugSnapshot],
) -> tuple[SearchService, VectorStore]:
    await client.query("MATCH (n) DETACH DELETE n")

    embed_fn = _make_e2e_embed_fn()
    service = KnowledgeGraphService(
        falkor_client=client,
        embed_fn=embed_fn,
        prompt_loader=_Stub(),
        llm_service=_Stub(),
        feature_config=MemoryKnowledgeGraphConfig(),
        vector_store=None,
        embedding_dim=DIM,
        graph_edge_weighting=arm.graph_edge_weighting,
        materialize_cooccurrence=arm.materialize_cooccurrence,
        graph_edge_decay=False,
        cluster_recall_expansion=arm.cluster_recall_expansion,
        cluster_expansion_per_entity=arm.cluster_expansion_per_entity,
    )
    service._extractor = _StubExtractor({mem.memory_id: mem for mem in corpus})  # type: ignore[assignment]

    for mem in corpus:
        result = await service.add_to_graph(
            content=mem.memory_id,
            memory_id=id_map[mem.memory_id],
            project_id=PERSONAL_PROJECT_ID,
        )
        assert result.status.value in {"success", "partial_failure"}, result

    if arm.recluster_entities:
        await service.recluster_entities(project_id=PERSONAL_PROJECT_ID)

    vector_store = VectorStore(
        path=str(tmp_path / f"qdrant_{arm.name}"),
        collection_name=f"e2e_{arm.name}_{os.getpid()}",
        embedding_dim=DIM,
    )
    await vector_store.initialize()
    for mem in corpus:
        await vector_store.upsert(
            id_map[mem.memory_id],
            mem.vector,
            payload={"content": mem.memory_id},
        )

    search_service = SearchService(
        storage=_MemoryStorage(corpus, id_map),  # type: ignore[arg-type]
        vector_store=vector_store,
        embed_fn=embed_fn,
        kg_service=service,
        keyword_search=lambda query, limit, project_id, *, include_global=True: [],
        config=MemoryConfig(
            cluster_recall_expansion=arm.cluster_recall_expansion,
            cluster_expansion_per_entity=arm.cluster_expansion_per_entity,
        ),
        falkordb_graph_search=arm.falkordb_graph_search,
        falkordb_graph_min_score=_GRAPH_MIN_SCORE,
        rrf_k=60,
        falkordb_rrf_k=60,
        vector_store_failure_logger=lambda message, error: None,
        run_db=None,
        search_debug_sink=snapshots.append,
    )
    return search_service, vector_store


async def _run_arm(
    *,
    client: Any,
    tmp_path: Any,
    arm: _ArmConfig,
    corpus: list[_E2EMemory],
    id_map: dict[str, str],
    expected_by_query: dict[str, set[str]],
) -> _ArmResult:
    snapshots: list[SearchDebugSnapshot] = []
    search_service, vector_store = await _build_service(
        client=client,
        tmp_path=tmp_path,
        arm=arm,
        corpus=corpus,
        id_map=id_map,
        snapshots=snapshots,
    )
    try:
        production_recalls: list[float] = []
        production_rrs: list[float] = []
        rrf_recalls: list[float] = []
        rrf_rrs: list[float] = []
        search_via_counts: Counter[str] = Counter()
        ranking_mode_counts: Counter[str] = Counter()
        topk_by_query: dict[str, list[str]] = {}
        search_via_by_query: dict[str, dict[str, str]] = {}

        for cluster in range(NCLUST):
            query = _query_text(cluster)
            before = len(snapshots)
            query_embedding = await search_service._embed_fn(query, is_query=True)  # type: ignore[misc]
            # Sanity: the resolved query peaks on its cluster axis. A silent embedding
            # degradation would otherwise fake a non-discriminating benchmark.
            assert query_embedding[_cluster_axis(cluster)] == max(query_embedding)
            assert query_embedding[_cluster_axis(cluster)] == 1.0

            results = await search_service.search(
                query=query, project_id=PERSONAL_PROJECT_ID, limit=K
            )
            assert len(snapshots) == before + 1
            snapshot = snapshots[-1]
            assert snapshot.returned_ids == [result.id for result in results]

            production_ranked = [result.id for result in results][:K]
            rrf_ranked = list(snapshot.merged_ids)[:K]
            expected = expected_by_query[query]

            production_recall, production_rr = _score_ranked(production_ranked, expected)
            rrf_recall, rrf_rr = _score_ranked(rrf_ranked, expected)
            production_recalls.append(production_recall)
            production_rrs.append(production_rr)
            rrf_recalls.append(rrf_recall)
            rrf_rrs.append(rrf_rr)
            topk_by_query[query] = production_ranked
            search_via_by_query[query] = {
                result.id: result.search_via or "unknown" for result in results
            }
            search_via_counts.update(result.search_via or "unknown" for result in results)
            ranking_mode_counts.update(result.ranking_mode or "unknown" for result in results)

        def mean(xs: list[float]) -> float:
            return sum(xs) / len(xs) if xs else 0.0

        return _ArmResult(
            production_recall=mean(production_recalls),
            production_mrr=mean(production_rrs),
            rrf_recall=mean(rrf_recalls),
            rrf_mrr=mean(rrf_rrs),
            search_via_counts=search_via_counts,
            ranking_mode_counts=ranking_mode_counts,
            topk_by_query=topk_by_query,
            search_via_by_query=search_via_by_query,
        )
    finally:
        await vector_store.close()


def _lifted_relevant_graph_ids(
    *,
    baseline: _ArmResult,
    candidate: _ArmResult,
    expected_by_query: dict[str, set[str]],
) -> dict[str, list[str]]:
    """Relevant ids in the candidate arm's top-K that the baseline arm missed and that
    the candidate surfaced via the graph (``graph`` in ``search_via``)."""
    lifted: dict[str, list[str]] = {}
    for query, cand_topk in candidate.topk_by_query.items():
        base_topk = set(baseline.topk_by_query.get(query, []))
        expected = expected_by_query[query]
        via = candidate.search_via_by_query[query]
        ids = [
            memory_id
            for memory_id in cand_topk
            if memory_id not in base_topk
            and memory_id in expected
            and "graph" in via.get(memory_id, "")
        ]
        if ids:
            lifted[query] = ids
    return lifted


async def test_search_memories_e2e_recall_gate(tmp_path: Any) -> None:
    host = os.environ.get("GOBBY_TEST_FALKOR_HOST", "127.0.0.1")
    port = int(os.environ.get("GOBBY_TEST_FALKOR_PORT", "16379"))
    password = os.environ.get("GOBBY_TEST_FALKOR_PASSWORD")
    if not password:
        pytest.skip("GOBBY_TEST_FALKOR_PASSWORD is unset")

    from gobby.memory.falkor_client import FalkorClient

    graph_name = f"test_recall_benchmark_e2e_{os.getpid()}"
    client = FalkorClient(host=host, port=port, password=password, graph_name=graph_name)
    reachable = False
    try:
        if not await client.ping():
            pytest.fail("FalkorDB not reachable for integration benchmark")
        reachable = True

        corpus = build_e2e_corpus()
        id_map = {mem.memory_id: _stable_memory_id(mem.memory_id) for mem in corpus}
        expected_by_query: dict[str, set[str]] = {}
        for cluster in range(NCLUST):
            query = _query_text(cluster)
            expected_by_query[query] = {
                id_map[mem.memory_id] for mem in corpus if mem.cluster == cluster and mem.relevant
            }

        arms = [
            _ArmConfig("graph_off", False, False, False),
            _ArmConfig("flags_off", True, False, False),
            _ArmConfig("cluster_expansion", True, False, False, True, True),
            _ArmConfig("flags_on", True, True, True),
        ]
        results: dict[str, _ArmResult] = {}
        for arm in arms:
            results[arm.name] = await _run_arm(
                client=client,
                tmp_path=tmp_path,
                arm=arm,
                corpus=corpus,
                id_map=id_map,
                expected_by_query=expected_by_query,
            )

        graph_off = results["graph_off"]
        flags_off = results["flags_off"]
        cluster_expansion = results["cluster_expansion"]
        flags_on = results["flags_on"]
        flags_off.lifted_relevant_graph_ids = _lifted_relevant_graph_ids(
            baseline=graph_off, candidate=flags_off, expected_by_query=expected_by_query
        )
        cluster_expansion.lifted_relevant_graph_ids = _lifted_relevant_graph_ids(
            baseline=graph_off,
            candidate=cluster_expansion,
            expected_by_query=expected_by_query,
        )
        flags_on.lifted_relevant_graph_ids = _lifted_relevant_graph_ids(
            baseline=graph_off, candidate=flags_on, expected_by_query=expected_by_query
        )

        print("\n=== Search facade recall benchmark (gobby #17104) ===")
        print(
            f"corpus: {len(corpus)} memories, {NCLUST} clusters "
            f"({ANCHORS_PER_CLUSTER} anchor + {HIDDEN_PER_CLUSTER} hidden + "
            f"{DECOYS_PER_CLUSTER} decoy each), recall@{K}"
        )
        for name in ("graph_off", "flags_off", "cluster_expansion", "flags_on"):
            result = results[name]
            print(
                f"{name:<10} production recall@{K}={result.production_recall:.3f} "
                f"MRR={result.production_mrr:.3f} | "
                f"RRF recall@{K}={result.rrf_recall:.3f} MRR={result.rrf_mrr:.3f}"
            )
            print(f"  search_via={dict(result.search_via_counts)}")
            print(f"  ranking_mode={dict(result.ranking_mode_counts)}")
        print(f"flags_off lifted relevant graph IDs: {flags_off.lifted_relevant_graph_ids}")
        print(
            "cluster_expansion lifted relevant graph IDs: "
            f"{cluster_expansion.lifted_relevant_graph_ids}"
        )
        print(f"flags_on  lifted relevant graph IDs: {flags_on.lifted_relevant_graph_ids}")
        print(
            "cluster_expansion adoption gate: "
            f"{cluster_expansion.production_recall > graph_off.production_recall and cluster_expansion.production_mrr >= graph_off.production_mrr - 1e-9}"
        )

        # ----------------------------------------------------------------- #
        # Gate (#17104): graph-on lifts relevant graph-only hits into top-K #
        # over the semantic-first graph-off baseline, with no MRR loss.     #
        # ----------------------------------------------------------------- #
        for result in results.values():
            assert 0.0 <= result.production_recall <= 1.0
            assert 0.0 <= result.production_mrr <= 1.0

        # Corpus validity: vector alone must leave headroom (it misses the hidden hits).
        assert graph_off.production_recall <= 0.8, (
            "graph_off recall has no headroom; the corpus no longer discriminates. "
            f"got {graph_off.production_recall:.3f}"
        )

        # The mechanism: each graph-on arm beats the graph-off baseline by lifting a
        # relevant graph-sourced id into top-K, and never regresses MRR (semantic-first
        # keeps the higher-similarity anchors on top).
        for arm_name in ("flags_off", "flags_on"):
            arm = results[arm_name]
            assert arm.production_recall > graph_off.production_recall, (
                f"{arm_name} did not improve recall over graph_off "
                f"({arm.production_recall:.3f} vs {graph_off.production_recall:.3f})"
            )
            assert arm.lifted_relevant_graph_ids, (
                f"{arm_name} improved recall without a relevant graph-sourced lift"
            )
            assert arm.production_mrr >= graph_off.production_mrr - 1e-9, (
                f"{arm_name} regressed MRR vs graph_off "
                f"({arm.production_mrr:.3f} vs {graph_off.production_mrr:.3f})"
            )
    finally:
        try:
            if reachable:
                await drop_recall_benchmark_graph(client, graph_name)
        finally:
            await client.close()

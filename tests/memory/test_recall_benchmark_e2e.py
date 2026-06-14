"""End-to-end recall gate for memory KG weighting through SearchService.search."""

from __future__ import annotations

import os
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.config.persistence import MemoryConfig, MemoryKnowledgeGraphConfig
from gobby.memory.services.knowledge_graph.service import KnowledgeGraphService
from gobby.memory.services.search import SearchDebugSnapshot, SearchService
from gobby.memory.vectorstore import VectorStore
from gobby.storage.memories import Memory
from tests.memory._recall_corpus import (
    DIM,
    NUM_CLUSTERS,
    K,
    MemoryDef,
    _Stub,
    _StubExtractor,
    build_corpus,
    make_embed_fn,
)

pytestmark = [pytest.mark.integration]

_E2E_EMBED_UNIQUE_SIGNAL = 1.5
_MEMORY_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "gobby-memory-recall-e2e")


@dataclass(frozen=True)
class _ArmConfig:
    name: str
    falkordb_graph_search: bool
    graph_edge_weighting: bool
    materialize_cooccurrence: bool


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
    lifted_relevant_graph_ids: dict[str, list[str]]


class _MemoryStorage:
    def __init__(self, corpus: list[MemoryDef], id_map: dict[str, str]) -> None:
        self._corpus_by_id = {id_map[mem.memory_id]: mem for mem in corpus}

    def _memory(self, memory_id: str) -> Memory:
        mem = self._corpus_by_id.get(memory_id)
        if mem is None:
            raise ValueError(memory_id)
        now = datetime.now(UTC).isoformat()
        return Memory(
            id=memory_id,
            memory_type="fact",
            content=mem.memory_id,
            created_at=now,
            updated_at=now,
            source_type="agent",
            tags=[],
        )

    def get_memories(self, memory_ids: list[str], project_id: str | None = None) -> list[Memory]:
        return [
            self._memory(memory_id) for memory_id in memory_ids if memory_id in self._corpus_by_id
        ]

    def get_memory(self, memory_id: str, project_id: str | None = None) -> Memory:
        return self._memory(memory_id)

    def list_memories(
        self,
        project_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 10,
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
    ) -> list[Memory]:
        return [self._memory(memory_id) for memory_id in list(self._corpus_by_id)[:limit]]

    def update_access_stats(self, memory_id: str, accessed_at: str) -> None:
        return None


def _stable_memory_id(memory_id: str) -> str:
    return str(uuid.uuid5(_MEMORY_ID_NAMESPACE, memory_id))


async def _mean_entity_embedding(mem: MemoryDef, embed_fn: Any) -> list[float]:
    vectors = [await embed_fn(entity) for entity in mem.entities]
    return [sum(values) / len(vectors) for values in zip(*vectors, strict=True)]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _score_ranked(ranked: list[str], expected: set[str]) -> tuple[float, float]:
    if not expected:
        return 0.0, 0.0
    recall = len(set(ranked) & expected) / len(expected)
    reciprocal_rank = 0.0
    for rank, memory_id in enumerate(ranked, start=1):
        if memory_id in expected:
            reciprocal_rank = 1.0 / rank
            break
    return recall, reciprocal_rank


def _expected_by_query(
    corpus: list[MemoryDef],
    id_map: dict[str, str],
) -> dict[str, set[str]]:
    by_cluster: dict[int, set[str]] = {}
    for mem in corpus:
        if mem.memory_id.endswith("_noise"):
            continue
        by_cluster.setdefault(mem.cluster, set()).add(id_map[mem.memory_id])

    expected: dict[str, set[str]] = {}
    for mem in corpus:
        if mem.memory_id.endswith("_noise"):
            continue
        query_id = id_map[mem.memory_id]
        expected[mem.memory_id] = by_cluster[mem.cluster] - {query_id}
    return expected


async def _build_service(
    *,
    client: Any,
    tmp_path: Any,
    arm: _ArmConfig,
    corpus: list[MemoryDef],
    id_map: dict[str, str],
    snapshots: list[SearchDebugSnapshot],
) -> tuple[SearchService, VectorStore]:
    await client.query("MATCH (n) DETACH DELETE n")

    embed_fn = make_embed_fn(DIM, unique_signal=_E2E_EMBED_UNIQUE_SIGNAL)
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
    )
    service._extractor = _StubExtractor({mem.memory_id: mem for mem in corpus})  # type: ignore[assignment]

    for mem in corpus:
        result = await service.add_to_graph(
            content=mem.memory_id,
            memory_id=id_map[mem.memory_id],
            project_id=None,
        )
        assert result.status.value in {"success", "partial_failure"}, result

    vector_store = VectorStore(
        path=str(tmp_path / f"qdrant_{arm.name}"),
        collection_name=f"e2e_{arm.name}_{os.getpid()}",
        embedding_dim=DIM,
    )
    await vector_store.initialize()
    for mem in corpus:
        await vector_store.upsert(
            id_map[mem.memory_id],
            await _mean_entity_embedding(mem, embed_fn),
            payload={"content": mem.memory_id},
        )

    search_service = SearchService(
        storage=_MemoryStorage(corpus, id_map),  # type: ignore[arg-type]
        vector_store=vector_store,
        embed_fn=embed_fn,
        kg_service=service,
        keyword_search=lambda query, limit, project_id: [],
        config=MemoryConfig(),
        falkordb_graph_search=arm.falkordb_graph_search,
        falkordb_graph_min_score=0.5,
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
    corpus: list[MemoryDef],
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

        queries = [mem for mem in corpus if not mem.memory_id.endswith("_noise")]
        for mem in queries:
            before = len(snapshots)
            query = mem.entities[1]
            query_embedding = await search_service._embed_fn(query, is_query=True)  # type: ignore[misc]
            assert query_embedding[mem.cluster % (DIM - 1)] > 0.0

            results = await search_service.search(query=query, limit=K + 1)
            assert len(snapshots) == before + 1
            snapshot = snapshots[-1]
            assert snapshot.returned_ids == [result.id for result in results]

            self_id = id_map[mem.memory_id]
            production_ranked = [result.id for result in results if result.id != self_id][:K]
            rrf_ranked = [memory_id for memory_id in snapshot.merged_ids if memory_id != self_id][
                :K
            ]
            expected = expected_by_query[mem.memory_id]

            production_recall, production_rr = _score_ranked(production_ranked, expected)
            rrf_recall, rrf_rr = _score_ranked(rrf_ranked, expected)
            production_recalls.append(production_recall)
            production_rrs.append(production_rr)
            rrf_recalls.append(rrf_recall)
            rrf_rrs.append(rrf_rr)
            topk_by_query[mem.memory_id] = production_ranked
            search_via_by_query[mem.memory_id] = {
                result.id: result.search_via or "unknown" for result in results
            }

            search_via_counts.update(result.search_via or "unknown" for result in results)
            ranking_mode_counts.update(result.ranking_mode or "unknown" for result in results)

        return _ArmResult(
            production_recall=_mean(production_recalls),
            production_mrr=_mean(production_rrs),
            rrf_recall=_mean(rrf_recalls),
            rrf_mrr=_mean(rrf_rrs),
            search_via_counts=search_via_counts,
            ranking_mode_counts=ranking_mode_counts,
            topk_by_query=topk_by_query,
            search_via_by_query=search_via_by_query,
            lifted_relevant_graph_ids={},
        )
    finally:
        await vector_store.close()


def _lifted_relevant_graph_ids(
    *,
    flags_off: _ArmResult,
    flags_on: _ArmResult,
    expected_by_query: dict[str, set[str]],
) -> dict[str, list[str]]:
    lifted: dict[str, list[str]] = {}
    for query, on_topk in flags_on.topk_by_query.items():
        off_topk = set(flags_off.topk_by_query[query])
        expected = expected_by_query[query]
        via = flags_on.search_via_by_query[query]
        relevant_graph_ids = [
            memory_id
            for memory_id in on_topk
            if memory_id not in off_topk
            and memory_id in expected
            and "graph" in via.get(memory_id, "")
        ]
        if relevant_graph_ids:
            lifted[query] = relevant_graph_ids
    return lifted


async def test_search_memories_e2e_recall_gate(tmp_path: Any) -> None:
    host = os.environ.get("GOBBY_TEST_FALKOR_HOST", "127.0.0.1")
    port = int(os.environ.get("GOBBY_TEST_FALKOR_PORT", "16379"))
    password = os.environ.get("GOBBY_TEST_FALKOR_PASSWORD")

    from gobby.memory.falkor_client import FalkorClient

    graph_name = f"test_recall_benchmark_e2e_{os.getpid()}"
    try:
        client = FalkorClient(host=host, port=port, password=password, graph_name=graph_name)
    except Exception as exc:
        pytest.skip(f"FalkorDB not reachable for integration benchmark: {exc}")
    reachable = False
    try:
        if not await client.ping():
            pytest.skip("FalkorDB not reachable for integration benchmark")
        reachable = True

        corpus = build_corpus()
        id_map = {mem.memory_id: _stable_memory_id(mem.memory_id) for mem in corpus}
        expected = _expected_by_query(corpus, id_map)
        arms = [
            _ArmConfig(
                name="graph_off",
                falkordb_graph_search=False,
                graph_edge_weighting=False,
                materialize_cooccurrence=False,
            ),
            _ArmConfig(
                name="flags_off",
                falkordb_graph_search=True,
                graph_edge_weighting=False,
                materialize_cooccurrence=False,
            ),
            _ArmConfig(
                name="flags_on",
                falkordb_graph_search=True,
                graph_edge_weighting=True,
                materialize_cooccurrence=True,
            ),
        ]

        results: dict[str, _ArmResult] = {}
        for arm in arms:
            results[arm.name] = await _run_arm(
                client=client,
                tmp_path=tmp_path,
                arm=arm,
                corpus=corpus,
                id_map=id_map,
                expected_by_query=expected,
            )

        graph_off = results["graph_off"]
        flags_off = results["flags_off"]
        flags_on = results["flags_on"]
        flags_on.lifted_relevant_graph_ids = _lifted_relevant_graph_ids(
            flags_off=flags_off,
            flags_on=flags_on,
            expected_by_query=expected,
        )

        print("\n=== Search facade recall benchmark (gobby #17102) ===")
        print(f"corpus: {len(corpus)} memories, {NUM_CLUSTERS} clusters, recall@{K}")
        for name in ("graph_off", "flags_off", "flags_on"):
            result = results[name]
            print(
                f"{name:<10} production recall@{K}={result.production_recall:.3f} "
                f"MRR={result.production_mrr:.3f} | "
                f"RRF recall@{K}={result.rrf_recall:.3f} MRR={result.rrf_mrr:.3f}"
            )
            print(f"  search_via={dict(result.search_via_counts)}")
            print(f"  ranking_mode={dict(result.ranking_mode_counts)}")
        graph_driven_lift = flags_on.production_recall > flags_off.production_recall and bool(
            flags_on.lifted_relevant_graph_ids
        )
        print(f"lifted relevant graph IDs: {flags_on.lifted_relevant_graph_ids}")
        print(f"flip gate passed: {graph_driven_lift}")

        assert graph_off.production_recall <= 0.8
        if flags_on.production_recall > flags_off.production_recall:
            assert flags_on.lifted_relevant_graph_ids
    finally:
        try:
            if reachable:
                await client.query("MATCH (n) DETACH DELETE n")
        finally:
            await client.close()

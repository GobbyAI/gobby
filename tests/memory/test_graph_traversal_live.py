"""Real-FalkorDB regression for indexed knowledge-graph traversal.

Projecting an UNWIND key alone does not force an indexed lookup before expanding
relationships. These tests inspect the server plans for production reader queries
and verify traversal results and latency against a real server.

They need GOBBY_TEST_FALKOR_PASSWORD (and optionally GOBBY_TEST_FALKOR_HOST and
GOBBY_TEST_FALKOR_PORT) and skip otherwise, like the recall benchmarks. Each run
uses its own graph and deletes that graph afterwards.

Run against the isolated test service:
    GOBBY_TEST_FALKOR_PASSWORD=... GOBBY_TEST_PROTECT=1 \\
        uv run pytest tests/memory/test_graph_traversal_live.py -m integration -v
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, call, patch

import pytest

if TYPE_CHECKING:
    from gobby.memory.falkor_client import FalkorClient

pytestmark = [pytest.mark.integration]


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_live_fixture_installs_schema_and_always_closes(
    monkeypatch: pytest.MonkeyPatch, cleanup_fails: bool
) -> None:
    monkeypatch.setenv("GOBBY_TEST_FALKOR_PASSWORD", "fixture-test-only")
    client = AsyncMock()
    if cleanup_fails:
        client._execute_command.side_effect = RuntimeError("cleanup failed")
    with patch("gobby.memory.falkor_client.FalkorClient", return_value=client):
        fixture_factory = cast(
            "Callable[[], AsyncGenerator[FalkorClient]]", live_client._get_wrapped_function()
        )
        fixture = fixture_factory()
        assert await anext(fixture) is client
        if cleanup_fails:
            with pytest.raises(RuntimeError, match="cleanup failed"):
                await fixture.aclose()
        else:
            await fixture.aclose()
        client.assert_has_calls(
            [
                call.ensure_memory_graph_schema(),
                call.query(
                    _SEED_CYPHER, {"hub": HUB_KEY, "pid": PROJECT_ID, "count": NEIGHBOR_COUNT}
                ),
            ]
        )
        client._execute_command.assert_awaited_once_with("GRAPH.DELETE", client._graph_name)
        client.close.assert_awaited_once_with()


PROJECT_ID = "traversal-live-project"
HUB_KEY = "traversal-live:hub"
NEIGHBOR_COUNT = 2000
STALL_SCALE_ENTITY_COUNT = 50000
STALL_SCALE_FRONTIER_COUNT = 64


async def test_cancelled_response_reconnects_before_next_query(live_client: FalkorClient) -> None:
    from gobby.memory import stage_timing

    with stage_timing.recording() as timings:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                live_client._execute_command("BLPOP", live_client._graph_name + ":absent", 0),
                timeout=0.1,
            )
        assert await asyncio.wait_for(live_client.ping(), timeout=2.0)
    assert timings.calls["falkor_response_cancelled"] == 1
    assert timings.calls["falkor_connection_opened"] == 1


_SEED_CYPHER = (
    "CREATE (h:_Entity {entity_key: $hub, project_id: $pid, is_global: false, name: 'hub'}) "
    "WITH h "
    "UNWIND range(1, $count) AS i "
    "CREATE (x:_Entity {entity_key: 'traversal-live:n' + toString(i), project_id: $pid, "
    "is_global: false, name: 'n' + toString(i)}) "
    "CREATE (h)-[:CO_OCCURS {weight: 0.5, support: 2, updated_at: timestamp()}]->(x) "
    "CREATE (x)-[:MENTIONED_IN]->(:Memory {memory_id: 'mem-' + toString(i), "
    "project_id: $pid, is_global: false, updated_at: timestamp()})"
)


@pytest.fixture
async def live_client() -> AsyncIterator[FalkorClient]:
    password = os.environ.get("GOBBY_TEST_FALKOR_PASSWORD")
    if not password:
        pytest.skip("GOBBY_TEST_FALKOR_PASSWORD is unset")

    from gobby.memory.falkor_client import FalkorClient

    host = os.environ.get("GOBBY_TEST_FALKOR_HOST", "127.0.0.1")
    port = int(os.environ.get("GOBBY_TEST_FALKOR_PORT", "26379"))
    client = FalkorClient(
        host=host,
        port=port,
        password=password,
        graph_name=f"test_graph_traversal_live_{os.getpid()}",
    )
    try:
        if not await client.ping():
            pytest.fail("FalkorDB not reachable for the traversal regression")
        # Schema setup waits for both unique constraints to become OPERATIONAL,
        # which requires their supporting entity-key and memory-id indexes ready.
        await client.ensure_memory_graph_schema()
        await client.query(
            _SEED_CYPHER, {"hub": HUB_KEY, "pid": PROJECT_ID, "count": NEIGHBOR_COUNT}
        )
        yield client
    finally:
        try:
            await client._execute_command("GRAPH.DELETE", client._graph_name)
        finally:
            await client.close()


async def test_hop_admits_seeded_neighbors_and_their_memories(live_client: FalkorClient) -> None:
    from gobby.memory.services.knowledge_graph.reader import (
        _RELATED_ENTITY_NEIGHBOR_LIMIT,
        KnowledgeGraphReader,
    )

    reader = KnowledgeGraphReader(live_client, None, embedding_dim=8)

    traversal = await reader.find_related_memory_ids(
        entity_keys=[HUB_KEY],
        max_hops=1,
        limit=NEIGHBOR_COUNT,
        project_id=PROJECT_ID,
        include_global=False,
        timeout_seconds=10.0,
    )

    # The hop admits at most this many neighbors per source; each mentions one memory.
    assert len(traversal.memory_ids) == _RELATED_ENTITY_NEIGHBOR_LIMIT
    assert all(memory_id.startswith("mem-") for memory_id in traversal.memory_ids)
    assert reader._traversal_timeout_count == 0


@pytest.mark.parametrize(
    ("query_marker", "binding"),
    [
        ("RETURN source_key AS source_key", "start"),
        ("RETURN DISTINCT m.memory_id AS memory_id", "e"),
        ("RETURN e.entity_key AS entity_key, m.memory_id AS memory_id", "e"),
    ],
    ids=["neighbors", "memory-links", "attributes"],
)
async def test_traversal_plan_binds_entities_with_an_index_scan(
    live_client: FalkorClient, query_marker: str, binding: str
) -> None:
    from gobby.memory.services.knowledge_graph.reader import KnowledgeGraphReader

    reader = KnowledgeGraphReader(live_client, None, embedding_dim=8)
    with patch.object(live_client, "query", wraps=live_client.query) as query_spy:
        traversal = await reader.find_related_memory_ids(
            entity_keys=[HUB_KEY],
            max_hops=1,
            limit=NEIGHBOR_COUNT,
            project_id=PROJECT_ID,
            include_global=False,
            timeout_seconds=10.0,
        )
    assert traversal.memory_ids
    query_call = next(
        query_call for query_call in query_spy.await_args_list if query_marker in query_call.args[0]
    )
    await _assert_entity_index_scan(live_client, *query_call.args, binding=binding)


async def _assert_entity_index_scan(
    live_client: FalkorClient, query: object, params: object, *, binding: str
) -> None:
    assert isinstance(query, str)
    assert isinstance(params, dict)
    header = (
        "CYPHER " + " ".join(f"{key}={json.dumps(value)}" for key, value in params.items()) + " "
    )

    plan = await live_client._execute_command(
        "GRAPH.EXPLAIN", live_client._graph_name, header + query
    )

    assert any(f"Node By Index Scan | ({binding}:_Entity)" in line for line in plan), plan


async def test_backing_memory_plan_binds_entities_with_an_index_scan(
    live_client: FalkorClient,
) -> None:
    from gobby.memory.services.knowledge_graph.reader import KnowledgeGraphReader

    reader = KnowledgeGraphReader(live_client, None, embedding_dim=8)
    entity_key = "traversal-live:n1"
    with patch.object(live_client, "query", wraps=live_client.query) as query_spy:
        backing = await reader._entity_backing_memories([entity_key], PROJECT_ID, False)
    assert backing == {entity_key: ["mem-1"]}
    query_call = query_spy.await_args
    assert query_call is not None

    await _assert_entity_index_scan(live_client, *query_call.args, binding="e")


async def test_vector_memory_link_plan_binds_entities_with_an_index_scan(
    live_client: FalkorClient,
) -> None:
    from gobby.memory.services.knowledge_graph.reader import KnowledgeGraphReader

    reader = KnowledgeGraphReader(live_client, None, embedding_dim=8)
    entity_rows = [
        {
            "entity_key": "traversal-live:n1",
            "name": "n1",
            "project_id": PROJECT_ID,
            "score": 1.0,
        }
    ]
    with (
        patch.object(reader, "ensure_vector_index", new_callable=AsyncMock),
        patch.object(live_client, "vector_search", new=AsyncMock(return_value=entity_rows)),
        patch.object(live_client, "query", wraps=live_client.query) as query_spy,
    ):
        results = await reader.search_entities_by_vector(
            [1.0] + [0.0] * 7, project_id=PROJECT_ID, include_global=False
        )
    assert results[0]["memory_ids"] == ["mem-1"]
    query_call = query_spy.await_args
    assert query_call is not None

    await _assert_entity_index_scan(live_client, *query_call.args, binding="e")


async def test_stall_scale_frontier_completes_within_recall_deadline(
    live_client: FalkorClient,
) -> None:
    from gobby.memory.services.knowledge_graph.reader import (
        _RELATED_ENTITY_NEIGHBOR_LIMIT,
        KnowledgeGraphReader,
    )

    # This geometry reproduces the old 10 s budget miss. Seed writes stay
    # outside the recall deadline.
    for first in range(0, STALL_SCALE_ENTITY_COUNT, 1000):
        await live_client.query(
            "UNWIND range($first, $last) AS i "
            "CREATE (:_Entity {entity_key: 'stress-' + toString(i), "
            "project_id: $pid, is_global: false})",
            {"first": first, "last": first + 999, "pid": PROJECT_ID},
        )
    for first in range(0, STALL_SCALE_ENTITY_COUNT, 1000):
        await live_client.query(
            "UNWIND range($first, $last) AS i UNWIND range(1, 7) AS offset "
            "WITH i, (i + offset) % $size AS j "
            "MATCH (a:_Entity {entity_key: 'stress-' + toString(i)}), "
            "(b:_Entity {entity_key: 'stress-' + toString(j)}) "
            "CREATE (a)-[:CO_OCCURS {weight: 0.5}]->(b)",
            {"first": first, "last": first + 999, "size": STALL_SCALE_ENTITY_COUNT},
        )
    source_keys = [
        f"stress-{index * STALL_SCALE_ENTITY_COUNT // STALL_SCALE_FRONTIER_COUNT}"
        for index in range(STALL_SCALE_FRONTIER_COUNT)
    ]
    reader = KnowledgeGraphReader(live_client, None, embedding_dim=8)

    async with asyncio.timeout(10.0):
        related_keys, _components, _scores = await reader._find_related_entity_keys(
            source_keys,
            max_hops=1,
            limit=STALL_SCALE_FRONTIER_COUNT * _RELATED_ENTITY_NEIGHBOR_LIMIT,
            project_id=PROJECT_ID,
            include_global=False,
        )

    assert len(related_keys) == STALL_SCALE_FRONTIER_COUNT * _RELATED_ENTITY_NEIGHBOR_LIMIT
    assert all(key.startswith("stress-") for key in related_keys)


async def test_hop_completes_well_inside_the_recall_budget(live_client: FalkorClient) -> None:
    """Loose guard: the barriered hop must not regress toward the recall budget.

    The live incident ran 1.5-5 s per hop on a graph of 27k edges; this seed is far
    smaller, so the bound is deliberately generous and only catches a regression
    to a scan-per-row plan.
    """
    from gobby.memory.services.knowledge_graph.reader import KnowledgeGraphReader

    reader = KnowledgeGraphReader(live_client, None, embedding_dim=8)

    started = time.perf_counter()
    await reader.find_related_memory_ids(
        entity_keys=[HUB_KEY],
        max_hops=1,
        limit=NEIGHBOR_COUNT,
        project_id=PROJECT_ID,
        include_global=False,
        timeout_seconds=10.0,
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0, f"traversal took {elapsed:.2f}s on the seeded hub"

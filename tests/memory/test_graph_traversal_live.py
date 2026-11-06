"""Real-FalkorDB regression for the knowledge-graph traversal query shape.

Mocked tests cannot see the planner cost behind the 10 s recall timeouts: on the
live graph the entity hop ran 1.5-5 s when UNWIND rows fed the indexed _Entity
MATCH directly, and 4-460 ms once a WITH barrier materialized them first. These
tests drive the production reader against a real server.

They need GOBBY_TEST_FALKOR_PASSWORD (and optionally GOBBY_TEST_FALKOR_HOST and
GOBBY_TEST_FALKOR_PORT) and skip otherwise, like the recall benchmarks. Each run
uses its own graph and deletes its contents afterwards.

Run against the isolated test service:
    GOBBY_TEST_FALKOR_PASSWORD=... GOBBY_TEST_PROTECT=1 \\
        uv run pytest tests/memory/test_graph_traversal_live.py -m integration -v
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from gobby.memory.falkor_client import FalkorClient

pytestmark = [pytest.mark.integration]

PROJECT_ID = "traversal-live-project"
HUB_KEY = "traversal-live:hub"
NEIGHBOR_COUNT = 2000

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
    port = int(os.environ.get("GOBBY_TEST_FALKOR_PORT", "16379"))
    client = FalkorClient(
        host=host,
        port=port,
        password=password,
        graph_name=f"test_graph_traversal_live_{os.getpid()}",
    )
    try:
        if not await client.ping():
            pytest.fail("FalkorDB not reachable for the traversal regression")
        await client.query("MATCH (n) DETACH DELETE n")
        await client.query(
            _SEED_CYPHER, {"hub": HUB_KEY, "pid": PROJECT_ID, "count": NEIGHBOR_COUNT}
        )
        yield client
    finally:
        await client.query("MATCH (n) DETACH DELETE n")
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

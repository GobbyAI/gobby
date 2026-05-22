"""Phase 2 FalkorDB dialect contract tests for the memory graph."""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gobby.memory.services.knowledge_graph import KnowledgeGraphService
from gobby.memory.services.knowledge_graph.code_linker import KnowledgeGraphCodeLinker
from gobby.memory.services.knowledge_graph.models import _GraphEntity
from gobby.memory.services.knowledge_graph.reader import KnowledgeGraphReader
from gobby.memory.services.knowledge_graph.writer import KnowledgeGraphWriter

pytestmark = pytest.mark.unit


def _load_falkor_symbols() -> tuple[type[Any], type[Exception]]:
    try:
        from gobby.memory.falkor_client import FalkorClient, FalkorQueryError
    except ModuleNotFoundError as exc:
        pytest.fail("Phase 2 requires gobby.memory.falkor_client.FalkorClient from Phase 1")
        raise AssertionError from exc
    return FalkorClient, FalkorQueryError


def _new_falkor_stub() -> tuple[Any, AsyncMock]:
    FalkorClient, _ = _load_falkor_symbols()
    client = object.__new__(FalkorClient)
    execute_command = AsyncMock()
    command_target = SimpleNamespace(execute_command=execute_command)
    client._db = command_target
    client._redis = command_target
    client._client = command_target
    client._graph_name = "gobby_kg"
    client._graph = SimpleNamespace(
        name="gobby_kg",
        query=AsyncMock(),
        ro_query=AsyncMock(),
    )
    return client, execute_command


def _compact(cypher: str) -> str:
    return " ".join(cypher.split())


def test_knowledge_graph_service_accepts_falkor_client_not_neo4j_client() -> None:
    """KnowledgeGraphService constructor names the FalkorDB client surface."""
    signature = inspect.signature(KnowledgeGraphService)

    assert "falkor_client" in signature.parameters
    assert "neo4j_client" not in signature.parameters


@pytest.mark.asyncio
async def test_reader_ensures_vector_index_with_required_dimension_keyword() -> None:
    """The reader calls FalkorClient.ensure_vector_index(dimension=...)."""
    falkor_client = AsyncMock()
    reader = KnowledgeGraphReader(falkor_client, embed_fn=None, embedding_dim=768)

    await reader.ensure_vector_index()

    falkor_client.ensure_vector_index.assert_awaited_once_with(dimension=768)
    assert reader.vector_index_ensured is True


@pytest.mark.asyncio
async def test_schema_uses_supporting_index_before_unique_constraints() -> None:
    """Memory schema creation uses FalkorDB's index plus Redis constraint flow."""
    client, _ = _new_falkor_stub()
    events: list[tuple[str, str, str | None]] = []

    async def ensure_supporting_index(label: str, prop: str) -> None:
        events.append(("index", label, prop))

    async def ensure_unique_constraint(label: str, prop: str) -> None:
        events.append(("constraint", label, prop))

    async def query(cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        events.append(("query", _compact(cypher), None))
        return []

    client.ensure_supporting_index = AsyncMock(side_effect=ensure_supporting_index)
    client.ensure_unique_constraint = AsyncMock(side_effect=ensure_unique_constraint)
    client.query = AsyncMock(side_effect=query)

    await client.ensure_memory_graph_schema()

    assert events[:4] == [
        ("index", "_Entity", "entity_key"),
        ("constraint", "_Entity", "entity_key"),
        ("index", "Memory", "memory_id"),
        ("constraint", "Memory", "memory_id"),
    ]
    index_queries = [
        event[1] for event in events if event[0] == "query" and "CREATE INDEX" in event[1]
    ]
    assert index_queries
    assert all("IF NOT EXISTS" not in cypher for cypher in index_queries)


@pytest.mark.asyncio
async def test_ensure_supporting_index_rewrites_index_ddl_and_catches_existing() -> None:
    """Supporting indexes use FalkorDB DDL and swallow already-indexed errors."""
    client, _ = _new_falkor_stub()
    _, FalkorQueryError = _load_falkor_symbols()
    client.query = AsyncMock(return_value=[])

    await client.ensure_supporting_index("_Entity", "entity_key")

    cypher = _compact(client.query.await_args.args[0])
    assert cypher == "CREATE INDEX FOR (n:_Entity) ON (n.entity_key)"
    assert "IF NOT EXISTS" not in cypher

    client.query = AsyncMock(side_effect=FalkorQueryError("node label already indexed"))
    await client.ensure_supporting_index("_Entity", "entity_key")


@pytest.mark.asyncio
async def test_ensure_unique_constraint_uses_redis_command_and_constraints_poll() -> None:
    """Unique constraints are created out-of-band and polled through db.constraints()."""
    client, execute_command = _new_falkor_stub()
    operational = [
        {
            "type": "UNIQUE",
            "label": "_Entity",
            "properties": ["entity_key"],
            "entitytype": "NODE",
            "status": "OPERATIONAL",
        }
    ]
    client.query = AsyncMock(return_value=operational)
    client._graph.ro_query = AsyncMock(return_value=operational)

    await client.ensure_unique_constraint("_Entity", "entity_key")

    command_text = " ".join(str(part) for part in execute_command.await_args.args)
    assert "GRAPH.CONSTRAINT" in command_text
    assert "CREATE" in command_text
    assert "gobby_kg" in command_text
    assert "UNIQUE" in command_text
    assert "NODE" in command_text
    assert "_Entity" in command_text
    assert "entity_key" in command_text

    poll_queries = [call.args[0] for call in client.query.await_args_list]
    poll_queries.extend(call.args[0] for call in client._graph.ro_query.await_args_list)
    assert "CALL db.constraints()" in poll_queries


@pytest.mark.asyncio
async def test_ensure_unique_constraint_raises_when_constraint_status_failed() -> None:
    """FAILED FalkorDB constraint builds surface as FalkorQueryError."""
    client, _ = _new_falkor_stub()
    _, FalkorQueryError = _load_falkor_symbols()
    failed = [
        {
            "type": "UNIQUE",
            "label": "_Entity",
            "properties": ["entity_key"],
            "entitytype": "NODE",
            "status": "FAILED",
        }
    ]
    client.query = AsyncMock(return_value=failed)
    client._graph.ro_query = AsyncMock(return_value=failed)

    with pytest.raises(FalkorQueryError, match="FAILED"):
        await client.ensure_unique_constraint("_Entity", "entity_key")


@pytest.mark.asyncio
async def test_vector_index_ddl_uses_falkor_options_and_required_dimension() -> None:
    """Vector index DDL has no Neo4j indexConfig and no hard-coded 1536 default."""
    FalkorClient, _ = _load_falkor_symbols()
    signature = inspect.signature(FalkorClient.ensure_vector_index)
    assert "dimension" in signature.parameters
    assert "dimensions" not in signature.parameters
    assert signature.parameters["dimension"].default is inspect.Parameter.empty

    client, _ = _new_falkor_stub()
    client.query = AsyncMock(return_value=[])

    await client.ensure_vector_index(dimension=768)

    cypher, params = client.query.await_args.args
    compact = _compact(cypher)
    assert "CREATE VECTOR INDEX FOR (n:_Entity) ON (n.embedding)" in compact
    assert "OPTIONS {dimension: $dim, similarityFunction: 'cosine'}" in compact
    assert params == {"dim": 768}
    assert "IF NOT EXISTS" not in compact
    assert "indexConfig" not in compact
    assert "1536" not in compact


@pytest.mark.asyncio
async def test_set_node_vector_uses_inline_vecf32_assignment() -> None:
    """Vector writes use SET n.embedding = vecf32(...) instead of Neo4j procedure calls."""
    client, _ = _new_falkor_stub()
    client.query = AsyncMock(return_value=[])

    await client.set_node_vector("entity-1", [0.1, 0.2, 0.3])

    cypher, params = client.query.await_args.args
    compact = _compact(cypher)
    assert "SET n.embedding = vecf32(" in compact
    assert "db.create.setNodeVectorProperty" not in compact
    assert params["entity_key"] == "entity-1"


@pytest.mark.asyncio
async def test_vector_search_uses_label_property_signature() -> None:
    """Vector queryNodes uses label and property arguments, not an index name."""
    client, _ = _new_falkor_stub()
    client.query = AsyncMock(return_value=[])

    await client.vector_search([0.1, 0.2], limit=2, min_score=0.4, project_id="proj-1")

    cypher, params = client.query.await_args.args
    compact = _compact(cypher)
    assert "CALL db.idx.vector.queryNodes('_Entity', 'embedding'," in compact
    assert "vecf32(" in compact
    assert "db.index.vector.queryNodes" not in compact
    assert "entity_embedding_index" not in compact
    assert params["candidate_limit"] >= 2
    assert params["min_score"] == 0.4


@pytest.mark.asyncio
async def test_memory_link_timestamps_use_unix_epoch_ms() -> None:
    """Memory MERGE timestamps use timestamp(), which FalkorDB returns as epoch ms."""
    client = AsyncMock()
    writer = KnowledgeGraphWriter(client)

    await writer.link_entities_to_memory([], memory_id="mem-1", project_id="proj-1")

    cypher = _compact(client.query.await_args.args[0])
    assert "timestamp()" in cypher
    assert "datetime()" not in cypher


@pytest.mark.asyncio
async def test_code_link_timestamps_use_unix_epoch_ms() -> None:
    """RELATES_TO_CODE writes use timestamp(), not datetime()."""
    client = AsyncMock()
    vector_store = AsyncMock()
    vector_store.search = AsyncMock(return_value=[("sym-1", 0.93)])
    linker = KnowledgeGraphCodeLinker(
        client,
        vector_store,
        code_link_min_score=0.8,
        code_symbol_collection_prefix="code_symbols_",
    )
    entity = _GraphEntity(
        entity_key="proj-1:python",
        name="Python",
        entity_type="tool",
        project_id="proj-1",
        normalized_name="python",
    )

    await linker.link_entities_to_code([entity], {"proj-1:python": [0.1]}, project_id="proj-1")

    cypher = _compact(client.query.await_args.args[0])
    assert "timestamp()" in cypher
    assert "datetime()" not in cypher


def test_daemon_python_source_has_no_apoc_procedure_fragments() -> None:
    """Daemon Python source should not contain APOC procedure fragments."""
    hits = []
    for path in Path("src/gobby").rglob("*.py"):
        if "apoc." in path.read_text():
            hits.append(str(path))

    assert hits == []

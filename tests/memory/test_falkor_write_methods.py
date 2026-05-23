"""Tests for FalkorClient write convenience methods."""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit


class FakeFalkorDB:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def select_graph(self, graph_name: str) -> object:
        self.graph_name = graph_name
        return object()


def _client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Create a FalkorClient with no real Redis connection."""
    fake_package = types.ModuleType("falkordb")
    fake_asyncio = types.ModuleType("falkordb.asyncio")
    fake_asyncio.FalkorDB = FakeFalkorDB
    monkeypatch.setitem(sys.modules, "falkordb", fake_package)
    monkeypatch.setitem(sys.modules, "falkordb.asyncio", fake_asyncio)

    from gobby.memory.falkor_client import FalkorClient

    return FalkorClient(host="127.0.0.1", port=16379, password="secret")


class TestMergeNode:
    """Tests for FalkorClient.merge_node()."""

    async def test_merge_node_generates_falkordb_cypher(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """merge_node uses stable _Entity identity and FalkorDB timestamp values."""
        client = _client(monkeypatch)
        client.query = AsyncMock(return_value=[])

        await client.merge_node(
            entity_key="josh",
            name="Josh",
            labels=["Person"],
            properties={"role": "engineer"},
        )

        client.query.assert_called_once()
        cypher, params = client.query.call_args.args
        assert "MERGE (n:_Entity {entity_key: $entity_key})" in cypher
        assert "MERGE (n:Person" not in cypher
        assert "SET n:Person" in cypher
        assert "ON CREATE SET" in cypher
        assert "ON MATCH SET" in cypher
        assert "timestamp()" in cypher
        assert "datetime()" not in cypher
        assert params["entity_key"] == "josh"
        assert params["props"]["name"] == "Josh"
        assert params["props"]["role"] == "engineer"

    async def test_merge_node_with_multiple_labels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """merge_node applies every validated label."""
        client = _client(monkeypatch)
        client.query = AsyncMock(return_value=[])

        await client.merge_node(
            entity_key="python",
            name="Python",
            labels=["Tool", "Language"],
        )

        cypher = client.query.call_args.args[0]
        assert "MERGE (n:_Entity {entity_key: $entity_key})" in cypher
        assert "SET n:Tool:Language" in cypher

    async def test_merge_node_keeps_entity_identity_across_type_changes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same entity_key with different type labels still merges through _Entity only."""
        client = _client(monkeypatch)
        client.query = AsyncMock(return_value=[])

        await client.merge_node(entity_key="gobby", name="Gobby", labels=["Project"])
        await client.merge_node(entity_key="gobby", name="Gobby", labels=["Tool"])

        cyphers = [call.args[0] for call in client.query.call_args_list]
        assert all("MERGE (n:_Entity {entity_key: $entity_key})" in cypher for cypher in cyphers)
        assert all("MERGE (n:Project" not in cypher for cypher in cyphers)
        assert all("MERGE (n:Tool" not in cypher for cypher in cyphers)
        assert "SET n:Project" in cyphers[0]
        assert "SET n:Tool" in cyphers[1]

    async def test_merge_node_ignores_entity_label_in_extra_labels(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_Entity is the identity label, not a dynamic classification label."""
        client = _client(monkeypatch)
        client.query = AsyncMock(return_value=[])

        await client.merge_node(entity_key="josh", name="Josh", labels=["Person", "_Entity"])

        cypher = client.query.call_args.args[0]
        assert "MERGE (n:_Entity {entity_key: $entity_key})" in cypher
        assert "SET n:Person" in cypher
        assert "SET n:_Entity" not in cypher

    async def test_merge_node_rejects_invalid_label(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Interpolated labels must remain Cypher identifiers."""
        client = _client(monkeypatch)

        with pytest.raises(ValueError, match="Invalid Cypher label"):
            await client.merge_node(
                entity_key="bad",
                name="Bad",
                labels=["Person) DETACH DELETE n"],
            )


class TestMergeRelationship:
    """Tests for FalkorClient.merge_relationship()."""

    async def test_merge_relationship_generates_cypher(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """merge_relationship matches source and target by entity_key."""
        client = _client(monkeypatch)
        client.query = AsyncMock(return_value=[])

        await client.merge_relationship(
            source_key="josh",
            target_key="gobby",
            rel_type="works_on",
            properties={"since": "2024"},
        )

        client.query.assert_called_once()
        cypher, params = client.query.call_args.args
        assert "MATCH (a:_Entity {entity_key: $source_key})" in cypher
        assert "MERGE (a)-[r:works_on]->(b)" in cypher
        assert "ON CREATE SET r += $props" in cypher
        assert "ON MATCH SET r += $props" in cypher
        assert params["source_key"] == "josh"
        assert params["target_key"] == "gobby"
        assert params["props"]["since"] == "2024"

    async def test_merge_relationship_sanitizes_rel_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LLM-provided relationship labels are normalized before interpolation."""
        client = _client(monkeypatch)
        client.query = AsyncMock(return_value=[])

        await client.merge_relationship("a", "b", "9 works-on")

        cypher = client.query.call_args.args[0]
        assert "MERGE (a)-[r:_9_works_on]->(b)" in cypher


class TestGraphCounts:
    """Tests for FalkorClient.get_graph_counts()."""

    async def test_get_graph_counts_reads_actual_falkordb_counts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _client(monkeypatch)
        client.query = AsyncMock(
            side_effect=[
                [{"total": 10}],
                [{"total": 3}],
                [{"total": 7}],
                [{"total": 0}],
                [{"total": 8}],
                [{"total": 5}],
                [{"total": 3}],
                [{"total": 0}],
            ]
        )

        result = await client.get_graph_counts(project_id="proj-1")

        assert result == {
            "graph": "gobby_kg",
            "project_id": "proj-1",
            "total_nodes": 10,
            "memory_nodes": 3,
            "entity_nodes": 7,
            "code_symbol_nodes": 0,
            "relationships": 8,
            "entity_relationships": 5,
            "mentioned_in_relationships": 3,
            "relates_to_code_relationships": 0,
        }
        first_cypher, first_params = client.query.call_args_list[0].args
        assert "MATCH (n)" in first_cypher
        assert "n.project_id = $project_id" in first_cypher
        assert first_params == {"project_id": "proj-1"}


class TestSetNodeVector:
    """Tests for FalkorClient.set_node_vector()."""

    async def test_set_node_vector_uses_vecf32_assignment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FalkorDB stores vectors with vecf32(), not Neo4j procedures."""
        client = _client(monkeypatch)
        client.query = AsyncMock(return_value=[])
        embedding = [0.1, 0.2, 0.3]

        await client.set_node_vector(entity_key="josh", embedding=embedding)

        client.query.assert_called_once()
        cypher, params = client.query.call_args.args
        assert "MATCH (n:_Entity {entity_key: $entity_key})" in cypher
        assert "SET n.embedding = vecf32($embedding)" in cypher
        assert "db.create.setNodeVectorProperty" not in cypher
        assert params["entity_key"] == "josh"
        assert params["embedding"] == embedding

    async def test_set_node_vector_with_custom_property(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """set_node_vector validates and interpolates the property name."""
        client = _client(monkeypatch)
        client.query = AsyncMock(return_value=[])

        await client.set_node_vector("josh", [0.1], property_name="custom_embedding")

        cypher = client.query.call_args.args[0]
        assert "SET n.custom_embedding = vecf32($embedding)" in cypher

    async def test_set_node_vector_rejects_invalid_property_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Property names cannot inject Cypher."""
        client = _client(monkeypatch)

        with pytest.raises(ValueError, match="Invalid Cypher property name"):
            await client.set_node_vector("carol", [0.5], property_name="bad'; DROP")

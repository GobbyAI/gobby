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
        """merge_node uses MERGE and FalkorDB timestamp values."""
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
        assert "MERGE (n:Person {entity_key: $entity_key})" in cypher
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
        assert "MERGE (n:Tool:Language {entity_key: $entity_key})" in cypher

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

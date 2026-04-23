"""Tests for Neo4jClient write convenience methods."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from gobby.memory.neo4j_client import Neo4jClient

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def client() -> Neo4jClient:
    """Create a Neo4jClient with no real connection."""
    return Neo4jClient(url="http://localhost:7474", auth="neo4j:password")


class TestMergeNode:
    """Tests for merge_node()."""

    async def test_merge_node_basic(self, client: Neo4jClient) -> None:
        """merge_node generates MERGE with ON CREATE/ON MATCH SET."""
        client.query = AsyncMock(return_value=[])

        await client.merge_node(
            entity_key="alice",
            name="Alice",
            labels=["Person"],
            properties={"age": 30},
        )

        client.query.assert_called_once()
        cypher = client.query.call_args[0][0]
        params = client.query.call_args[0][1]

        assert "MERGE" in cypher
        assert "ON CREATE SET" in cypher
        assert "ON MATCH SET" in cypher
        assert params["entity_key"] == "alice"
        assert params["props"]["name"] == "Alice"
        assert params["props"]["age"] == 30

    async def test_merge_node_sets_labels(self, client: Neo4jClient) -> None:
        """merge_node applies labels to the node."""
        client.query = AsyncMock(return_value=[])

        await client.merge_node(
            entity_key="bob",
            name="Bob",
            labels=["Person", "Developer"],
        )

        cypher = client.query.call_args[0][0]
        assert ":Person:Developer" in cypher

    async def test_merge_node_no_labels(self, client: Neo4jClient) -> None:
        """merge_node works without labels."""
        client.query = AsyncMock(return_value=[])

        await client.merge_node(entity_key="charlie", name="Charlie")

        cypher = client.query.call_args[0][0]
        assert "MERGE" in cypher
        params = client.query.call_args[0][1]
        assert params["entity_key"] == "charlie"
        assert params["props"]["name"] == "Charlie"

    async def test_merge_node_empty_properties(self, client: Neo4jClient) -> None:
        """merge_node with no properties still sets name."""
        client.query = AsyncMock(return_value=[])

        await client.merge_node(entity_key="diana", name="Diana", labels=["Entity"])

        params = client.query.call_args[0][1]
        assert params["entity_key"] == "diana"
        assert params["props"]["name"] == "Diana"


class TestMergeRelationship:
    """Tests for merge_relationship()."""

    async def test_merge_relationship_basic(self, client: Neo4jClient) -> None:
        """merge_relationship generates MATCH + MERGE for relationship."""
        client.query = AsyncMock(return_value=[])

        await client.merge_relationship("alice", "bob", "KNOWS", {"since": 2020})

        client.query.assert_called_once()
        cypher = client.query.call_args[0][0]
        params = client.query.call_args[0][1]

        assert "MATCH" in cypher
        assert "MERGE" in cypher
        assert "KNOWS" in cypher
        assert params["source_key"] == "alice"
        assert params["target_key"] == "bob"
        assert params["props"]["since"] == 2020

    async def test_merge_relationship_no_properties(self, client: Neo4jClient) -> None:
        """merge_relationship works without properties."""
        client.query = AsyncMock(return_value=[])

        await client.merge_relationship("alice", "bob", "FRIENDS")

        cypher = client.query.call_args[0][0]
        params = client.query.call_args[0][1]
        assert "FRIENDS" in cypher
        assert params["props"] == {}

    async def test_merge_relationship_sets_properties(self, client: Neo4jClient) -> None:
        """merge_relationship applies ON CREATE SET and ON MATCH SET."""
        client.query = AsyncMock(return_value=[])

        await client.merge_relationship("x", "y", "RELATED", {"weight": 0.5})

        cypher = client.query.call_args[0][0]
        assert "ON CREATE SET" in cypher
        assert "ON MATCH SET" in cypher


class TestSetNodeVector:
    """Tests for set_node_vector()."""

    async def test_set_node_vector(self, client: Neo4jClient) -> None:
        """set_node_vector calls db.create.setNodeVectorProperty."""
        client.query = AsyncMock(return_value=[])
        embedding = [0.1, 0.2, 0.3]

        await client.set_node_vector("alice", embedding)

        client.query.assert_called_once()
        cypher = client.query.call_args[0][0]
        params = client.query.call_args[0][1]

        assert "db.create.setNodeVectorProperty" in cypher
        assert params["entity_key"] == "alice"
        assert params["embedding"] == embedding

    async def test_set_node_vector_custom_property(self, client: Neo4jClient) -> None:
        """set_node_vector supports custom vector property name."""
        client.query = AsyncMock(return_value=[])

        await client.set_node_vector("bob", [0.5], property_name="custom_vec")

        cypher = client.query.call_args[0][0]
        assert "custom_vec" in cypher

    async def test_set_node_vector_rejects_invalid_property_name(self, client: Neo4jClient) -> None:
        """set_node_vector rejects property names with injection characters."""
        with pytest.raises(ValueError, match="Invalid Cypher property name"):
            await client.set_node_vector("carol", [0.5], property_name="bad'; DROP")

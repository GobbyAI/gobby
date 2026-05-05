"""Tests for Neo4jClient write convenience methods (merge_node, merge_relationship, set_node_vector)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from gobby.memory.neo4j_client import Neo4jClient

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def client() -> Neo4jClient:
    """Create a Neo4jClient with mocked HTTP transport."""
    return Neo4jClient(url="http://localhost:7474", auth="neo4j:password")


class TestMergeNode:
    """Tests for Neo4jClient.merge_node()."""

    async def test_merge_node_generates_correct_cypher(self, client: Neo4jClient) -> None:
        """merge_node calls query() with MERGE Cypher and correct params."""
        client.query = AsyncMock(return_value=[])

        await client.merge_node(
            entity_key="josh",
            name="Josh",
            labels=["Person"],
            properties={"role": "engineer"},
        )

        client.query.assert_called_once()
        cypher, params = client.query.call_args[0][0], client.query.call_args[0][1]
        assert "MERGE" in cypher
        assert "ON CREATE SET" in cypher
        assert "ON MATCH SET" in cypher
        assert params["entity_key"] == "josh"
        assert params["props"]["name"] == "Josh"
        assert params["props"]["role"] == "engineer"

    async def test_merge_node_with_multiple_labels(self, client: Neo4jClient) -> None:
        """merge_node handles multiple labels."""
        client.query = AsyncMock(return_value=[])

        await client.merge_node(
            entity_key="python",
            name="Python",
            labels=["Tool", "Language"],
            properties={},
        )

        client.query.assert_called_once()
        cypher = client.query.call_args[0][0]
        assert "Tool" in cypher
        assert "Language" in cypher

    async def test_merge_node_with_no_labels(self, client: Neo4jClient) -> None:
        """merge_node works with empty labels list."""
        client.query = AsyncMock(return_value=[])

        await client.merge_node(entity_key="unknown", name="Unknown", labels=[], properties={})

        client.query.assert_called_once()
        assert client.query.call_count == 1
        assert client.query.call_args is not None

    async def test_merge_node_with_no_properties(self, client: Neo4jClient) -> None:
        """merge_node works with empty properties."""
        client.query = AsyncMock(return_value=[])

        await client.merge_node(entity_key="gobby", name="Gobby", labels=["Project"], properties={})

        client.query.assert_called_once()
        params = client.query.call_args[0][1]
        assert params["entity_key"] == "gobby"
        assert params["props"]["name"] == "Gobby"

    async def test_merge_node_name_in_properties(self, client: Neo4jClient) -> None:
        """merge_node always sets name on the node."""
        client.query = AsyncMock(return_value=[])

        await client.merge_node(
            entity_key="josh",
            name="Josh",
            labels=["Person"],
            properties={"org": "Anthropic"},
        )

        params = client.query.call_args[0][1]
        assert params["entity_key"] == "josh"
        assert params["props"]["name"] == "Josh"


class TestMergeRelationship:
    """Tests for Neo4jClient.merge_relationship()."""

    async def test_merge_relationship_generates_correct_cypher(self, client: Neo4jClient) -> None:
        """merge_relationship matches source/target and creates relationship."""
        client.query = AsyncMock(return_value=[])

        await client.merge_relationship(
            source_key="josh",
            target_key="gobby",
            rel_type="works_on",
            properties={"since": "2024"},
        )

        client.query.assert_called_once()
        cypher, params = client.query.call_args[0][0], client.query.call_args[0][1]
        assert "MATCH" in cypher
        assert "MERGE" in cypher
        assert "works_on" in cypher
        assert params["source_key"] == "josh"
        assert params["target_key"] == "gobby"

    async def test_merge_relationship_without_properties(self, client: Neo4jClient) -> None:
        """merge_relationship works with no properties."""
        client.query = AsyncMock(return_value=[])

        await client.merge_relationship(
            source_key="gobby",
            target_key="python",
            rel_type="USES",
        )

        client.query.assert_called_once()
        cypher = client.query.call_args[0][0]
        assert "USES" in cypher

    async def test_merge_relationship_sets_properties(self, client: Neo4jClient) -> None:
        """merge_relationship sets properties on the relationship."""
        client.query = AsyncMock(return_value=[])

        await client.merge_relationship(
            source_key="josh",
            target_key="python",
            rel_type="uses",
            properties={"version": "3.13"},
        )

        cypher = client.query.call_args[0][0]
        params = client.query.call_args[0][1]
        assert "SET" in cypher
        assert params["props"]["version"] == "3.13"

    async def test_merge_relationship_preserves_rel_type(self, client: Neo4jClient) -> None:
        """merge_relationship uses rel_type as provided."""
        client.query = AsyncMock(return_value=[])

        await client.merge_relationship(
            source_key="a",
            target_key="b",
            rel_type="WORKS_ON",
        )

        cypher = client.query.call_args[0][0]
        assert "WORKS_ON" in cypher


class TestSetNodeVector:
    """Tests for Neo4jClient.set_node_vector()."""

    async def test_set_node_vector_calls_procedure(self, client: Neo4jClient) -> None:
        """set_node_vector calls db.create.setNodeVectorProperty."""
        client.query = AsyncMock(return_value=[])

        embedding = [0.1, 0.2, 0.3]
        await client.set_node_vector(
            entity_key="josh",
            embedding=embedding,
        )

        client.query.assert_called_once()
        cypher, params = client.query.call_args[0][0], client.query.call_args[0][1]
        assert "MATCH" in cypher
        assert "db.create.setNodeVectorProperty" in cypher
        assert params["entity_key"] == "josh"
        assert params["embedding"] == embedding

    async def test_set_node_vector_with_custom_property(self, client: Neo4jClient) -> None:
        """set_node_vector allows custom vector property name."""
        client.query = AsyncMock(return_value=[])

        await client.set_node_vector(
            entity_key="josh",
            embedding=[0.1],
            property_name="custom_embedding",
        )

        cypher = client.query.call_args[0][0]
        assert "custom_embedding" in cypher

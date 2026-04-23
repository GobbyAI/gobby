"""Tests for Neo4jClient vector index and search methods."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.memory.neo4j_client import (
    Neo4jClient,
    Neo4jConnectionError,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def client() -> Neo4jClient:
    """Create a Neo4jClient with mocked HTTP client."""
    c = Neo4jClient(url="http://localhost:7474", auth="neo4j:test")
    c._client = AsyncMock()
    return c


class TestEnsureVectorIndex:
    """Tests for ensure_vector_index."""

    async def test_creates_index_with_defaults(self, client: Neo4jClient) -> None:
        """ensure_vector_index sends correct Cypher for default params."""
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.json.return_value = {"data": {"fields": [], "values": []}}
        client._client.post = AsyncMock(return_value=mock_response)

        await client.ensure_vector_index()

        call_args = client._client.post.call_args
        body = call_args.kwargs.get("json") or call_args[1].get("json")
        assert "CREATE VECTOR INDEX entity_embedding_index IF NOT EXISTS" in body["statement"]
        assert "1536" in body["statement"]
        assert "cosine" in body["statement"]

    async def test_creates_index_with_custom_params(self, client: Neo4jClient) -> None:
        """ensure_vector_index accepts custom dimensions and similarity."""
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.json.return_value = {"data": {"fields": [], "values": []}}
        client._client.post = AsyncMock(return_value=mock_response)

        await client.ensure_vector_index(
            index_name="custom_index",
            dimensions=768,
            similarity="euclidean",
        )

        call_args = client._client.post.call_args
        body = call_args.kwargs.get("json") or call_args[1].get("json")
        assert "custom_index" in body["statement"]
        assert "768" in body["statement"]
        assert "euclidean" in body["statement"]

    async def test_validates_index_name(self, client: Neo4jClient) -> None:
        """ensure_vector_index rejects invalid index names."""
        with pytest.raises(ValueError, match="Invalid Cypher"):
            await client.ensure_vector_index(index_name="DROP INDEX; --")

    async def test_connection_error_propagates(self, client: Neo4jClient) -> None:
        """ensure_vector_index propagates connection errors."""
        import httpx

        client._client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(Neo4jConnectionError):
            await client.ensure_vector_index()


class TestVectorSearch:
    """Tests for vector_search."""

    async def test_returns_matching_entities(self, client: Neo4jClient) -> None:
        """vector_search returns entities with scores."""
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.json.return_value = {
            "data": {
                "fields": ["name", "labels", "score", "props"],
                "values": [
                    ["Python", ["Tool", "_Entity"], 0.95, {"entity_type": "tool"}],
                    ["FastAPI", ["Framework", "_Entity"], 0.82, {"entity_type": "framework"}],
                ],
            }
        }
        client._client.post = AsyncMock(return_value=mock_response)

        results = await client.vector_search(
            query_embedding=[0.1, 0.2, 0.3],
            limit=5,
            min_score=0.5,
        )

        assert len(results) == 2
        assert results[0]["name"] == "Python"
        assert results[0]["score"] == 0.95
        assert results[1]["name"] == "FastAPI"

    async def test_empty_results(self, client: Neo4jClient) -> None:
        """vector_search returns empty list when no matches."""
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.json.return_value = {"data": {"fields": [], "values": []}}
        client._client.post = AsyncMock(return_value=mock_response)

        results = await client.vector_search(
            query_embedding=[0.1, 0.2],
            min_score=0.9,
        )

        assert results == []

    async def test_filters_project_scoped_results_after_overfetch(
        self, client: Neo4jClient
    ) -> None:
        """vector_search should overfetch candidates, then trim after project filtering."""
        client.query = AsyncMock(
            return_value=[
                {
                    "entity_key": "other",
                    "name": "Other",
                    "entity_type": "tool",
                    "project_id": "proj-2",
                    "labels": ["_Entity"],
                    "score": 0.99,
                    "props": {},
                },
                {
                    "entity_key": "wanted-1",
                    "name": "Python",
                    "entity_type": "tool",
                    "project_id": "proj-1",
                    "labels": ["_Entity"],
                    "score": 0.95,
                    "props": {},
                },
                {
                    "entity_key": "wanted-2",
                    "name": "FastAPI",
                    "entity_type": "framework",
                    "project_id": "proj-1",
                    "labels": ["_Entity"],
                    "score": 0.91,
                    "props": {},
                },
            ]
        )

        results = await client.vector_search(
            query_embedding=[0.1, 0.2, 0.3],
            limit=1,
            min_score=0.5,
            project_id="proj-1",
        )

        assert [row["entity_key"] for row in results] == ["wanted-1"]
        client.query.assert_awaited_once()
        _, params = client.query.await_args.args
        assert params["candidate_limit"] > 1

    async def test_validates_index_name(self, client: Neo4jClient) -> None:
        """vector_search rejects invalid index names."""
        with pytest.raises(ValueError, match="Invalid Cypher"):
            await client.vector_search(
                query_embedding=[0.1],
                index_name="bad;name",
            )

    async def test_connection_error_propagates(self, client: Neo4jClient) -> None:
        """vector_search propagates connection errors."""
        import httpx

        client._client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(Neo4jConnectionError):
            await client.vector_search(query_embedding=[0.1, 0.2])

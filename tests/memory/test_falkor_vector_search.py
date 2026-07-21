"""Tests for FalkorClient vector index and vector search methods."""

from __future__ import annotations

import inspect
import sys
import types
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gobby.config.persistence import EmbeddingsConfig

pytestmark = pytest.mark.unit


class FakeFalkorDB:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def select_graph(self, graph_name: str) -> object:
        self.graph_name = graph_name
        return object()


def _client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Create a FalkorClient with mocked query execution."""
    fake_package = types.ModuleType("falkordb")
    fake_asyncio = types.ModuleType("falkordb.asyncio")
    fake_asyncio.FalkorDB = FakeFalkorDB
    monkeypatch.setitem(sys.modules, "falkordb", fake_package)
    monkeypatch.setitem(sys.modules, "falkordb.asyncio", fake_asyncio)

    from gobby.memory.falkor_client import FalkorClient

    c = FalkorClient(host="127.0.0.1", port=16379, password="secret")
    c.query = AsyncMock(return_value=[])
    return c


class TestEnsureVectorIndex:
    """Tests for ensure_vector_index."""

    def test_dimension_is_required_positional_kwarg(self) -> None:
        """The 1536 default is removed from the public signature."""
        from gobby.memory.falkor_client import FalkorClient

        signature = inspect.signature(FalkorClient.ensure_vector_index)
        assert list(signature.parameters) == ["self", "dimension", "similarity", "index_name"]
        assert signature.parameters["dimension"].default is inspect.Parameter.empty
        assert signature.parameters["similarity"].default == "cosine"
        assert signature.parameters["index_name"].default == "entity_embedding_index"

    async def test_ensure_vector_index_requires_dimension(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Calling without dimension should fail before issuing Cypher."""
        client = _client(monkeypatch)

        with pytest.raises(TypeError):
            await client.ensure_vector_index()
        client.query.assert_not_called()

    async def test_ensure_vector_index_uses_falkordb_ddl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Vector index DDL should use FalkorDB's native syntax."""
        client = _client(monkeypatch)

        await client.ensure_vector_index(dimension=768)

        client.query.assert_called_once()
        cypher = client.query.call_args.args[0]
        params = client.query.call_args.args[1]
        assert "CREATE VECTOR INDEX FOR (n:_Entity) ON (n.embedding)" in cypher
        assert "OPTIONS {dimension: $dim, similarityFunction: 'cosine'}" in cypher
        assert params == {"dim": 768}
        assert "IF NOT EXISTS" not in cypher
        assert "1536" not in cypher
        assert "vector.dimensions" not in cypher

    async def test_ensure_vector_index_accepts_embeddings_config_dim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Callers should pass EmbeddingsConfig.dim into the required dimension arg."""
        client = _client(monkeypatch)
        embedding_config = EmbeddingsConfig(dim=1024)

        await client.ensure_vector_index(dimension=embedding_config.dim)

        cypher, params = client.query.call_args.args
        assert "dimension: $dim" in cypher
        assert params == {"dim": 1024}

    async def test_ensure_vector_index_validates_index_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invalid index names should fail before query execution."""
        client = _client(monkeypatch)

        with pytest.raises(ValueError, match="Invalid Cypher index name"):
            await client.ensure_vector_index(dimension=768, index_name="DROP INDEX; --")


class TestVectorSearch:
    """Tests for vector_search."""

    async def test_vector_search_filters_project_before_exact_cosine_ranking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Project scope is applied before distance ranking and limiting."""
        client = _client(monkeypatch)
        client.query.return_value = [
            {
                "entity_key": "wanted",
                "name": "Python",
                "entity_type": "tool",
                "project_id": "proj-1",
                "labels": ["_Entity"],
                "score": 0.95,
                "props": {},
            }
        ]

        results = await client.vector_search(
            query_embedding=[0.1, 0.2, 0.3],
            limit=5,
            min_score=0.5,
            project_id="proj-1",
        )

        assert [row["entity_key"] for row in results] == ["wanted"]
        cypher, params = client.query.call_args.args
        assert "MATCH (node:_Entity)" in cypher
        assert "node.project_id = $project_id" in cypher
        assert "$include_global AND node.is_global = true" in cypher
        assert cypher.index("node.project_id = $project_id") < cypher.index("vec.cosineDistance")
        assert "CALL db.idx.vector.queryNodes" not in cypher
        assert "vecf32($embedding)" in cypher
        assert "ORDER BY distance ASC LIMIT $limit" in cypher
        assert params["embedding"] == [0.1, 0.2, 0.3]
        assert params["project_id"] == "proj-1"
        assert params["include_global"] is True
        assert params["limit"] == 5
        assert params["min_score"] == 0.5

    async def test_vector_search_converts_cosine_distance_to_similarity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exact cosine distance is converted to higher-is-better similarity."""
        client = _client(monkeypatch)
        client.query.return_value = []

        await client.vector_search(
            query_embedding=[0.1, 0.2, 0.3],
            limit=5,
            min_score=0.5,
            project_id="proj-1",
        )

        cypher, _params = client.query.call_args.args
        assert "vec.cosineDistance(node.embedding, vecf32($embedding)) AS distance" in cypher
        assert "(1.0 - distance) >= $min_score" in cypher
        assert "(1.0 - distance) AS score" in cypher

    async def test_project_match_survives_two_hundred_closer_foreign_neighbors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A global top-200 window cannot displace an in-scope result."""
        client = _client(monkeypatch)
        foreign_neighbors = [
            {
                "entity_key": f"foreign-{index}",
                "project_id": "proj-2",
                "distance": index / 1000,
            }
            for index in range(200)
        ]
        wanted = {"entity_key": "wanted", "project_id": "proj-1", "distance": 0.9}
        assert (
            wanted
            not in sorted([*foreign_neighbors, wanted], key=lambda row: row["distance"])[:200]
        )

        def execute_project_scoped_query(_cypher, params):
            scoped = [
                row
                for row in [*foreign_neighbors, wanted]
                if row["project_id"] == params["project_id"]
                or (params["include_global"] and row["project_id"] is None)
            ]
            return [
                {
                    "entity_key": row["entity_key"],
                    "project_id": row["project_id"],
                    "score": 1.0 - row["distance"],
                }
                for row in sorted(scoped, key=lambda row: row["distance"])[: params["limit"]]
            ]

        client.query.side_effect = execute_project_scoped_query

        results = await client.vector_search(
            query_embedding=[0.1, 0.2, 0.3],
            limit=1,
            min_score=0.0,
            project_id="proj-1",
        )

        assert [row["entity_key"] for row in results] == ["wanted"]
        cypher, params = client.query.call_args.args
        assert "node.project_id = $project_id" in cypher
        assert "$include_global AND node.is_global = true" in cypher
        assert "CALL db.idx.vector.queryNodes" not in cypher
        assert params["project_id"] == "proj-1"
        assert params["include_global"] is True
        assert params["limit"] == 1

    async def test_vector_search_returns_empty_for_non_positive_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No query should run for a non-positive limit."""
        client = _client(monkeypatch)

        assert await client.vector_search([0.1], limit=0) == []
        client.query.assert_not_called()

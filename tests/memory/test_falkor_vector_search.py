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
        assert "CREATE VECTOR INDEX FOR (n:_Entity) ON (n.embedding)" in cypher
        assert "OPTIONS {dimension: 768, similarityFunction: 'cosine'}" in cypher
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

        cypher = client.query.call_args.args[0]
        assert "dimension: 1024" in cypher

    async def test_ensure_vector_index_validates_index_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invalid index names should fail before query execution."""
        client = _client(monkeypatch)

        with pytest.raises(ValueError, match="Invalid Cypher index name"):
            await client.ensure_vector_index(dimension=768, index_name="DROP INDEX; --")


class TestVectorSearch:
    """Tests for vector_search."""

    async def test_vector_search_uses_falkordb_query_nodes_signature(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FalkorDB vector search uses label and property, not Neo4j index name."""
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
        assert "CALL db.idx.vector.queryNodes('_Entity', 'embedding'," in cypher
        assert "vecf32($embedding)" in cypher
        assert "db.index.vector.queryNodes" not in cypher
        assert params["embedding"] == [0.1, 0.2, 0.3]
        assert params["candidate_limit"] > 5
        assert params["min_score"] == 0.5

    async def test_vector_search_filters_project_after_overfetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Project filtering happens after overfetch so one wrong-project hit does not starve results."""
        client = _client(monkeypatch)
        client.query.return_value = [
            {"entity_key": "other", "project_id": "proj-2", "score": 0.99},
            {"entity_key": "wanted-1", "project_id": "proj-1", "score": 0.95},
            {"entity_key": "wanted-2", "project_id": "proj-1", "score": 0.91},
        ]

        results = await client.vector_search(
            query_embedding=[0.1, 0.2, 0.3],
            limit=1,
            min_score=0.5,
            project_id="proj-1",
        )

        assert [row["entity_key"] for row in results] == ["wanted-1"]

    async def test_vector_search_returns_empty_for_non_positive_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No query should run for a non-positive limit."""
        client = _client(monkeypatch)

        assert await client.vector_search([0.1], limit=0) == []
        client.query.assert_not_called()

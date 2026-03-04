"""Tests for KnowledgeGraphService graph search, _Entity labels, and MENTIONED_IN links."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.memory.neo4j_client import Neo4jConnectionError
from gobby.memory.services.knowledge_graph import (
    KnowledgeGraphService,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def mock_neo4j() -> AsyncMock:
    """Mock Neo4jClient."""
    client = AsyncMock()
    client.merge_node = AsyncMock(return_value=[])
    client.merge_relationship = AsyncMock(return_value=[])
    client.set_node_vector = AsyncMock(return_value=None)
    client.ensure_vector_index = AsyncMock()
    client.vector_search = AsyncMock(return_value=[])
    client.query = AsyncMock(return_value=[])
    return client


@pytest.fixture
def mock_llm() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_embed_fn() -> AsyncMock:
    return AsyncMock(return_value=[0.1, 0.2, 0.3])


@pytest.fixture
def mock_prompt_loader() -> MagicMock:
    loader = MagicMock()
    loader.render = MagicMock(return_value="rendered prompt")
    return loader


@pytest.fixture
def service(
    mock_neo4j: AsyncMock,
    mock_llm: AsyncMock,
    mock_embed_fn: AsyncMock,
    mock_prompt_loader: MagicMock,
) -> KnowledgeGraphService:
    return KnowledgeGraphService(
        neo4j_client=mock_neo4j,
        llm_provider=mock_llm,
        embed_fn=mock_embed_fn,
        prompt_loader=mock_prompt_loader,
    )


class TestEntityLabelAndMemoryLinkage:
    """Tests for _Entity label addition and MENTIONED_IN linkage."""

    async def test_add_to_graph_sets_entity_label(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        """add_to_graph adds _Entity label via SET after merge."""
        mock_llm.generate_json = AsyncMock(
            side_effect=[
                {"entities": [{"entity": "Josh", "entity_type": "person"}]},
                {"relations": []},
                {"relations_to_delete": []},
            ]
        )

        await service.add_to_graph("Josh is a developer")

        # Check that SET n:_Entity query was called
        entity_label_calls = [
            c for c in mock_neo4j.query.call_args_list if "SET n:_Entity" in str(c)
        ]
        assert len(entity_label_calls) >= 1

    async def test_add_to_graph_creates_mentioned_in_links(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        """add_to_graph creates Memory node and MENTIONED_IN relationships when memory_id provided."""
        mock_llm.generate_json = AsyncMock(
            side_effect=[
                {"entities": [{"entity": "Python", "entity_type": "tool"}]},
                {"relations": []},
                {"relations_to_delete": []},
            ]
        )

        await service.add_to_graph("Python is great", memory_id="mem-123")

        # Check Memory node was created
        memory_merge_calls = [
            c for c in mock_neo4j.query.call_args_list if "MERGE (m:Memory" in str(c)
        ]
        assert len(memory_merge_calls) >= 1

        # Check MENTIONED_IN link was created
        mentioned_calls = [c for c in mock_neo4j.query.call_args_list if "MENTIONED_IN" in str(c)]
        assert len(mentioned_calls) >= 1

    async def test_add_to_graph_no_mentioned_in_without_memory_id(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        """add_to_graph skips MENTIONED_IN when no memory_id is provided."""
        mock_llm.generate_json = AsyncMock(
            side_effect=[
                {"entities": [{"entity": "Python", "entity_type": "tool"}]},
                {"relations": []},
                {"relations_to_delete": []},
            ]
        )

        await service.add_to_graph("Python is great")

        # No MENTIONED_IN or Memory merge calls
        mentioned_calls = [
            c
            for c in mock_neo4j.query.call_args_list
            if "MENTIONED_IN" in str(c) or "MERGE (m:Memory" in str(c)
        ]
        assert len(mentioned_calls) == 0


class TestSearchEntitiesByVector:
    """Tests for search_entities_by_vector."""

    async def test_returns_entities_with_memory_ids(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
    ) -> None:
        """search_entities_by_vector returns entities with linked memory IDs."""
        mock_neo4j.vector_search = AsyncMock(
            return_value=[
                {"name": "Python", "labels": ["Tool", "_Entity"], "score": 0.9, "props": {}},
            ]
        )
        # Batch memory lookup via UNWIND (ensure_vector_index is directly mocked, not via query)
        mock_neo4j.query = AsyncMock(
            return_value=[
                {"entity_name": "Python", "memory_id": "mem-001"},
                {"entity_name": "Python", "memory_id": "mem-002"},
            ],
        )

        results = await service.search_entities_by_vector(
            query_embedding=[0.1, 0.2, 0.3],
            limit=5,
            min_score=0.5,
        )

        assert len(results) == 1
        assert results[0]["name"] == "Python"
        assert results[0]["score"] == 0.9
        assert "mem-001" in results[0]["memory_ids"]
        assert "mem-002" in results[0]["memory_ids"]

    async def test_returns_empty_when_no_matches(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
    ) -> None:
        """search_entities_by_vector returns empty list when no entities match."""
        mock_neo4j.vector_search = AsyncMock(return_value=[])

        results = await service.search_entities_by_vector(
            query_embedding=[0.1, 0.2],
        )

        assert results == []

    async def test_graceful_degradation_on_connection_error(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
    ) -> None:
        """search_entities_by_vector returns empty list on connection error."""
        mock_neo4j.vector_search = AsyncMock(side_effect=Neo4jConnectionError("refused"))

        results = await service.search_entities_by_vector(
            query_embedding=[0.1, 0.2],
        )

        assert results == []

    async def test_lazy_vector_index_creation(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
    ) -> None:
        """search_entities_by_vector creates index on first call, skips on second."""
        mock_neo4j.vector_search = AsyncMock(return_value=[])

        await service.search_entities_by_vector(query_embedding=[0.1])
        await service.search_entities_by_vector(query_embedding=[0.2])

        # ensure_vector_index should be called only once
        assert mock_neo4j.ensure_vector_index.call_count == 1


class TestFindRelatedMemoryIds:
    """Tests for find_related_memory_ids."""

    async def test_returns_memory_ids_from_traversal(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
    ) -> None:
        """find_related_memory_ids returns memory IDs via graph traversal."""
        mock_neo4j.query = AsyncMock(
            return_value=[
                {"memory_id": "mem-100"},
                {"memory_id": "mem-200"},
                {"memory_id": "mem-300"},
            ]
        )

        result = await service.find_related_memory_ids(
            entity_names=["Python", "FastAPI"],
            max_hops=2,
            limit=20,
        )

        assert result == ["mem-100", "mem-200", "mem-300"]

    async def test_returns_empty_for_empty_names(
        self,
        service: KnowledgeGraphService,
    ) -> None:
        """find_related_memory_ids returns empty for empty entity names."""
        result = await service.find_related_memory_ids(entity_names=[])
        assert result == []

    async def test_clamps_max_hops(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
    ) -> None:
        """find_related_memory_ids clamps max_hops to 1-3."""
        mock_neo4j.query = AsyncMock(return_value=[])

        # Upper bound: 10 → 3
        await service.find_related_memory_ids(entity_names=["A"], max_hops=10)
        call_args = mock_neo4j.query.call_args
        assert "*1..3" in call_args[0][0]

        # Lower bound: 0 → 1
        mock_neo4j.query.reset_mock()
        await service.find_related_memory_ids(entity_names=["A"], max_hops=0)
        call_args = mock_neo4j.query.call_args
        assert "*1..1" in call_args[0][0]

    async def test_graceful_degradation_on_connection_error(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
    ) -> None:
        """find_related_memory_ids returns empty on connection error."""
        mock_neo4j.query = AsyncMock(side_effect=Neo4jConnectionError("refused"))

        result = await service.find_related_memory_ids(entity_names=["Python"])

        assert result == []


class TestSearchGraphUpgraded:
    """Tests for the upgraded search_graph with vector search fallback."""

    async def test_uses_vector_search_first(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
        mock_embed_fn: AsyncMock,
    ) -> None:
        """search_graph tries vector search before substring."""
        mock_neo4j.vector_search = AsyncMock(
            return_value=[
                {"name": "Python", "labels": ["Tool"], "score": 0.9, "props": {}},
            ]
        )
        # Memory lookup returns empty (no MENTIONED_IN links)
        mock_neo4j.query = AsyncMock(return_value=[])

        results = await service.search_graph("programming language")

        assert len(results) == 1
        assert results[0]["name"] == "Python"
        mock_embed_fn.assert_called_with("programming language")

    async def test_falls_back_to_substring_on_vector_failure(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
        mock_embed_fn: AsyncMock,
    ) -> None:
        """search_graph falls back to substring when vector search fails."""
        mock_embed_fn.side_effect = Exception("Embedding service down")

        mock_neo4j.query = AsyncMock(
            return_value=[
                {"name": "Python", "labels": ["Tool"], "props": {}},
            ]
        )

        results = await service.search_graph("Python")

        assert len(results) == 1
        assert results[0]["name"] == "Python"

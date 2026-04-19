"""Tests for KnowledgeGraphService."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.memory.identity import entity_key
from gobby.memory.neo4j_client import Neo4jConnectionError
from gobby.memory.services.knowledge_graph import (
    Entity,
    KnowledgeGraphService,
    Relationship,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_neo4j() -> AsyncMock:
    """Mock Neo4jClient."""
    client = AsyncMock()
    client.merge_node = AsyncMock(return_value=[])
    client.merge_relationship = AsyncMock(return_value=[])
    client.set_node_vector = AsyncMock(return_value=None)
    client.get_entity_graph = AsyncMock(return_value={"entities": [], "relationships": []})
    client.get_entity_neighbors = AsyncMock(return_value={"entities": [], "relationships": []})
    client.query = AsyncMock(return_value=[])
    return client


@pytest.fixture
def mock_llm() -> AsyncMock:
    """Mock LLMProvider."""
    return AsyncMock()


@pytest.fixture
def mock_embed_fn() -> AsyncMock:
    """Mock embedding function."""
    return AsyncMock(return_value=[0.1, 0.2, 0.3])


@pytest.fixture
def mock_prompt_loader() -> MagicMock:
    """Mock PromptLoader."""
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
    """Create a KnowledgeGraphService with all mocked deps."""
    return KnowledgeGraphService(
        neo4j_client=mock_neo4j,
        llm_provider=mock_llm,
        embed_fn=mock_embed_fn,
        prompt_loader=mock_prompt_loader,
    )


# ===========================================================================
# Dataclass tests
# ===========================================================================


class TestEntity:
    """Tests for Entity dataclass."""

    def test_entity_creation(self) -> None:
        """Entity stores name and entity_type."""
        e = Entity(name="Josh", entity_type="person")
        assert e.name == "Josh"
        assert e.entity_type == "person"

    def test_entity_asdict(self) -> None:
        """Entity can be serialized to dict."""
        e = Entity(name="Python", entity_type="tool")
        d = asdict(e)
        assert d == {"name": "Python", "entity_type": "tool"}


class TestRelationship:
    """Tests for Relationship dataclass."""

    def test_relationship_creation(self) -> None:
        """Relationship stores source, target, relationship."""
        r = Relationship(source="Josh", target="Gobby", relationship="works_on")
        assert r.source == "Josh"
        assert r.target == "Gobby"
        assert r.relationship == "works_on"

    def test_relationship_asdict(self) -> None:
        """Relationship can be serialized to dict."""
        r = Relationship(source="A", target="B", relationship="uses")
        d = asdict(r)
        assert d == {"source": "A", "target": "B", "relationship": "uses"}


# ===========================================================================
# Write path: add_to_graph
# ===========================================================================


class TestAddToGraph:
    """Tests for KnowledgeGraphService.add_to_graph()."""

    async def test_ensure_graph_schema_serializes_concurrent_initialization(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
    ) -> None:
        """Concurrent callers should only run schema DDL once."""

        async def _ensure_once() -> None:
            await asyncio.sleep(0.01)

        mock_neo4j.ensure_memory_graph_schema = AsyncMock(side_effect=_ensure_once)

        await asyncio.gather(*[service._ensure_graph_schema() for _ in range(5)])

        mock_neo4j.ensure_memory_graph_schema.assert_awaited_once()
        assert service._graph_schema_ensured is True

    async def test_add_to_graph_extracts_entities(
        self,
        service: KnowledgeGraphService,
        mock_llm: AsyncMock,
        mock_prompt_loader: MagicMock,
    ) -> None:
        """add_to_graph calls LLM to extract entities from content."""
        mock_llm.generate_json = AsyncMock(
            side_effect=[
                # Entity extraction
                {"entities": [{"entity": "Josh", "entity_type": "person"}]},
                # Relationship extraction
                {"relations": []},
                # Delete relations (existing relations empty)
                {"relations_to_delete": []},
            ]
        )

        await service.add_to_graph("Josh works at Anthropic")

        # Verify entity extraction prompt was rendered
        mock_prompt_loader.render.assert_any_call(
            "memory/extract_entities",
            {"content": "Josh works at Anthropic"},
        )

    async def test_add_to_graph_extracts_relationships(
        self,
        service: KnowledgeGraphService,
        mock_llm: AsyncMock,
        mock_prompt_loader: MagicMock,
    ) -> None:
        """add_to_graph calls LLM to extract relationships between entities."""
        entities = [{"entity": "Josh", "entity_type": "person"}]
        mock_llm.generate_json = AsyncMock(
            side_effect=[
                {"entities": entities},
                {
                    "relations": [
                        {"source": "Josh", "relationship": "works_on", "destination": "Gobby"}
                    ]
                },
                {"relations_to_delete": []},
            ]
        )

        await service.add_to_graph("Josh works on Gobby")

        mock_prompt_loader.render.assert_any_call(
            "memory/extract_relations",
            {"content": "Josh works on Gobby", "entities": json.dumps(entities)},
        )

    async def test_add_to_graph_merges_nodes(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        """add_to_graph calls merge_node for each extracted entity."""
        mock_llm.generate_json = AsyncMock(
            side_effect=[
                {
                    "entities": [
                        {"entity": "Josh", "entity_type": "person"},
                        {"entity": "Python", "entity_type": "tool"},
                    ]
                },
                {"relations": []},
                {"relations_to_delete": []},
            ]
        )

        await service.add_to_graph("Josh uses Python")

        assert mock_neo4j.merge_node.call_count == 2
        # Check first call was for Josh
        first_call = mock_neo4j.merge_node.call_args_list[0]
        assert first_call.kwargs["name"] == "Josh"

    async def test_add_to_graph_merges_relationships(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        """add_to_graph calls merge_relationship for each extracted relationship."""
        mock_llm.generate_json = AsyncMock(
            side_effect=[
                {
                    "entities": [
                        {"entity": "Josh", "entity_type": "person"},
                        {"entity": "Python", "entity_type": "tool"},
                    ]
                },
                {
                    "relations": [
                        {"source": "Josh", "relationship": "uses", "destination": "Python"},
                    ]
                },
                {"relations_to_delete": []},
            ]
        )

        await service.add_to_graph("Josh uses Python")

        mock_neo4j.merge_relationship.assert_called_once()
        call_kwargs = mock_neo4j.merge_relationship.call_args.kwargs
        assert call_kwargs["source_key"] == entity_key(None, "Josh")
        assert call_kwargs["target_key"] == entity_key(None, "Python")
        assert call_kwargs["rel_type"] == "uses"

    async def test_add_to_graph_sets_embeddings(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
        mock_llm: AsyncMock,
        mock_embed_fn: AsyncMock,
    ) -> None:
        """add_to_graph sets embedding vectors on nodes."""
        mock_llm.generate_json = AsyncMock(
            side_effect=[
                {"entities": [{"entity": "Josh", "entity_type": "person"}]},
                {"relations": []},
                {"relations_to_delete": []},
            ]
        )

        await service.add_to_graph("Josh is a person")

        mock_embed_fn.assert_called()
        mock_neo4j.set_node_vector.assert_called_once()

    async def test_add_to_graph_deletes_outdated_relations(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        """add_to_graph deletes outdated relationships identified by LLM."""
        # Existing relations in Neo4j
        mock_neo4j.query = AsyncMock(
            return_value=[
                {"source": "Josh", "rel_type": "uses", "target": "Python 3.12"},
            ]
        )

        mock_llm.generate_json = AsyncMock(
            side_effect=[
                {
                    "entities": [
                        {"entity": "Josh", "entity_type": "person"},
                        {"entity": "Python 3.13", "entity_type": "tool"},
                    ]
                },
                {
                    "relations": [
                        {"source": "Josh", "relationship": "uses", "destination": "Python 3.13"},
                    ]
                },
                {
                    "relations_to_delete": [
                        {"source": "Josh", "relationship": "uses", "destination": "Python 3.12"},
                    ]
                },
            ]
        )

        await service.add_to_graph("Josh uses Python 3.13")

        # Should have called query to delete the outdated relation
        delete_calls = [c for c in mock_neo4j.query.call_args_list if "DELETE" in str(c)]
        assert len(delete_calls) >= 1

    async def test_add_to_graph_no_entities_returns_early(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        """add_to_graph returns early when no entities are extracted."""
        mock_llm.generate_json = AsyncMock(
            return_value={"entities": []},
        )

        await service.add_to_graph("nothing useful")

        mock_neo4j.merge_node.assert_not_called()


# ===========================================================================
# Read path
# ===========================================================================


class TestGetEntityGraph:
    """Tests for get_entity_graph read method."""

    async def test_get_entity_graph_delegates_to_client(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
    ) -> None:
        """get_entity_graph delegates to neo4j_client."""
        expected = {"entities": [{"name": "Josh"}], "relationships": []}
        mock_neo4j.get_entity_graph = AsyncMock(return_value=expected)

        result = await service.get_entity_graph(limit=100)

        assert result == expected
        mock_neo4j.get_entity_graph.assert_called_once_with(limit=100, project_id=None)


class TestGetEntityNeighbors:
    """Tests for get_entity_neighbors read method."""

    async def test_get_entity_neighbors_delegates(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
    ) -> None:
        """get_entity_neighbors delegates to neo4j_client."""
        expected = {"entities": [{"name": "Python"}], "relationships": []}
        mock_neo4j.get_entity_neighbors = AsyncMock(return_value=expected)

        result = await service.get_entity_neighbors("Josh")

        assert result == expected
        mock_neo4j.get_entity_neighbors.assert_called_once_with("Josh", project_id=None)


class TestSearchGraph:
    """Tests for search_graph read method."""

    async def test_search_graph_returns_matching_entities(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
    ) -> None:
        """search_graph queries Neo4j for entities matching the query."""
        mock_neo4j.query = AsyncMock(
            return_value=[
                {"name": "Python", "labels": ["Tool"], "score": 0.9},
            ]
        )

        result = await service.search_graph("programming language", limit=5)

        assert len(result) >= 1
        mock_neo4j.query.assert_called()


# ===========================================================================
# Graceful degradation
# ===========================================================================


class TestGracefulDegradation:
    """Tests for graceful behavior when Neo4j is unavailable."""

    async def test_get_entity_graph_returns_none_when_neo4j_down(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
    ) -> None:
        """get_entity_graph returns None when Neo4j is unreachable."""
        mock_neo4j.get_entity_graph = AsyncMock(side_effect=Neo4jConnectionError("refused"))

        result = await service.get_entity_graph()

        assert result is None

    async def test_get_entity_neighbors_returns_none_when_neo4j_down(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
    ) -> None:
        """get_entity_neighbors returns None when Neo4j is unreachable."""
        mock_neo4j.get_entity_neighbors = AsyncMock(side_effect=Neo4jConnectionError("refused"))

        result = await service.get_entity_neighbors("Josh")

        assert result is None

    async def test_add_to_graph_handles_neo4j_down(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        """add_to_graph logs warning but doesn't crash when Neo4j is down."""
        mock_llm.generate_json = AsyncMock(
            side_effect=[
                {"entities": [{"entity": "Josh", "entity_type": "person"}]},
                {"relations": []},
                {"relations_to_delete": []},
            ]
        )
        mock_neo4j.merge_node = AsyncMock(side_effect=Neo4jConnectionError("refused"))

        # Should not raise
        await service.add_to_graph("Josh is here")

    async def test_search_graph_returns_empty_when_neo4j_down(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
    ) -> None:
        """search_graph returns empty list when Neo4j is unreachable."""
        mock_neo4j.query = AsyncMock(side_effect=Neo4jConnectionError("refused"))

        result = await service.search_graph("test")

        assert result == []

    async def test_add_to_graph_handles_llm_failure(
        self,
        service: KnowledgeGraphService,
        mock_llm: AsyncMock,
        mock_neo4j: AsyncMock,
    ) -> None:
        """add_to_graph handles LLM extraction failure gracefully."""
        mock_llm.generate_json = AsyncMock(side_effect=Exception("LLM error"))

        # Should not raise
        await service.add_to_graph("some content")

        mock_neo4j.merge_node.assert_not_called()


# ===========================================================================
# Cross-graph linking: RELATES_TO_CODE
# ===========================================================================


@pytest.fixture
def mock_vector_store() -> AsyncMock:
    """Mock VectorStore for code symbol searches."""
    store = AsyncMock()
    store.search = AsyncMock(return_value=[])
    return store


@pytest.fixture
def service_with_vector_store(
    mock_neo4j: AsyncMock,
    mock_llm: AsyncMock,
    mock_embed_fn: AsyncMock,
    mock_prompt_loader: MagicMock,
    mock_vector_store: AsyncMock,
) -> KnowledgeGraphService:
    """KnowledgeGraphService with VectorStore for code linking tests."""
    return KnowledgeGraphService(
        neo4j_client=mock_neo4j,
        llm_provider=mock_llm,
        embed_fn=mock_embed_fn,
        prompt_loader=mock_prompt_loader,
        vector_store=mock_vector_store,
        code_link_min_score=0.82,
        code_symbol_collection_prefix="code_symbols_",
    )


def _stub_llm_for_entities(mock_llm: AsyncMock, entities: list[dict[str, str]]) -> None:
    """Configure mock LLM to return the given entities with no relationships."""
    mock_llm.generate_json = AsyncMock(
        side_effect=[
            {"entities": entities},
            {"relations": []},
            {"relations_to_delete": []},
        ]
    )


class TestRelatesToCode:
    """Tests for RELATES_TO_CODE cross-graph linking (Step 9)."""

    async def test_writes_edges_for_hits_above_threshold(
        self,
        service_with_vector_store: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
        mock_llm: AsyncMock,
        mock_vector_store: AsyncMock,
    ) -> None:
        """RELATES_TO_CODE edges are written when symbol matches exceed threshold."""
        _stub_llm_for_entities(mock_llm, [{"entity": "auth", "entity_type": "concept"}])
        mock_vector_store.search = AsyncMock(
            return_value=[("sym-uuid-1", 0.90), ("sym-uuid-2", 0.85)]
        )

        await service_with_vector_store.add_to_graph(
            "auth module", memory_id="mem-1", project_id="proj-1"
        )

        # Find the UNWIND RELATES_TO_CODE query call
        relates_calls = [c for c in mock_neo4j.query.call_args_list if "RELATES_TO_CODE" in str(c)]
        assert len(relates_calls) == 1
        call_args = relates_calls[0]
        links = (
            call_args.args[1]["links"]
            if len(call_args.args) > 1
            else call_args.kwargs.get("parameters", {}).get("links", [])
        )
        assert len(links) == 2
        assert links[0]["entity_key"] == entity_key("proj-1", "auth")
        assert links[0]["symbol_id"] == "sym-uuid-1"
        assert links[0]["score"] == 0.90

    async def test_filters_hits_below_threshold(
        self,
        service_with_vector_store: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
        mock_llm: AsyncMock,
        mock_vector_store: AsyncMock,
    ) -> None:
        """Hits below code_link_min_score are not written as edges."""
        _stub_llm_for_entities(mock_llm, [{"entity": "auth", "entity_type": "concept"}])
        mock_vector_store.search = AsyncMock(
            return_value=[("sym-uuid-1", 0.75), ("sym-uuid-2", 0.60)]
        )

        await service_with_vector_store.add_to_graph(
            "auth module", memory_id="mem-1", project_id="proj-1"
        )

        relates_calls = [c for c in mock_neo4j.query.call_args_list if "RELATES_TO_CODE" in str(c)]
        assert len(relates_calls) == 0

    async def test_skips_when_no_project_id(
        self,
        service_with_vector_store: KnowledgeGraphService,
        mock_llm: AsyncMock,
        mock_vector_store: AsyncMock,
    ) -> None:
        """Step 9 is skipped entirely when project_id is None."""
        _stub_llm_for_entities(mock_llm, [{"entity": "auth", "entity_type": "concept"}])

        await service_with_vector_store.add_to_graph("auth module", memory_id="mem-1")

        mock_vector_store.search.assert_not_called()

    async def test_skips_when_no_vector_store(
        self,
        service: KnowledgeGraphService,
        mock_llm: AsyncMock,
        mock_neo4j: AsyncMock,
    ) -> None:
        """Step 9 is skipped when service has no VectorStore."""
        _stub_llm_for_entities(mock_llm, [{"entity": "auth", "entity_type": "concept"}])

        await service.add_to_graph("auth module", memory_id="mem-1", project_id="proj-1")

        relates_calls = [c for c in mock_neo4j.query.call_args_list if "RELATES_TO_CODE" in str(c)]
        assert len(relates_calls) == 0

    async def test_graceful_noop_when_collection_missing(
        self,
        service_with_vector_store: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
        mock_llm: AsyncMock,
        mock_vector_store: AsyncMock,
    ) -> None:
        """Gracefully no-ops when Qdrant collection doesn't exist."""
        _stub_llm_for_entities(mock_llm, [{"entity": "auth", "entity_type": "concept"}])
        mock_vector_store.search = AsyncMock(
            side_effect=Exception("Collection code_symbols_proj-1 not found")
        )

        # Should not raise
        await service_with_vector_store.add_to_graph(
            "auth module", memory_id="mem-1", project_id="proj-1"
        )

        relates_calls = [c for c in mock_neo4j.query.call_args_list if "RELATES_TO_CODE" in str(c)]
        assert len(relates_calls) == 0

    async def test_uses_correct_collection_name(
        self,
        service_with_vector_store: KnowledgeGraphService,
        mock_llm: AsyncMock,
        mock_vector_store: AsyncMock,
    ) -> None:
        """Searches the correct Qdrant collection: prefix + project_id."""
        _stub_llm_for_entities(mock_llm, [{"entity": "auth", "entity_type": "concept"}])
        mock_vector_store.search = AsyncMock(return_value=[])

        await service_with_vector_store.add_to_graph(
            "auth module", memory_id="mem-1", project_id="my-project"
        )

        mock_vector_store.search.assert_called_once()
        call_kwargs = mock_vector_store.search.call_args.kwargs
        assert call_kwargs["collection_name"] == "code_symbols_my-project"


# ===========================================================================
# Project-ID scoping on Memory nodes
# ===========================================================================


class TestMemoryNodeProjectIdScoping:
    """Tests for project_id scoping on :Memory nodes."""

    async def test_link_entities_sets_project_id_on_memory_node(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
    ) -> None:
        """_link_entities_to_memory sets project_id on the Memory node."""
        entities = service._normalize_entities(
            [Entity(name="Auth", entity_type="concept")],
            project_id="proj-A",
        )
        await service._link_entities_to_memory(entities, "mem-1", project_id="proj-A")

        # First query call is the MERGE for Memory node
        merge_call = mock_neo4j.query.call_args_list[0]
        cypher = merge_call.args[0]
        params = merge_call.args[1]
        assert "ON CREATE SET m.project_id" in cypher
        assert "ON MATCH SET m.project_id = coalesce($project_id, m.project_id)" in cypher
        assert params["project_id"] == "proj-A"
        assert params["memory_id"] == "mem-1"

    async def test_link_entities_with_none_project_id(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
    ) -> None:
        """_link_entities_to_memory with project_id=None doesn't overwrite existing value."""
        entities = service._normalize_entities(
            [Entity(name="Auth", entity_type="concept")],
            project_id=None,
        )
        await service._link_entities_to_memory(entities, "mem-1", project_id=None)

        merge_call = mock_neo4j.query.call_args_list[0]
        params = merge_call.args[1]
        # coalesce(NULL, m.project_id) preserves existing value
        assert params["project_id"] is None

    async def test_add_to_graph_passes_project_id_to_link(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        """add_to_graph passes project_id through to _link_entities_to_memory."""
        mock_llm.generate_json = AsyncMock(
            side_effect=[
                {"entities": [{"entity": "Auth", "entity_type": "concept"}]},
                {"relations": []},
                {"relations_to_delete": []},
            ]
        )

        await service.add_to_graph("auth module", memory_id="mem-1", project_id="proj-B")

        # Find the Memory MERGE query (has ON CREATE SET m.project_id)
        memory_merges = [
            c for c in mock_neo4j.query.call_args_list if "ON CREATE SET m.project_id" in str(c)
        ]
        assert len(memory_merges) == 1
        assert memory_merges[0].args[1]["project_id"] == "proj-B"

    async def test_search_entities_by_vector_filters_by_project_id(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
    ) -> None:
        """search_entities_by_vector passes project_id filter to memory lookup query."""
        mock_neo4j.vector_search = AsyncMock(
            return_value=[
                {
                    "entity_key": entity_key("proj-A", "Auth"),
                    "name": "Auth",
                    "entity_type": "concept",
                    "project_id": "proj-A",
                    "labels": ["Concept"],
                    "score": 0.9,
                }
            ]
        )
        mock_neo4j.ensure_vector_index = AsyncMock()

        await service.search_entities_by_vector(
            query_embedding=[0.1, 0.2],
            project_id="proj-A",
        )

        # Find the MENTIONED_IN query
        mem_queries = [c for c in mock_neo4j.query.call_args_list if "MENTIONED_IN" in str(c)]
        assert len(mem_queries) == 1
        cypher = mem_queries[0].args[0]
        params = mem_queries[0].args[1]
        assert "m.project_id = $project_id" in cypher
        assert params["project_id"] == "proj-A"

    async def test_find_related_memory_ids_filters_by_project_id(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
    ) -> None:
        """find_related_memory_ids passes project_id filter to traversal query."""
        mock_neo4j.query = AsyncMock(return_value=[{"memory_id": "mem-1"}])

        await service.find_related_memory_ids(
            entity_keys=[entity_key("proj-A", "Auth")],
            project_id="proj-A",
        )

        call = mock_neo4j.query.call_args_list[0]
        cypher = call.args[0]
        params = call.args[1]
        assert "m.project_id = $project_id" in cypher
        assert params["project_id"] == "proj-A"


class TestRemoveMemoryFromGraph:
    """Tests for remove_memory_from_graph."""

    async def test_remove_memory_from_graph_deletes_node(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
    ) -> None:
        """remove_memory_from_graph issues DETACH DELETE on the Memory node."""
        await service.remove_memory_from_graph("mem-1")

        delete_calls = [c for c in mock_neo4j.query.call_args_list if "DETACH DELETE m" in c.args[0]]
        assert len(delete_calls) == 1
        cypher = delete_calls[0].args[0]
        params = delete_calls[0].args[1]
        assert "DETACH DELETE" in cypher
        assert "memory_id: $memory_id" in cypher
        assert params["memory_id"] == "mem-1"

    async def test_remove_memory_from_graph_nonexistent_is_noop(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
    ) -> None:
        """remove_memory_from_graph on non-existent ID doesn't raise."""
        mock_neo4j.query.return_value = []
        await service.remove_memory_from_graph("nonexistent")
        # No error raised — MATCH...DETACH DELETE is a no-op on zero matches

    async def test_remove_memory_from_graph_neo4j_unreachable(
        self,
        service: KnowledgeGraphService,
        mock_neo4j: AsyncMock,
    ) -> None:
        """remove_memory_from_graph logs warning when Neo4j is unreachable."""
        mock_neo4j.query.side_effect = Neo4jConnectionError("connection refused")
        # Should not raise
        await service.remove_memory_from_graph("mem-1")

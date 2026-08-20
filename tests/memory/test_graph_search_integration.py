"""Tests for KnowledgeGraphService graph search, type labels, and MENTIONED_IN links."""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.persistence import MemoryKnowledgeGraphConfig
from gobby.memory.falkor_client import FalkorConnectionError, FalkorTimeoutError
from gobby.memory.identity import entity_key
from gobby.memory.services.knowledge_graph import (
    KnowledgeGraphService,
)
from gobby.storage.projects import PERSONAL_PROJECT_ID

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _neighbor_row(source_key: str, related_entity_key: str) -> dict[str, str]:
    return {"source_key": source_key, "related_entity_key": related_entity_key}


@pytest.fixture
def mock_falkor() -> AsyncMock:
    """Mock FalkorDBClient."""
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
def mock_feature_config() -> MemoryKnowledgeGraphConfig:
    return MemoryKnowledgeGraphConfig()


@pytest.fixture
def service(
    mock_falkor: AsyncMock,
    mock_llm: AsyncMock,
    mock_embed_fn: AsyncMock,
    mock_prompt_loader: MagicMock,
    mock_feature_config: MemoryKnowledgeGraphConfig,
) -> KnowledgeGraphService:
    return KnowledgeGraphService(
        falkor_client=mock_falkor,
        llm_service=mock_llm,
        feature_config=mock_feature_config,
        embed_fn=mock_embed_fn,
        prompt_loader=mock_prompt_loader,
    )


class TestEntityLabelAndMemoryLinkage:
    """Tests for entity type labels and MENTIONED_IN linkage."""

    async def test_add_to_graph_sets_entity_label(
        self,
        service: KnowledgeGraphService,
        mock_falkor: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        """add_to_graph passes type labels while _Entity identity is client-owned."""
        mock_llm.call_json_feature = AsyncMock(
            side_effect=[
                {"entities": [{"entity": "Josh", "entity_type": "person"}]},
                {"relations": []},
                {"relation_ids_to_delete": []},
            ]
        )

        await service.add_to_graph("Josh is a developer")

        merge_call = mock_falkor.merge_node.call_args
        assert merge_call is not None
        assert merge_call.kwargs["labels"] == ["Person"]

    async def test_add_to_graph_creates_mentioned_in_links(
        self,
        service: KnowledgeGraphService,
        mock_falkor: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        """add_to_graph creates Memory node and MENTIONED_IN relationships when memory_id provided."""
        mock_llm.call_json_feature = AsyncMock(
            side_effect=[
                {"entities": [{"entity": "Python", "entity_type": "tool"}]},
                {"relations": []},
                {"relation_ids_to_delete": []},
            ]
        )

        await service.add_to_graph("Python is great", memory_id="mem-123")

        # Check Memory node was created
        memory_merge_calls = [
            c for c in mock_falkor.query.call_args_list if "MERGE (m:Memory" in str(c)
        ]
        assert len(memory_merge_calls) >= 1

        # Check MENTIONED_IN link was created
        mentioned_calls = [c for c in mock_falkor.query.call_args_list if "MENTIONED_IN" in str(c)]
        assert len(mentioned_calls) >= 1

    async def test_add_to_graph_no_mentioned_in_without_memory_id(
        self,
        service: KnowledgeGraphService,
        mock_falkor: AsyncMock,
        mock_llm: AsyncMock,
    ) -> None:
        """add_to_graph skips MENTIONED_IN when no memory_id is provided."""
        mock_llm.call_json_feature = AsyncMock(
            side_effect=[
                {"entities": [{"entity": "Python", "entity_type": "tool"}]},
                {"relations": []},
                {"relation_ids_to_delete": []},
            ]
        )

        await service.add_to_graph("Python is great")

        # No MENTIONED_IN or Memory merge calls
        mentioned_calls = [
            c
            for c in mock_falkor.query.call_args_list
            if "MENTIONED_IN" in str(c) or "MERGE (m:Memory" in str(c)
        ]
        assert len(mentioned_calls) == 0


class TestSearchEntitiesByVector:
    """Tests for search_entities_by_vector."""

    async def test_returns_entities_with_memory_ids(
        self,
        service: KnowledgeGraphService,
        mock_falkor: AsyncMock,
    ) -> None:
        """search_entities_by_vector returns entities with linked memory IDs."""
        mock_falkor.vector_search = AsyncMock(
            return_value=[
                {
                    "entity_key": entity_key(PERSONAL_PROJECT_ID, "Python"),
                    "name": "Python",
                    "entity_type": "tool",
                    "project_id": None,
                    "labels": ["Tool", "_Entity"],
                    "score": 0.9,
                    "props": {},
                },
            ]
        )
        # Batch memory lookup via UNWIND (ensure_vector_index is directly mocked, not via query)
        mock_falkor.query = AsyncMock(
            return_value=[
                {"entity_key": entity_key(PERSONAL_PROJECT_ID, "Python"), "memory_id": "mem-001"},
                {"entity_key": entity_key(PERSONAL_PROJECT_ID, "Python"), "memory_id": "mem-002"},
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
        cypher = mock_falkor.query.call_args.args[0]
        params = mock_falkor.query.call_args.args[1]
        assert "ORDER BY m.updated_at DESC LIMIT $memory_link_limit" in cypher
        assert params["memory_link_limit"] == 20

    async def test_returns_empty_when_no_matches(
        self,
        service: KnowledgeGraphService,
        mock_falkor: AsyncMock,
    ) -> None:
        """search_entities_by_vector returns empty list when no entities match."""
        mock_falkor.vector_search = AsyncMock(return_value=[])

        results = await service.search_entities_by_vector(
            query_embedding=[0.1, 0.2],
        )

        assert results == []

    async def test_graceful_degradation_on_connection_error(
        self,
        service: KnowledgeGraphService,
        mock_falkor: AsyncMock,
    ) -> None:
        """search_entities_by_vector returns empty list on connection error."""
        mock_falkor.vector_search = AsyncMock(side_effect=FalkorConnectionError("refused"))

        results = await service.search_entities_by_vector(
            query_embedding=[0.1, 0.2],
        )

        assert results == []

    async def test_lazy_vector_index_creation(
        self,
        service: KnowledgeGraphService,
        mock_falkor: AsyncMock,
    ) -> None:
        """search_entities_by_vector creates index on first call, skips on second."""
        mock_falkor.vector_search = AsyncMock(return_value=[])

        await service.search_entities_by_vector(query_embedding=[0.1])
        await service.search_entities_by_vector(query_embedding=[0.2])

        # ensure_vector_index should be called only once
        assert mock_falkor.ensure_vector_index.call_count == 1


class TestFindRelatedMemoryIds:
    """Tests for find_related_memory_ids."""

    async def test_returns_memory_ids_from_traversal(
        self,
        service: KnowledgeGraphService,
        mock_falkor: AsyncMock,
    ) -> None:
        """find_related_memory_ids returns memory IDs via graph traversal."""
        python_key = entity_key(PERSONAL_PROJECT_ID, "Python")
        fastapi_key = entity_key(PERSONAL_PROJECT_ID, "FastAPI")
        mock_falkor.query = AsyncMock(
            side_effect=[
                [
                    _neighbor_row(python_key, entity_key(PERSONAL_PROJECT_ID, "Django")),
                    _neighbor_row(fastapi_key, entity_key(PERSONAL_PROJECT_ID, "Starlette")),
                ],
                [
                    {"memory_id": "mem-100"},
                    {"memory_id": "mem-200"},
                    {"memory_id": "mem-300"},
                ],
            ]
        )

        result = await service.find_related_memory_ids(
            entity_keys=[
                python_key,
                fastapi_key,
            ],
            max_hops=1,
            limit=20,
        )

        assert result.memory_ids == ["mem-100", "mem-200", "mem-300"]

    async def test_traversal_query_is_bounded_entity_to_entity(
        self,
        service: KnowledgeGraphService,
        mock_falkor: AsyncMock,
    ) -> None:
        """Traversal uses bounded _Entity-to-_Entity neighbor expansion."""
        mock_falkor.query = AsyncMock(return_value=[])

        await service.find_related_memory_ids(
            entity_keys=[entity_key(PERSONAL_PROJECT_ID, "Python")],
            max_hops=2,
        )

        cypher = mock_falkor.query.call_args.args[0]
        params = mock_falkor.query.call_args.args[1]
        assert "[*1.." not in cypher
        assert "UNWIND $source_keys AS source_key" in cypher
        assert "(start:_Entity {entity_key: source_key})-[r]-(neighbor:_Entity)" in cypher
        assert "NOT (type(r) IN $excluded_relationship_types)" in cypher
        assert params["excluded_relationship_types"] == ["MENTIONED_IN", "RELATES_TO_CODE"]
        assert params["source_keys"] == [entity_key(PERSONAL_PROJECT_ID, "Python")]
        assert "neighbor_limit" not in params
        assert "coalesce(r.weight, 1.0) AS edge_weight" in cypher
        assert "r.updated_at AS updated_at" in cypher

    async def test_caps_seed_entities(
        self,
        service: KnowledgeGraphService,
        mock_falkor: AsyncMock,
    ) -> None:
        """find_related_memory_ids caps traversal seed entities."""
        mock_falkor.query = AsyncMock(return_value=[])
        keys = [entity_key(PERSONAL_PROJECT_ID, f"Entity{i}") for i in range(10)]

        await service.find_related_memory_ids(entity_keys=keys, max_hops=1)

        assert mock_falkor.query.await_count == 1
        assert mock_falkor.query.call_args.args[1]["source_keys"] == keys[:8]

    async def test_caps_neighbors_before_memory_lookup(
        self,
        service: KnowledgeGraphService,
        mock_falkor: AsyncMock,
    ) -> None:
        """Neighbor rows are capped before related memory lookup."""
        source_key = entity_key(PERSONAL_PROJECT_ID, "Python")
        neighbor_rows = [
            _neighbor_row(source_key, entity_key(PERSONAL_PROJECT_ID, f"Neighbor{i}"))
            for i in range(12)
        ]
        mock_falkor.query = AsyncMock(side_effect=[neighbor_rows, []])

        await service.find_related_memory_ids(
            entity_keys=[entity_key(PERSONAL_PROJECT_ID, "Python")],
            max_hops=1,
            limit=20,
        )

        neighbor_params = mock_falkor.query.call_args_list[0].args[1]
        memory_params = mock_falkor.query.call_args_list[1].args[1]
        assert neighbor_params["source_keys"] == [entity_key(PERSONAL_PROJECT_ID, "Python")]
        assert "neighbor_limit" not in neighbor_params
        assert len(memory_params["entity_keys"]) == 8

    async def test_project_filters_apply_to_traversal_and_memory_lookup(
        self,
        service: KnowledgeGraphService,
        mock_falkor: AsyncMock,
    ) -> None:
        """Project filters apply to start entity, related entity, and memory nodes."""
        auth_key = entity_key("proj-A", "Auth")
        mock_falkor.query = AsyncMock(
            side_effect=[
                [_neighbor_row(auth_key, entity_key("proj-A", "AuthService"))],
                [{"memory_id": "mem-100"}],
            ]
        )

        await service.find_related_memory_ids(
            entity_keys=[auth_key],
            max_hops=1,
            project_id="proj-A",
        )

        neighbor_cypher = mock_falkor.query.call_args_list[0].args[0]
        neighbor_params = mock_falkor.query.call_args_list[0].args[1]
        memory_cypher = mock_falkor.query.call_args_list[1].args[0]
        memory_params = mock_falkor.query.call_args_list[1].args[1]
        assert "start.project_id = $project_id" in neighbor_cypher
        assert "neighbor.project_id = $project_id" in neighbor_cypher
        assert "e.project_id = $project_id" in memory_cypher
        assert "m.project_id = $project_id" in memory_cypher
        assert neighbor_params["project_id"] == "proj-A"
        assert memory_params["project_id"] == "proj-A"

    async def test_circuit_breaker_counts_socket_timeouts(
        self,
        service: KnowledgeGraphService,
        mock_falkor: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_falkor.query = AsyncMock(
            side_effect=FalkorTimeoutError("FalkorDB read timed out after 15s: socket")
        )
        logger_name = "gobby.memory.services.knowledge_graph.reader"

        with caplog.at_level(logging.WARNING, logger=logger_name):
            result = await service.find_related_memory_ids(
                entity_keys=[entity_key(PERSONAL_PROJECT_ID, "Python")]
            )

        assert result.memory_ids == []
        warnings = [
            record
            for record in caplog.records
            if record.name == logger_name and "graph traversal timed out" in record.getMessage()
        ]
        assert len(warnings) == 1
        assert warnings[0].__dict__["consecutive_timeouts"] == 1

    async def test_circuit_breaker_skips_after_repeated_query_timeouts(
        self,
        service: KnowledgeGraphService,
        mock_falkor: AsyncMock,
    ) -> None:
        """Repeated Falkor query timeouts temporarily skip related expansion."""
        clock = [0.0]
        mock_falkor.query = AsyncMock(side_effect=Exception("Query timed out"))

        with patch(
            "gobby.memory.services.knowledge_graph.reader.time.monotonic",
            side_effect=lambda: clock[0],
        ):
            for _ in range(3):
                result = await service.find_related_memory_ids(
                    entity_keys=[entity_key(PERSONAL_PROJECT_ID, "Python")]
                )
                assert result.memory_ids == []

            assert mock_falkor.query.await_count == 3
            mock_falkor.query.reset_mock()

            result = await service.find_related_memory_ids(
                entity_keys=[entity_key(PERSONAL_PROJECT_ID, "Python")]
            )
            assert result.memory_ids == []
            mock_falkor.query.assert_not_awaited()

            clock[0] = 61.0
            python_key = entity_key(PERSONAL_PROJECT_ID, "Python")
            mock_falkor.query = AsyncMock(
                side_effect=[
                    [_neighbor_row(python_key, entity_key(PERSONAL_PROJECT_ID, "Django"))],
                    [{"memory_id": "mem-100"}],
                ]
            )

            result = await service.find_related_memory_ids(
                entity_keys=[python_key],
                max_hops=1,
            )

        assert result.memory_ids == ["mem-100"]

    async def test_configured_deadline_trips_breaker_and_rate_limits_warning(
        self,
        service: KnowledgeGraphService,
        mock_falkor: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        timeout_seconds = 0.001

        async def blocked_query(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
            await asyncio.Event().wait()
            return []

        mock_falkor.query = AsyncMock(side_effect=blocked_query)
        logger_name = "gobby.memory.services.knowledge_graph.reader"

        with caplog.at_level(logging.WARNING, logger=logger_name):
            for _ in range(3):
                result = await service.find_related_memory_ids(
                    entity_keys=[entity_key(PERSONAL_PROJECT_ID, "Python")],
                    timeout_seconds=timeout_seconds,
                )
                assert result.memory_ids == []

            assert mock_falkor.query.await_count == 3

            result = await service.find_related_memory_ids(
                entity_keys=[entity_key(PERSONAL_PROJECT_ID, "Python")],
                timeout_seconds=timeout_seconds,
            )
            assert result.memory_ids == []
            assert mock_falkor.query.await_count == 3

            reader = service._reader
            reader._traversal_disabled_until = 0.0
            reader._last_traversal_warning_at = time.monotonic() - 61.0
            result = await service.find_related_memory_ids(
                entity_keys=[entity_key(PERSONAL_PROJECT_ID, "Python")],
                timeout_seconds=timeout_seconds,
            )

        assert result.memory_ids == []
        assert mock_falkor.query.await_count == 4
        warnings = [
            record
            for record in caplog.records
            if record.name == logger_name and "graph traversal timed out" in record.getMessage()
        ]
        assert len(warnings) == 2
        assert warnings[0].__dict__["effective_timeout_seconds"] == timeout_seconds
        assert warnings[0].__dict__["consecutive_timeouts"] == 1
        assert warnings[0].__dict__["cooldown_seconds"] == 60.0
        assert warnings[0].__dict__["suppressed_warnings"] == 0
        assert warnings[1].__dict__["consecutive_timeouts"] == 4
        assert warnings[1].__dict__["suppressed_warnings"] == 2

    async def test_clamps_max_hops(
        self,
        service: KnowledgeGraphService,
        mock_falkor: AsyncMock,
    ) -> None:
        """find_related_memory_ids clamps max_hops to 1-3."""
        key_a = entity_key(PERSONAL_PROJECT_ID, "A")
        key_b = entity_key(PERSONAL_PROJECT_ID, "B")
        key_c = entity_key(PERSONAL_PROJECT_ID, "C")
        mock_falkor.query = AsyncMock(
            side_effect=[
                [_neighbor_row(key_a, key_b)],
                [_neighbor_row(key_b, key_c)],
                [_neighbor_row(key_c, entity_key(PERSONAL_PROJECT_ID, "D"))],
                [],
            ]
        )

        await service.find_related_memory_ids(entity_keys=[key_a], max_hops=10)
        neighbor_queries = mock_falkor.query.call_args_list[:3]
        assert len(neighbor_queries) == 3
        assert all("[*1.." not in call.args[0] for call in neighbor_queries)

        mock_falkor.query = AsyncMock(
            side_effect=[
                [_neighbor_row(key_a, key_b)],
                [],
            ]
        )
        await service.find_related_memory_ids(entity_keys=[key_a], max_hops=0)
        neighbor_queries = mock_falkor.query.call_args_list[:1]
        assert len(neighbor_queries) == 1
        assert "[*1.." not in neighbor_queries[0].args[0]

    async def test_graceful_degradation_on_connection_error(
        self,
        service: KnowledgeGraphService,
        mock_falkor: AsyncMock,
    ) -> None:
        """find_related_memory_ids returns empty on connection error."""
        mock_falkor.query = AsyncMock(side_effect=FalkorConnectionError("refused"))

        result = await service.find_related_memory_ids(
            entity_keys=[entity_key(PERSONAL_PROJECT_ID, "Python")]
        )

        assert result.memory_ids == []

    async def test_returns_empty_for_empty_names(
        self,
        service: KnowledgeGraphService,
    ) -> None:
        """find_related_memory_ids returns empty for empty entity names."""
        result = await service.find_related_memory_ids(entity_keys=[])
        assert result.memory_ids == []


class TestSearchGraphUpgraded:
    """Tests for the upgraded search_graph with vector search fallback."""

    async def test_uses_vector_search_first(
        self,
        service: KnowledgeGraphService,
        mock_falkor: AsyncMock,
        mock_embed_fn: AsyncMock,
    ) -> None:
        """search_graph tries vector search before substring."""
        mock_falkor.vector_search = AsyncMock(
            return_value=[
                {
                    "entity_key": entity_key("project-a", "Python"),
                    "name": "Python",
                    "entity_type": "tool",
                    "project_id": "project-a",
                    "labels": ["Tool"],
                    "score": 0.9,
                    "props": {},
                },
                {
                    "entity_key": entity_key("project-b", "Secret"),
                    "name": "Secret",
                    "entity_type": "tool",
                    "project_id": "project-b",
                    "labels": ["Tool"],
                    "score": 0.95,
                    "props": {},
                },
            ]
        )
        # Memory lookup returns empty (no MENTIONED_IN links)
        mock_falkor.query = AsyncMock(return_value=[])

        results = await service.search_graph("programming language", project_id="project-a")

        assert len(results) == 1
        assert results[0]["name"] == "Python"
        mock_embed_fn.assert_called_with("programming language", is_query=True)
        mock_falkor.vector_search.assert_awaited_once_with(
            query_embedding=mock_embed_fn.return_value,
            limit=10,
            min_score=0.3,
            project_id="project-a",
            include_global=True,
        )

    async def test_falls_back_to_substring_on_vector_failure(
        self,
        service: KnowledgeGraphService,
        mock_falkor: AsyncMock,
        mock_embed_fn: AsyncMock,
    ) -> None:
        """search_graph falls back to substring when vector search fails."""
        mock_embed_fn.side_effect = Exception("Embedding service down")

        mock_falkor.query = AsyncMock(
            return_value=[
                {
                    "name": "Python",
                    "project_id": "project-a",
                    "labels": ["Tool"],
                    "props": {},
                },
                {
                    "name": "Secret",
                    "project_id": "project-b",
                    "labels": ["Tool"],
                    "props": {},
                },
            ]
        )

        results = await service.search_graph("Python", project_id="project-a")

        assert len(results) == 1
        assert results[0]["name"] == "Python"
        query, params = mock_falkor.query.await_args.args
        assert "n.project_id = $project_id" in query
        assert params["project_id"] == "project-a"
        assert params["include_global"] is True

"""Phase 2 FalkorDB wiring contract tests for memory services."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.manager import MemoryManager
from gobby.runner_init import services

pytestmark = pytest.mark.unit


def _mock_llm_service(provider: AsyncMock | None = None) -> MagicMock:
    llm_service = MagicMock()
    llm_service.get_provider_for_feature = MagicMock(
        return_value=(provider or AsyncMock(), "haiku", None)
    )
    return llm_service


def test_memory_manager_constructor_uses_falkordb_kwargs() -> None:
    """MemoryManager exposes FalkorDB connection kwargs and drops Neo4j kwargs."""
    signature = inspect.signature(MemoryManager)

    assert "falkordb_host" in signature.parameters
    assert "falkordb_port" in signature.parameters
    assert "falkordb_password" in signature.parameters
    assert "falkordb_graph_name" in signature.parameters
    assert "falkordb_graph_search" in signature.parameters
    assert "falkordb_graph_min_score" in signature.parameters
    assert "falkordb_rrf_k" in signature.parameters
    assert "neo4j_url" not in signature.parameters
    assert "neo4j_auth" not in signature.parameters


def test_memory_manager_constructs_knowledge_graph_service_with_falkor_client() -> None:
    """Configured graph search wires FalkorClient into KnowledgeGraphService."""
    falkor_client = AsyncMock()
    llm_provider = AsyncMock()
    llm_service = _mock_llm_service(llm_provider)
    vector_store = AsyncMock()
    embed_fn = AsyncMock(return_value=[0.1, 0.2])

    with (
        patch("gobby.memory.manager.FalkorClient", create=True) as falkor_cls,
        patch("gobby.memory.manager.KnowledgeGraphService") as kg_service_cls,
    ):
        falkor_cls.return_value = falkor_client
        manager = MemoryManager(
            db=MagicMock(),
            config=MemoryConfig(),
            llm_service=llm_service,
            vector_store=vector_store,
            embed_fn=embed_fn,
            falkordb_host="127.0.0.1",
            falkordb_port=16379,
            falkordb_password="secret",
            falkordb_graph_name="gobby_kg",
            falkordb_graph_search=True,
            falkordb_graph_min_score=0.42,
            falkordb_rrf_k=77,
            embedding_dim=768,
            collection_prefix="code_symbols_",
        )

    falkor_cls.assert_called_once_with(
        host="127.0.0.1",
        port=16379,
        password="secret",
        graph_name="gobby_kg",
    )
    call_kwargs = kg_service_cls.call_args.kwargs
    assert call_kwargs["falkor_client"] is falkor_client
    assert "neo4j_client" not in call_kwargs
    assert call_kwargs["llm_provider"] is llm_provider
    assert manager.falkor_client is falkor_client


def test_memory_manager_clear_graph_clients_clears_every_falkor_reference() -> None:
    """Graph disablement clears manager, search service, and indexing service references."""
    with (
        patch("gobby.memory.manager.FalkorClient", create=True) as falkor_cls,
        patch("gobby.memory.manager.KnowledgeGraphService") as kg_service_cls,
    ):
        falkor_cls.return_value = AsyncMock()
        kg_service_cls.return_value = MagicMock()
        manager = MemoryManager(
            db=MagicMock(),
            config=MemoryConfig(),
            llm_service=_mock_llm_service(),
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
            falkordb_host="127.0.0.1",
            falkordb_port=16379,
            falkordb_password="secret",
            falkordb_graph_name="gobby_kg",
        )

    assert manager.falkor_client is not None
    assert manager.kg_service is not None

    manager.clear_graph_clients()

    assert manager.falkor_client is None
    assert manager.kg_service is None
    assert manager._search_service._kg_service is None
    assert manager._indexing_service._kg_service is None


def test_runner_memory_stack_uses_canonical_falkordb_enablement_and_kwargs() -> None:
    """Runner init gates FalkorDB construction and passes FalkorConfig fields."""
    runner = SimpleNamespace(
        config=SimpleNamespace(
            memory=MemoryConfig(),
            embeddings=SimpleNamespace(
                dim=768,
                model="nomic-embed-text",
                api_key=None,
                api_base=None,
            ),
            databases=SimpleNamespace(
                qdrant=SimpleNamespace(
                    url="http://qdrant:6333", api_key=None, collection_prefix="code_"
                ),
                falkordb=SimpleNamespace(
                    host="127.0.0.1",
                    port=16379,
                    requirepass="secret",
                    graph_name="gobby_kg",
                    graph_search=True,
                    graph_min_score=0.45,
                    rrf_k=71,
                ),
            ),
        ),
        database=MagicMock(),
        db_executor=SimpleNamespace(run=AsyncMock()),
        llm_service=None,
        secret_store=MagicMock(),
        vector_store=None,
        memory_manager=None,
    )

    with (
        patch("gobby.runner_init.services.VectorStore") as vector_store_cls,
        patch("gobby.runner_init.services.MemoryManager") as memory_manager_cls,
        patch("gobby.runner_init.services.is_falkordb_enabled", create=True) as enabled,
    ):
        vector_store_cls.return_value = MagicMock()
        enabled.return_value = True

        services._init_memory_stack(runner)

    enabled.assert_called_once_with(runner.config.databases)
    memory_manager_cls.assert_called_once()
    kwargs = memory_manager_cls.call_args.kwargs
    assert kwargs["falkordb_host"] == "127.0.0.1"
    assert kwargs["falkordb_port"] == 16379
    assert kwargs["falkordb_password"] == "secret"
    assert kwargs["falkordb_graph_name"] == "gobby_kg"
    assert kwargs["falkordb_graph_search"] is True
    assert kwargs["falkordb_graph_min_score"] == 0.45
    assert kwargs["falkordb_rrf_k"] == 71
    assert "neo4j_url" not in kwargs
    assert "neo4j_auth" not in kwargs

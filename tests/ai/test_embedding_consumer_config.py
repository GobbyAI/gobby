"""Shared embedding configuration reaches memory and MCP tool consumers."""

import asyncio
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.ai.embeddings import EmbeddingService
from gobby.config.app import DaemonConfig
from gobby.runner import GobbyRunner
from gobby.runner_init.services import MemoryServiceBundle, _build_memory_services

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "model,dim", [("nomic-embed-text", 768), ("qwen3-embedding:0.6b-q8_0", 1024)]
)
async def test_memory_and_tools_follow_single_embedding_config(model: str, dim: int) -> None:
    config = DaemonConfig(
        embeddings={"model": model, "dim": dim, "api_base": "http://127.0.0.1:1234/v1"},
        memory_backup={"enabled": False},
    )
    runner = MagicMock()
    runner.machine_id = "00000000-0000-0000-0000-000000000001"
    runner.secret_store.get.return_value = None
    with (
        patch("gobby.runner_init.services.EmbeddingGenerationState"),
        patch("gobby.runner_init.services._ManagedEmbeddingLease", autospec=True),
        patch("gobby.runner_init.services.VectorStore") as vector_store,
        patch("gobby.runner_init.services.MemoryManager") as memory_manager,
        patch("gobby.runner_init.services.build_daemon_text_generation_service"),
        patch("gobby.runner_init.services.create_llm_service"),
    ):
        prepared = _build_memory_services(
            cast(GobbyRunner, runner), config, asyncio.get_running_loop(), {}, 1
        )

    bundle = prepared.value
    assert isinstance(bundle, MemoryServiceBundle)
    embed_fn = memory_manager.call_args.kwargs["embed_fn"]
    service = embed_fn.__self__
    assert isinstance(service, EmbeddingService)
    assert (service.model, service.dim) == (model, dim)
    assert service.api_base == config.embeddings.api_base
    assert memory_manager.call_args.kwargs["embedding_dim"] == dim
    assert vector_store.call_args.kwargs["embedding_dim"] == dim
    tool_service = bundle.semantic_search._embedding_service
    assert (tool_service.model, tool_service.dim) == (model, dim)
    assert tool_service.api_base == config.embeddings.api_base

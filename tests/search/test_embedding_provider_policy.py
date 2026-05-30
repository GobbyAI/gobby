"""Regression tests for explicit embedding provider selection."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from gobby.search.embeddings import (
    EmbeddingGenerationError,
    clear_cache,
    generate_embedding,
    is_embedding_configured,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
    clear_cache()
    yield
    clear_cache()


def _make_openai_client(dim: int = 1536) -> AsyncMock:
    mock_client = AsyncMock()

    @dataclass
    class FakeItem:
        embedding: list[float]

    @dataclass
    class FakeResponse:
        data: list[FakeItem]

    async def fake_create(model: str, input: list[str]) -> FakeResponse:
        return FakeResponse([FakeItem([0.1] * dim) for _ in input])

    mock_client.embeddings.create = AsyncMock(side_effect=fake_create)
    return mock_client


@pytest.mark.asyncio
async def test_local_nomic_without_api_base_never_calls_openai_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local model without api_base fails before any cloud-capable SDK call."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-used")

    with (
        patch("openai.AsyncOpenAI") as mock_openai,
        pytest.raises(EmbeddingGenerationError, match="embeddings.api_base") as exc_info,
    ):
        await generate_embedding("hello", model="nomic-embed-text")

    assert "nomic-embed-text" in str(exc_info.value)
    assert "api_base=None" in str(exc_info.value)
    mock_openai.assert_not_called()


@pytest.mark.asyncio
async def test_lmstudio_nomic_without_api_base_never_calls_openai_sdk() -> None:
    """LM Studio's Nomic model id must also name a local endpoint explicitly."""
    with (
        patch("openai.AsyncOpenAI") as mock_openai,
        pytest.raises(EmbeddingGenerationError, match="embeddings.api_base"),
    ):
        await generate_embedding(
            "hello",
            model="text-embedding-nomic-embed-text-v1.5@f16",
        )

    mock_openai.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_openai_embedding_config_allows_cloud_call() -> None:
    """OpenAI cloud remains valid when model and api_key are explicit."""
    mock_client = _make_openai_client(dim=1536)

    with patch("openai.AsyncOpenAI", return_value=mock_client) as mock_openai:
        result = await generate_embedding(
            "hello",
            model="text-embedding-3-small",
            api_key="sk-test",
            expected_dim=1536,
        )

    assert len(result) == 1536
    mock_openai.assert_called_once_with(base_url=None, api_key="sk-test")
    mock_client.embeddings.create.assert_awaited_once()
    assert mock_client.embeddings.create.await_args.kwargs["model"] == "text-embedding-3-small"


@pytest.mark.asyncio
async def test_explicit_openai_prefix_is_stripped_for_cloud_api() -> None:
    """The explicit openai/ prefix selects cloud without being sent to OpenAI."""
    mock_client = _make_openai_client(dim=1536)

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        await generate_embedding(
            "hello",
            model="openai/text-embedding-3-small",
            api_key="sk-test",
            expected_dim=1536,
        )

    assert mock_client.embeddings.create.await_args.kwargs["model"] == "text-embedding-3-small"


def test_embedding_configured_requires_api_base_for_local_even_with_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-used")

    assert is_embedding_configured(model="nomic-embed-text", api_base=None, api_key=None) is False
    assert (
        is_embedding_configured(
            model="text-embedding-nomic-embed-text-v1.5@f16",
            api_base=None,
            api_key=None,
        )
        is False
    )
    assert (
        is_embedding_configured(
            model="text-embedding-nomic-embed-text-v1.5@f16",
            api_base="http://localhost:1234/v1",
            api_key=None,
        )
        is True
    )
    assert (
        is_embedding_configured(
            model="text-embedding-3-small",
            api_base=None,
            api_key="sk-test",
        )
        is True
    )

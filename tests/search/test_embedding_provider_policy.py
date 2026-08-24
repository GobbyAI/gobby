"""Regression tests for explicit embedding provider selection."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gobby.ai.embeddings import (
    EmbeddingGenerationError,
)
from gobby.ai.embeddings import (
    _clear_embedding_cache as clear_cache,
)
from gobby.ai.embeddings import (
    _generate_embedding as generate_embedding,
)
from gobby.ai.embeddings import (
    _is_embedding_configured as is_embedding_configured,
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
        index: int

    @dataclass
    class FakeResponse:
        data: list[FakeItem]

    async def fake_create(model: str, input: list[str]) -> SimpleNamespace:
        response = FakeResponse([FakeItem([0.1] * dim, index) for index, _ in enumerate(input)])
        return SimpleNamespace(parse=lambda: response)

    mock_client.embeddings.with_raw_response.create = AsyncMock(side_effect=fake_create)
    return mock_client


@pytest.mark.asyncio
async def test_local_nomic_without_api_base_never_calls_openai_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local model without api_base fails before any cloud-capable SDK call."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-used")

    with (
        patch("openai.AsyncOpenAI") as mock_openai,
        pytest.raises(EmbeddingGenerationError, match="embedding API base") as exc_info,
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
        pytest.raises(EmbeddingGenerationError, match="embedding API base"),
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
    mock_client.embeddings.with_raw_response.create.assert_awaited_once()
    assert (
        mock_client.embeddings.with_raw_response.create.await_args.kwargs["model"]
        == "text-embedding-3-small"
    )


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

    assert (
        mock_client.embeddings.with_raw_response.create.await_args.kwargs["model"]
        == "text-embedding-3-small"
    )


@pytest.mark.asyncio
async def test_local_endpoint_with_configured_key_uses_it_as_bearer_credential() -> None:
    """A configured embedding api_key reaches the SDK client for local endpoints.

    Regression for gobby-cli#719: gcode routes query embeddings through the
    daemon's /api/embeddings proxy; when LM Studio requires an API token the
    configured key must be the client credential (the SDK sends it as
    ``Authorization: Bearer <key>``), not the local placeholder.
    """
    mock_client = _make_openai_client(dim=768)

    with patch("openai.AsyncOpenAI", return_value=mock_client) as mock_openai:
        result = await generate_embedding(
            "hello",
            model="nomic-embed-text",
            api_base="http://localhost:1234/v1",
            api_key="lm-studio-token",
            expected_dim=768,
        )

    assert len(result) == 768
    mock_openai.assert_called_once_with(
        base_url="http://localhost:1234/v1", api_key="lm-studio-token"
    )
    mock_client.embeddings.with_raw_response.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_local_endpoint_without_key_falls_back_to_placeholder_credential() -> None:
    """Without a configured key, local endpoints get the documented placeholder.

    Pins the ``api_key or "unused"`` fallback so an auth failure against a
    token-protected LM Studio is attributable to missing configuration, not
    to the daemon dropping a configured key (gobby-cli#719).
    """
    mock_client = _make_openai_client(dim=768)

    with patch("openai.AsyncOpenAI", return_value=mock_client) as mock_openai:
        result = await generate_embedding(
            "hello world",
            model="nomic-embed-text",
            api_base="http://localhost:1234/v1",
            expected_dim=768,
        )

    assert len(result) == 768
    mock_openai.assert_called_once_with(base_url="http://localhost:1234/v1", api_key="unused")


@pytest.mark.asyncio
async def test_local_provider_prefixed_model_is_stripped_for_local_endpoint() -> None:
    """Local provider selectors are not sent to OpenAI-compatible embedding APIs."""
    mock_client = _make_openai_client(dim=768)

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        await generate_embedding(
            "hello",
            model="local:ollama/nomic-embed-text",
            api_base="http://localhost:11434/v1",
            expected_dim=768,
        )

    assert (
        mock_client.embeddings.with_raw_response.create.await_args.kwargs["model"]
        == "nomic-embed-text"
    )


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

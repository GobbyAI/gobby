"""Tests for embedding dimension validation at the API boundary."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest

import gobby.search.embeddings as embeddings_mod
from gobby.search.embeddings import clear_cache, generate_embedding

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    """Reset global embedding cache and reload cooldown between tests."""
    clear_cache()
    embeddings_mod._last_reload_attempt = 0.0
    yield
    clear_cache()
    embeddings_mod._last_reload_attempt = 0.0


def _make_mock_client(dim: int) -> AsyncMock:
    """Create a mock AsyncOpenAI client returning vectors of the requested size."""
    mock_client = AsyncMock()

    async def fake_create(model: str, input: list[str]):
        class FakeItem:
            def __init__(self, embedding: list[float]):
                self.embedding = embedding

        class FakeResponse:
            def __init__(self, items: list[FakeItem]):
                self.data = items

        items = []
        for text in input:
            vec = [0.0] * dim
            vec[0] = hash(text) % 1000 / 1000.0
            items.append(FakeItem(vec))
        return FakeResponse(items)

    mock_client.embeddings.create = fake_create
    return mock_client


def _make_evicting_client(dim: int) -> AsyncMock:
    """Fail once with model eviction, then succeed with the requested vector size."""
    from openai import BadRequestError

    mock_client = AsyncMock()
    call_count = 0

    async def fake_create(model: str, input: list[str]):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise BadRequestError(
                message="No models loaded. Please load a model.",
                response=AsyncMock(status_code=400, headers={}),
                body=None,
            )

        class FakeItem:
            def __init__(self, embedding: list[float]):
                self.embedding = embedding

        class FakeResponse:
            def __init__(self, items: list[FakeItem]):
                self.data = items

        return FakeResponse([FakeItem([0.1] * dim) for _ in input])

    mock_client.embeddings.create = fake_create
    return mock_client


@pytest.mark.asyncio
async def test_expected_dim_match_succeeds() -> None:
    """Matching configured and returned dimensions should succeed."""
    mock_client = _make_mock_client(dim=768)

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        result = await generate_embedding(
            "hello",
            model="test-model",
            api_base="http://localhost:1234/v1",
            expected_dim=768,
        )

    assert len(result) == 768


@pytest.mark.asyncio
async def test_expected_dim_mismatch_raises() -> None:
    """A provider dimension mismatch should fail immediately with a clear error."""
    mock_client = _make_mock_client(dim=768)

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        with pytest.raises(RuntimeError, match="expected_dim=1024") as exc_info:
            await generate_embedding(
                "hello",
                model="test-model",
                api_base="http://localhost:1234/v1",
                expected_dim=1024,
            )

    assert "actual_dim=768" in str(exc_info.value)
    assert "model=test-model" in str(exc_info.value)
    assert "api_base=http://localhost:1234/v1" in str(exc_info.value)


@pytest.mark.asyncio
async def test_expected_dim_mismatch_after_reload_raises() -> None:
    """Dimension validation should also run on the post-reload retry path."""
    mock_client = _make_evicting_client(dim=768)

    with (
        patch("openai.AsyncOpenAI", return_value=mock_client),
        patch("gobby.cli.services.try_autoload_embedding_model", return_value=True),
    ):
        with pytest.raises(RuntimeError, match="actual_dim=768"):
            await generate_embedding(
                "hello",
                model="test-model",
                api_base="http://localhost:1234/v1",
                expected_dim=1024,
            )


@pytest.mark.asyncio
async def test_stale_cache_entry_is_refetched_for_new_expected_dim() -> None:
    """Cached vectors with the wrong dimension should be treated as a cache miss."""
    initial_client = _make_mock_client(dim=768)
    replacement_client = _make_mock_client(dim=1024)

    with patch("openai.AsyncOpenAI", return_value=initial_client):
        cached = await generate_embedding("hello", model="test-model", expected_dim=768)

    replacement_calls = 0
    original_create = replacement_client.embeddings.create

    async def tracking_create(model: str, input: list[str]):
        nonlocal replacement_calls
        replacement_calls += 1
        return await original_create(model=model, input=input)

    replacement_client.embeddings.create = tracking_create

    with patch("openai.AsyncOpenAI", return_value=replacement_client):
        refreshed = await generate_embedding("hello", model="test-model", expected_dim=1024)

    assert len(cached) == 768
    assert len(refreshed) == 1024
    assert replacement_calls == 1


@pytest.mark.asyncio
async def test_expected_dim_none_preserves_back_compat() -> None:
    """Callers that do not opt in to validation should keep current behavior."""
    mock_client = _make_mock_client(dim=768)

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        result = await generate_embedding("hello", model="test-model")

    assert len(result) == 768


def test_validate_embeddings_dim_checks_every_vector() -> None:
    """Mixed-dimension batches should fail at the first offending vector."""
    with pytest.raises(RuntimeError, match="index=1") as exc_info:
        embeddings_mod._validate_embeddings_dim(
            [[0.1, 0.2], [0.3]],
            expected_dim=2,
            model="test-model",
            api_base="http://localhost:1234/v1",
        )

    assert "actual_dim=1" in str(exc_info.value)

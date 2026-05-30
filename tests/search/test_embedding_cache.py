"""Tests for the embedding result cache in generate_embeddings()."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from gobby.search.embeddings import (
    _CACHE_TTL,
    EmbeddingGenerationError,
    _cache,
    _cache_key,
    clear_cache,
    generate_embedding,
    generate_embeddings,
)

pytestmark = pytest.mark.unit
LOCAL_API_BASE = "http://localhost:1234/v1"


@pytest.fixture(autouse=True)
def _clean_cache():
    """Ensure cached embeddings are cleared before and after each client test."""
    clear_cache()
    yield
    clear_cache()


def _make_mock_client(dim: int = 4) -> AsyncMock:
    """Create a mock AsyncOpenAI client that returns deterministic embeddings.

    Each embedding is [hash(text) % 100 / 100, 0, 0, ...] so different texts
    produce different vectors.
    """
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


@pytest.mark.asyncio
async def test_cache_hit_avoids_api_call() -> None:
    """Second call for same text should hit cache, not the API."""
    mock_client = _make_mock_client()
    call_count = 0
    original_create = mock_client.embeddings.create

    async def tracking_create(model: str, input: list[str]):
        nonlocal call_count
        call_count += 1
        return await original_create(model=model, input=input)

    mock_client.embeddings.create = tracking_create

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        result1 = await generate_embedding("hello", model="test-model", api_base=LOCAL_API_BASE)
        result2 = await generate_embedding("hello", model="test-model", api_base=LOCAL_API_BASE)

    assert result1 == result2
    assert call_count == 1  # second call hit cache
    key = _cache_key("hello", "test-model", LOCAL_API_BASE)
    assert key in _cache


@pytest.mark.asyncio
async def test_openai_client_closed_after_success() -> None:
    """Client cleanup should run after a successful embedding fetch."""
    mock_client = _make_mock_client()

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        await generate_embedding(
            "close-success",
            model="close-success-model",
            api_base=LOCAL_API_BASE,
        )

    mock_client.close.assert_awaited_once()
    assert mock_client.close.await_count == 1
    assert mock_client.close.await_args is not None


@pytest.mark.asyncio
async def test_openai_client_closed_after_failure() -> None:
    """Client cleanup should run when an unexpected fetch error propagates."""
    mock_client = AsyncMock()
    mock_client.embeddings.create.side_effect = ValueError("boom")

    with (
        patch("openai.AsyncOpenAI", return_value=mock_client),
        pytest.raises(ValueError, match="boom"),
    ):
        await generate_embedding(
            "close-failure",
            model="close-failure-model",
            api_base=LOCAL_API_BASE,
        )

    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_provider_response_raises_embedding_generation_error() -> None:
    """Empty provider responses should be classified as expected provider failures."""
    mock_client = AsyncMock()

    class FakeResponse:
        data: list[object] = []

    mock_client.embeddings.create.return_value = FakeResponse()

    with (
        patch("openai.AsyncOpenAI", return_value=mock_client),
        pytest.raises(EmbeddingGenerationError, match="empty result"),
    ):
        await generate_embedding("empty", model="empty-model", api_base=LOCAL_API_BASE)

    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cache_miss_on_different_text() -> None:
    """Different texts should both call the API."""
    mock_client = _make_mock_client()
    call_count = 0
    original_create = mock_client.embeddings.create

    async def tracking_create(model: str, input: list[str]):
        nonlocal call_count
        call_count += 1
        return await original_create(model=model, input=input)

    mock_client.embeddings.create = tracking_create

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        r1 = await generate_embedding("hello", model="test-model", api_base=LOCAL_API_BASE)
        r2 = await generate_embedding("world", model="test-model", api_base=LOCAL_API_BASE)

    assert r1 != r2
    assert call_count == 2


@pytest.mark.asyncio
async def test_ttl_expiry() -> None:
    """Cache entries should expire after TTL."""
    mock_client = _make_mock_client()
    call_count = 0
    original_create = mock_client.embeddings.create

    async def tracking_create(model: str, input: list[str]):
        nonlocal call_count
        call_count += 1
        return await original_create(model=model, input=input)

    mock_client.embeddings.create = tracking_create

    with (
        patch("openai.AsyncOpenAI", return_value=mock_client),
        patch("gobby.search.embeddings.time") as mock_time,
    ):
        mock_time.monotonic.return_value = 1000.0
        await generate_embedding("hello", model="test-model", api_base=LOCAL_API_BASE)
        assert call_count == 1

        # Advance past TTL
        mock_time.monotonic.return_value = 1000.0 + _CACHE_TTL + 1.0
        await generate_embedding("hello", model="test-model", api_base=LOCAL_API_BASE)
        assert call_count == 2


@pytest.mark.asyncio
async def test_batch_dedup_within_request() -> None:
    """Duplicate texts in a single batch should only be sent once to the API."""
    mock_client = _make_mock_client()
    captured_inputs: list[list[str]] = []
    original_create = mock_client.embeddings.create

    async def tracking_create(model: str, input: list[str]):
        captured_inputs.append(input)
        return await original_create(model=model, input=input)

    mock_client.embeddings.create = tracking_create

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        results = await generate_embeddings(
            ["alpha", "alpha", "beta"],
            model="test-model",
            api_base=LOCAL_API_BASE,
        )

    # API should receive only unique texts
    assert len(captured_inputs) == 1
    assert sorted(captured_inputs[0]) == ["alpha", "beta"]

    # Results should still have 3 entries with duplicates matching
    assert len(results) == 3
    assert results[0] == results[1]  # both "alpha"
    assert results[0] != results[2]  # "alpha" != "beta"


@pytest.mark.asyncio
async def test_cross_call_dedup() -> None:
    """A cached embedding from one call should be reused in a later batch."""
    mock_client = _make_mock_client()
    captured_inputs: list[list[str]] = []
    original_create = mock_client.embeddings.create

    async def tracking_create(model: str, input: list[str]):
        captured_inputs.append(input)
        return await original_create(model=model, input=input)

    mock_client.embeddings.create = tracking_create

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        # First call caches "x"
        r1 = await generate_embedding("x", model="test-model", api_base=LOCAL_API_BASE)

        # Second call should only fetch "y"
        results = await generate_embeddings(
            ["x", "y"],
            model="test-model",
            api_base=LOCAL_API_BASE,
        )

    assert len(captured_inputs) == 2
    assert captured_inputs[0] == ["x"]  # first call
    assert captured_inputs[1] == ["y"]  # second call — "x" was cached
    assert results[0] == r1  # cached value matches


@pytest.mark.asyncio
async def test_different_model_is_cache_miss() -> None:
    """Same text with different model should not hit cache."""
    mock_client = _make_mock_client()
    call_count = 0
    original_create = mock_client.embeddings.create

    async def tracking_create(model: str, input: list[str]):
        nonlocal call_count
        call_count += 1
        return await original_create(model=model, input=input)

    mock_client.embeddings.create = tracking_create

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        await generate_embedding("hello", model="model-a", api_base=LOCAL_API_BASE)
        await generate_embedding("hello", model="model-b", api_base=LOCAL_API_BASE)

    assert call_count == 2


@pytest.mark.asyncio
async def test_different_api_base_is_cache_miss() -> None:
    """Same text with different api_base should not hit cache."""
    mock_client = _make_mock_client()
    call_count = 0
    original_create = mock_client.embeddings.create

    async def tracking_create(model: str, input: list[str]):
        nonlocal call_count
        call_count += 1
        return await original_create(model=model, input=input)

    mock_client.embeddings.create = tracking_create

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        await generate_embedding("hello", model="test-model", api_base="http://localhost:1234/v1")
        await generate_embedding("hello", model="test-model", api_base="http://localhost:5678/v1")

    assert call_count == 2


@pytest.mark.asyncio
async def test_max_size_eviction() -> None:
    """Cache should evict oldest entries when exceeding max size."""
    mock_client = _make_mock_client()

    with (
        patch("openai.AsyncOpenAI", return_value=mock_client),
        patch("gobby.search.embeddings._CACHE_MAX_SIZE", 5),
    ):
        # Fill cache with 5 entries
        for i in range(5):
            await generate_embedding(f"text-{i}", model="test-model", api_base=LOCAL_API_BASE)
        assert len(_cache) == 5

        # Add one more — should evict the oldest
        await generate_embedding("text-new", model="test-model", api_base=LOCAL_API_BASE)
        assert len(_cache) == 5
        assert _cache_key("text-new", "test-model", LOCAL_API_BASE) in _cache


@pytest.mark.asyncio
async def test_empty_input() -> None:
    """Empty input should return empty list without touching cache."""
    result = await generate_embeddings([])
    assert result == []
    assert len(_cache) == 0


@pytest.mark.asyncio
async def test_clear_cache() -> None:
    """clear_cache() should empty the cache."""
    mock_client = _make_mock_client()

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        await generate_embedding("hello", model="test-model", api_base=LOCAL_API_BASE)
    assert len(_cache) > 0

    clear_cache()
    assert len(_cache) == 0

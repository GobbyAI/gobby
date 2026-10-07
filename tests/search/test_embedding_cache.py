"""Tests for the embedding result cache in generate_embeddings()."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator, Sequence
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from gobby.ai.embedding_cache import (
    _CACHE_TTL,
    _cache,
    _cache_key,
    _inflight,
    generate_cached_embeddings,
)
from gobby.ai.embeddings import (
    EmbeddingGenerationError,
    _reachability_cache,
    _reachability_cache_key,
    _ReachabilityEntry,
    clear_cache,
)
from gobby.ai.embeddings import (
    _generate_embedding as generate_embedding,
)
from gobby.ai.embeddings import (
    _generate_embeddings as generate_embeddings,
)
from tests.search.fakes import RawEmbeddingsResponse

pytestmark = pytest.mark.unit
LOCAL_API_BASE = "http://localhost:1234/v1"


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
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

    async def fake_create(model: str, input: list[str]) -> RawEmbeddingsResponse:
        class FakeItem:
            def __init__(self, embedding: list[float], index: int) -> None:
                self.embedding = embedding
                self.index = index

        class FakeResponse:
            def __init__(self, items: list[FakeItem]) -> None:
                self.data = items

        items = []
        for index, text in enumerate(input):
            vec = [0.0] * dim
            vec[0] = hash(text) % 1000 / 1000.0
            items.append(FakeItem(vec, index))
        response = FakeResponse(items)
        return RawEmbeddingsResponse(response.data)

    mock_client.embeddings.with_raw_response.create = fake_create
    return mock_client


@pytest.mark.asyncio
async def test_cache_hit_avoids_api_call() -> None:
    """Second call for same text should hit cache, not the API."""
    mock_client = _make_mock_client()
    call_count = 0
    original_create = mock_client.embeddings.with_raw_response.create

    async def tracking_create(model: str, input: list[str]) -> RawEmbeddingsResponse:
        nonlocal call_count
        call_count += 1
        return cast(RawEmbeddingsResponse, await original_create(model=model, input=input))

    mock_client.embeddings.with_raw_response.create = tracking_create

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
    mock_client.embeddings.with_raw_response.create.side_effect = ValueError("boom")

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

    mock_client.embeddings.with_raw_response.create.return_value = RawEmbeddingsResponse(
        FakeResponse().data
    )

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
    original_create = mock_client.embeddings.with_raw_response.create

    async def tracking_create(model: str, input: list[str]) -> RawEmbeddingsResponse:
        nonlocal call_count
        call_count += 1
        return cast(RawEmbeddingsResponse, await original_create(model=model, input=input))

    mock_client.embeddings.with_raw_response.create = tracking_create

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
    original_create = mock_client.embeddings.with_raw_response.create

    async def tracking_create(model: str, input: list[str]) -> RawEmbeddingsResponse:
        nonlocal call_count
        call_count += 1
        return cast(RawEmbeddingsResponse, await original_create(model=model, input=input))

    mock_client.embeddings.with_raw_response.create = tracking_create

    with (
        patch("openai.AsyncOpenAI", return_value=mock_client),
        patch("gobby.ai.embedding_cache.time") as mock_time,
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
    original_create = mock_client.embeddings.with_raw_response.create

    async def tracking_create(model: str, input: list[str]) -> RawEmbeddingsResponse:
        captured_inputs.append(input)
        return cast(RawEmbeddingsResponse, await original_create(model=model, input=input))

    mock_client.embeddings.with_raw_response.create = tracking_create

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


async def _cancel_embedding_tasks(tasks: Sequence[asyncio.Task[object]]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_waiter_timeout_does_not_cancel_shared_fetch() -> None:
    started = asyncio.Event()
    peer_lookup = asyncio.Event()
    release = asyncio.Event()
    tasks: list[asyncio.Task[list[float]]] = []
    lookup_count = 0
    fetch_count = 0
    vector = [0.25, 0.0, 0.0, 0.0]

    def tracking_key(text: str, model: str, api_base: str | None) -> str:
        nonlocal lookup_count
        lookup_count += 1
        if lookup_count == 2:
            peer_lookup.set()
        return _cache_key(text, model, api_base)

    async def fetch(texts: list[str], **_kwargs: object) -> list[list[float]]:
        nonlocal fetch_count
        fetch_count += 1
        started.set()
        await release.wait()
        return [vector for _text in texts]

    with (
        patch("gobby.ai.embeddings._fetch_embeddings", new=fetch),
        patch("gobby.ai.embedding_cache._cache_key", new=tracking_key),
    ):
        try:
            tasks.append(asyncio.create_task(generate_embedding("same", model="test-model")))
            async with asyncio.timeout(2):
                await started.wait()
            tasks.append(asyncio.create_task(generate_embedding("same", model="test-model")))
            async with asyncio.timeout(2):
                await peer_lookup.wait()
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0):
                    await generate_embedding("same", model="test-model")
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert results == [vector, vector]
            assert fetch_count == 1
        finally:
            release.set()
            await _cancel_embedding_tasks(tasks)


@pytest.mark.asyncio
async def test_cancelled_producer_releases_key_and_waiting_peer_recovers() -> None:
    started = asyncio.Event()
    peer_lookup = asyncio.Event()
    release = asyncio.Event()
    tasks: list[asyncio.Task[list[float]]] = []
    lookup_count = 0
    fetch_count = 0
    vector = [0.5, 0.0, 0.0, 0.0]
    key = _cache_key("same", "test-model", None)

    def tracking_key(text: str, model: str, api_base: str | None) -> str:
        nonlocal lookup_count
        lookup_count += 1
        if lookup_count == 2:
            peer_lookup.set()
        return _cache_key(text, model, api_base)

    async def fetch(texts: list[str], **_kwargs: object) -> list[list[float]]:
        nonlocal fetch_count
        fetch_count += 1
        if fetch_count == 1:
            started.set()
            await release.wait()
        return [vector for _text in texts]

    with (
        patch("gobby.ai.embeddings._fetch_embeddings", new=fetch),
        patch("gobby.ai.embedding_cache._cache_key", new=tracking_key),
    ):
        try:
            producer = asyncio.create_task(generate_embedding("same", model="test-model"))
            tasks.append(producer)
            async with asyncio.timeout(2):
                await started.wait()
            peer = asyncio.create_task(generate_embedding("same", model="test-model"))
            tasks.append(peer)
            async with asyncio.timeout(2):
                await peer_lookup.wait()
            producer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await producer
            assert key not in _inflight, "cancelled producer left an orphaned fill"
            async with asyncio.timeout(2):
                assert await peer == vector
                assert await generate_embedding("same", model="test-model") == vector
            assert fetch_count == 2
        finally:
            release.set()
            await _cancel_embedding_tasks(tasks)


@pytest.mark.asyncio
async def test_wrong_dim_fill_is_not_cached_and_fails_all_waiters() -> None:
    """A fill with any wrong-dimension vector caches nothing and fails every waiter."""
    peer_lookup = asyncio.Event()
    release = asyncio.Event()
    tasks: list[asyncio.Task[list[list[float]]]] = []
    lookup_count = 0

    def tracking_key(text: str, model: str, api_base: str | None) -> str:
        nonlocal lookup_count
        lookup_count += 1
        if lookup_count == 3:
            peer_lookup.set()
        return _cache_key(text, model, api_base)

    async def fetch(texts: list[str]) -> list[list[float]]:
        await release.wait()
        return [[0.5, 0.0, 0.0] for _text in texts]

    def generate(texts: list[str]) -> asyncio.Task[list[list[float]]]:
        task = asyncio.create_task(
            generate_cached_embeddings(
                texts, model="test-model", api_base=None, expected_dim=4, fetch=fetch
            )
        )
        tasks.append(task)
        return task

    with patch("gobby.ai.embedding_cache._cache_key", new=tracking_key):
        try:
            producer = generate(["a", "b"])
            peer = generate(["a"])
            async with asyncio.timeout(2):
                await peer_lookup.wait()
            release.set()
            async with asyncio.timeout(2):
                results = await asyncio.gather(producer, peer, return_exceptions=True)
        finally:
            release.set()
            await _cancel_embedding_tasks(tasks)

    assert [type(result) for result in results] == [EmbeddingGenerationError] * 2
    for text in ("a", "b"):
        key = _cache_key(text, "test-model", None)
        assert key not in _cache, f"wrong-dimension fill for {text!r} was cached"
        assert key not in _inflight


@pytest.mark.asyncio
async def test_cache_clear_keeps_new_fill_owned_until_it_completes() -> None:
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    first_finished = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    tasks: list[asyncio.Task[list[float]]] = []
    fetch_count = 0
    key = _cache_key("same", "test-model", None)
    old_vector = [0.25, 0.0, 0.0, 0.0]
    new_vector = [0.5, 0.0, 0.0, 0.0]

    async def fetch(texts: list[str], **_kwargs: object) -> list[list[float]]:
        nonlocal fetch_count
        fetch_count += 1
        if fetch_count == 1:
            first_started.set()
            await release_first.wait()
            first_finished.set()
            return [old_vector for _text in texts]
        second_started.set()
        await release_second.wait()
        return [new_vector for _text in texts]

    with patch("gobby.ai.embeddings._fetch_embeddings", new=fetch):
        try:
            tasks.append(asyncio.create_task(generate_embedding("same", model="test-model")))
            async with asyncio.timeout(2):
                await first_started.wait()
            old_fill = _inflight[key]
            clear_cache()
            tasks.append(asyncio.create_task(generate_embedding("same", model="test-model")))
            async with asyncio.timeout(2):
                await second_started.wait()
            new_fill = _inflight[key]
            assert new_fill is not old_fill
            release_first.set()
            async with asyncio.timeout(2):
                await first_finished.wait()
            assert _inflight[key] is new_fill
            assert key not in _cache, "an invalidated producer published its obsolete result"
            release_second.set()
            async with asyncio.timeout(2):
                results = await asyncio.gather(*tasks)
            assert results == [new_vector, new_vector]
            assert await generate_embedding("same", model="test-model") == new_vector
            assert fetch_count == 2
        finally:
            release_first.set()
            release_second.set()
            await _cancel_embedding_tasks(tasks)


@pytest.mark.asyncio
async def test_concurrent_misses_across_event_loops_share_fetch() -> None:
    started = threading.Event()
    peer_lookup = threading.Event()
    release = threading.Event()
    lookup_lock = threading.Lock()
    lookup_count = 0
    fetch_count = 0
    tasks: list[asyncio.Task[list[float]]] = []
    vector = [0.75, 0.0, 0.0, 0.0]

    def tracking_key(text: str, model: str, api_base: str | None) -> str:
        nonlocal lookup_count
        with lookup_lock:
            lookup_count += 1
            if lookup_count == 2:
                peer_lookup.set()
        return _cache_key(text, model, api_base)

    async def fetch(texts: list[str], **_kwargs: object) -> list[list[float]]:
        nonlocal fetch_count
        fetch_count += 1
        started.set()
        assert await asyncio.to_thread(release.wait, 2)
        return [vector for _text in texts]

    def generate_on_new_loop() -> list[float]:
        return asyncio.run(generate_embedding("same", model="test-model"))

    with (
        patch("gobby.ai.embeddings._fetch_embeddings", new=fetch),
        patch("gobby.ai.embedding_cache._cache_key", new=tracking_key),
    ):
        try:
            tasks.append(asyncio.create_task(asyncio.to_thread(generate_on_new_loop)))
            assert await asyncio.to_thread(started.wait, 2)
            tasks.append(asyncio.create_task(asyncio.to_thread(generate_on_new_loop)))
            assert await asyncio.to_thread(peer_lookup.wait, 2)
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert results == [vector, vector]
            assert fetch_count == 1
        finally:
            release.set()
            await _cancel_embedding_tasks(tasks)


@pytest.mark.asyncio
async def test_concurrent_identical_misses_share_inflight_fetch() -> None:
    """Concurrent cache misses for the same key should share one provider call."""
    mock_client = _make_mock_client()
    call_count = 0
    started = asyncio.Event()
    second_lookup = asyncio.Event()
    release = asyncio.Event()
    original_create = mock_client.embeddings.with_raw_response.create
    lookup_count = 0

    def tracking_cache_key(text: str, model: str, api_base: str | None) -> str:
        nonlocal lookup_count
        lookup_count += 1
        if lookup_count == 2:
            second_lookup.set()
        return _cache_key(text, model, api_base)

    async def tracking_create(model: str, input: list[str]) -> RawEmbeddingsResponse:
        nonlocal call_count
        call_count += 1
        started.set()
        await release.wait()
        return cast(RawEmbeddingsResponse, await original_create(model=model, input=input))

    mock_client.embeddings.with_raw_response.create = tracking_create

    with (
        patch("openai.AsyncOpenAI", return_value=mock_client),
        patch("gobby.ai.embedding_cache._cache_key", side_effect=tracking_cache_key),
    ):
        first = asyncio.create_task(
            generate_embedding("same", model="test-model", api_base=LOCAL_API_BASE)
        )
        await started.wait()
        second = asyncio.create_task(
            generate_embedding("same", model="test-model", api_base=LOCAL_API_BASE)
        )
        await second_lookup.wait()
        release.set()
        result1, result2 = await asyncio.gather(first, second)

    assert result1 == result2
    assert call_count == 1


@pytest.mark.asyncio
async def test_cross_call_dedup() -> None:
    """A cached embedding from one call should be reused in a later batch."""
    mock_client = _make_mock_client()
    captured_inputs: list[list[str]] = []
    original_create = mock_client.embeddings.with_raw_response.create

    async def tracking_create(model: str, input: list[str]) -> RawEmbeddingsResponse:
        captured_inputs.append(input)
        return cast(RawEmbeddingsResponse, await original_create(model=model, input=input))

    mock_client.embeddings.with_raw_response.create = tracking_create

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
    original_create = mock_client.embeddings.with_raw_response.create

    async def tracking_create(model: str, input: list[str]) -> RawEmbeddingsResponse:
        nonlocal call_count
        call_count += 1
        return cast(RawEmbeddingsResponse, await original_create(model=model, input=input))

    mock_client.embeddings.with_raw_response.create = tracking_create

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        await generate_embedding("hello", model="model-a", api_base=LOCAL_API_BASE)
        await generate_embedding("hello", model="model-b", api_base=LOCAL_API_BASE)

    assert call_count == 2


@pytest.mark.asyncio
async def test_different_api_base_is_cache_miss() -> None:
    """Same text with different api_base should not hit cache."""
    mock_client = _make_mock_client()
    call_count = 0
    original_create = mock_client.embeddings.with_raw_response.create

    async def tracking_create(model: str, input: list[str]) -> RawEmbeddingsResponse:
        nonlocal call_count
        call_count += 1
        return cast(RawEmbeddingsResponse, await original_create(model=model, input=input))

    mock_client.embeddings.with_raw_response.create = tracking_create

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
        patch("gobby.ai.embedding_cache._CACHE_MAX_SIZE", 5),
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


def test_clear_cache_clears_reachability_cache() -> None:
    """clear_cache() should empty reachability probes too."""
    _reachability_cache[_reachability_cache_key("http://localhost:1234/v1", None)] = (
        _ReachabilityEntry(
            reachable=True,
            checked_at=1.0,
        )
    )

    clear_cache()

    assert _reachability_cache == {}

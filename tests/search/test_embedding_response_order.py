"""Embedding API response ordering and index validation tests."""

from __future__ import annotations

import array
import base64
import threading
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gobby.ai.embeddings import (
    EmbeddingGenerationError,
    _extract_ordered_embeddings,
    _fetch_embeddings,
    _retry_embeddings_after_reload,
)
from tests.search.fakes import RawEmbeddingsResponse

LOCAL_API_BASE = "http://localhost:1234/v1"


@dataclass
class _EmbeddingItem:
    index: int
    embedding: list[float] | str


def _raw_response(*items: _EmbeddingItem) -> RawEmbeddingsResponse:
    return RawEmbeddingsResponse(items)


@pytest.mark.asyncio
async def test_fetch_embeddings_associates_out_of_order_vectors_by_index() -> None:
    client = AsyncMock()
    client.embeddings.with_raw_response.create.return_value = _raw_response(
        _EmbeddingItem(index=1, embedding=[0.0, 1.0]),
        _EmbeddingItem(index=0, embedding=[1.0, 0.0]),
    )

    with patch("openai.AsyncOpenAI", return_value=client):
        embeddings = await _fetch_embeddings(
            ["first", "second"],
            model="test-model",
            api_base=LOCAL_API_BASE,
            api_key=None,
            max_retries=0,
            base_delay=0.01,
            expected_dim=2,
        )

    assert embeddings == [[1.0, 0.0], [0.0, 1.0]]
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_embeddings_parses_batch_off_the_event_loop() -> None:
    """Response deserialization must run in a worker thread, not on the loop."""
    loop_thread = threading.get_ident()
    response = _raw_response(
        _EmbeddingItem(index=0, embedding=[1.0, 0.0]),
        _EmbeddingItem(index=1, embedding=[0.0, 1.0]),
        _EmbeddingItem(index=2, embedding=[0.5, 0.5]),
    )
    client = AsyncMock()
    client.embeddings.with_raw_response.create.return_value = response

    with patch("openai.AsyncOpenAI", return_value=client):
        embeddings = await _fetch_embeddings(
            ["first", "second", "third"],
            model="test-model",
            api_base=LOCAL_API_BASE,
            api_key=None,
            max_retries=0,
            base_delay=0.01,
            expected_dim=2,
        )

    assert embeddings == [[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]]
    assert len(response.read_threads) == 1
    assert response.read_threads[0] != loop_thread


@pytest.mark.asyncio
async def test_reload_retry_associates_out_of_order_vectors_by_index() -> None:
    client = AsyncMock()
    client.embeddings.with_raw_response.create.return_value = _raw_response(
        _EmbeddingItem(index=1, embedding=[0.0, 1.0]),
        _EmbeddingItem(index=0, embedding=[1.0, 0.0]),
    )

    embeddings = await _retry_embeddings_after_reload(
        client,
        ["first", "second"],
        "test-model",
        2,
        LOCAL_API_BASE,
    )

    assert embeddings == [[1.0, 0.0], [0.0, 1.0]]


@pytest.mark.asyncio
async def test_reload_retry_parses_off_the_event_loop() -> None:
    """The post-reload retry must also deserialize in a worker thread."""
    loop_thread = threading.get_ident()
    response = _raw_response(
        _EmbeddingItem(index=0, embedding=[1.0, 0.0]),
        _EmbeddingItem(index=1, embedding=[0.0, 1.0]),
    )
    client = AsyncMock()
    client.embeddings.with_raw_response.create.return_value = response

    embeddings = await _retry_embeddings_after_reload(
        client,
        ["first", "second"],
        "test-model",
        2,
        LOCAL_API_BASE,
    )

    assert embeddings == [[1.0, 0.0], [0.0, 1.0]]
    assert len(response.read_threads) == 1
    assert response.read_threads[0] != loop_thread


@pytest.mark.parametrize(
    "response_data,error_match",
    [
        pytest.param(
            [_EmbeddingItem(index=0, embedding=[1.0]), _EmbeddingItem(index=0, embedding=[2.0])],
            "invalid result indices",
            id="duplicate-index",
        ),
        pytest.param(
            [_EmbeddingItem(index=0, embedding=[1.0]), _EmbeddingItem(index=2, embedding=[2.0])],
            "invalid result indices",
            id="missing-index",
        ),
        pytest.param(
            [SimpleNamespace(embedding=[1.0])],
            "malformed result",
            id="malformed-index",
        ),
    ],
)
def test_extract_ordered_embeddings_rejects_invalid_indices(
    response_data: list[object],
    error_match: str,
) -> None:
    with pytest.raises(EmbeddingGenerationError, match=error_match):
        _extract_ordered_embeddings(
            response_data,
            requested_count=2,
            model="test-model",
            api_base=LOCAL_API_BASE,
        )


@pytest.mark.asyncio
async def test_fetch_embeddings_decodes_raw_float_and_base64_bodies() -> None:
    """Vectors decode from the raw body, never through SDK per-float model construction."""
    encoded = base64.b64encode(array.array("f", [0.25, -1.5]).tobytes()).decode()
    client = AsyncMock()
    client.embeddings.with_raw_response.create.return_value = _raw_response(
        _EmbeddingItem(index=1, embedding=encoded),
        _EmbeddingItem(index=0, embedding=[1.0, 0.0]),
    )

    with patch("openai.AsyncOpenAI", return_value=client):
        embeddings = await _fetch_embeddings(
            ["first", "second"],
            model="test-model",
            api_base=LOCAL_API_BASE,
            api_key=None,
            max_retries=0,
            base_delay=0.01,
            expected_dim=2,
        )

    assert embeddings == [[1.0, 0.0], [0.25, -1.5]]


@pytest.mark.asyncio
async def test_fetch_embeddings_rejects_malformed_raw_body() -> None:
    client = AsyncMock()
    client.embeddings.with_raw_response.create.return_value = _raw_response(
        _EmbeddingItem(index=0, embedding="not base64 floats!"),
    )

    with (
        patch("openai.AsyncOpenAI", return_value=client),
        pytest.raises(EmbeddingGenerationError, match="malformed result"),
    ):
        await _fetch_embeddings(
            ["first"],
            model="test-model",
            api_base=LOCAL_API_BASE,
            api_key=None,
            max_retries=0,
            base_delay=0.01,
            expected_dim=2,
        )

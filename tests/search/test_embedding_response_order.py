"""Embedding API response ordering and index validation tests."""

from __future__ import annotations

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

LOCAL_API_BASE = "http://localhost:1234/v1"


@dataclass
class _EmbeddingItem:
    index: int
    embedding: list[float]


def _response(*items: object) -> SimpleNamespace:
    return SimpleNamespace(data=list(items))


def _raw_response(*items: object) -> SimpleNamespace:
    """Wrap a response the way with_raw_response does: parse() yields the model."""
    response = _response(*items)
    return SimpleNamespace(parse=lambda: response)


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
    parse_threads: list[int] = []
    response = _response(
        _EmbeddingItem(index=0, embedding=[1.0, 0.0]),
        _EmbeddingItem(index=1, embedding=[0.0, 1.0]),
        _EmbeddingItem(index=2, embedding=[0.5, 0.5]),
    )

    def _parse() -> SimpleNamespace:
        parse_threads.append(threading.get_ident())
        return response

    client = AsyncMock()
    client.embeddings.with_raw_response.create.return_value = SimpleNamespace(parse=_parse)

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
    assert len(parse_threads) == 1
    assert parse_threads[0] != loop_thread


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
    parse_threads: list[int] = []
    response = _response(
        _EmbeddingItem(index=0, embedding=[1.0, 0.0]),
        _EmbeddingItem(index=1, embedding=[0.0, 1.0]),
    )

    def _parse() -> SimpleNamespace:
        parse_threads.append(threading.get_ident())
        return response

    client = AsyncMock()
    client.embeddings.with_raw_response.create.return_value = SimpleNamespace(parse=_parse)

    embeddings = await _retry_embeddings_after_reload(
        client,
        ["first", "second"],
        "test-model",
        2,
        LOCAL_API_BASE,
    )

    assert embeddings == [[1.0, 0.0], [0.0, 1.0]]
    assert len(parse_threads) == 1
    assert parse_threads[0] != loop_thread


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

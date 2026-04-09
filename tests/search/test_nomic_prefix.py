"""Tests for nomic task prefix application in embeddings."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from gobby.search.embeddings import (
    _apply_prefix,
    _needs_nomic_prefix,
    clear_cache,
    generate_embedding,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_cache()
    yield
    clear_cache()


# -- _needs_nomic_prefix --


def test_needs_nomic_prefix_standard() -> None:
    assert _needs_nomic_prefix("nomic-embed-text") is True


def test_needs_nomic_prefix_variant() -> None:
    assert _needs_nomic_prefix("Nomic-Embed-Text-v1.5") is True


def test_needs_nomic_prefix_with_org() -> None:
    assert _needs_nomic_prefix("openai/nomic-embed-text") is True


def test_needs_nomic_prefix_non_nomic() -> None:
    assert _needs_nomic_prefix("text-embedding-3-small") is False


# -- _apply_prefix --


def test_apply_prefix_query_nomic() -> None:
    result = _apply_prefix("hello world", is_query=True, model="nomic-embed-text")
    assert result == "search_query: hello world"


def test_apply_prefix_document_nomic() -> None:
    result = _apply_prefix("hello world", is_query=False, model="nomic-embed-text")
    assert result == "search_document: hello world"


def test_apply_prefix_non_nomic_query() -> None:
    result = _apply_prefix("hello world", is_query=True, model="text-embedding-3-small")
    assert result == "hello world"


def test_apply_prefix_non_nomic_document() -> None:
    result = _apply_prefix("hello world", is_query=False, model="text-embedding-3-small")
    assert result == "hello world"


def test_apply_prefix_nomic_variant_model() -> None:
    result = _apply_prefix("test", is_query=False, model="Nomic-Embed-Text-v1.5")
    assert result == "search_document: test"


# -- Integration: prefix reaches the API --


def _make_mock_client(dim: int = 4) -> tuple[AsyncMock, list[list[str]]]:
    """Return (mock_client, captured_inputs) where captured_inputs records each API call."""
    mock_client = AsyncMock()
    captured: list[list[str]] = []

    async def fake_create(model: str, input: list[str]):
        captured.append(input)

        class FakeItem:
            def __init__(self, embedding: list[float]):
                self.embedding = embedding

        class FakeResponse:
            def __init__(self, items: list[FakeItem]):
                self.data = items

        items = [FakeItem([0.1] * dim) for _ in input]
        return FakeResponse(items)

    mock_client.embeddings.create = fake_create
    return mock_client, captured


@pytest.mark.asyncio
async def test_generate_embedding_document_prefix_reaches_api() -> None:
    """Document embedding should send 'search_document: ...' to the API."""
    mock_client, captured = _make_mock_client()

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        await generate_embedding("cats", model="nomic-embed-text", is_query=False)

    assert len(captured) == 1
    assert captured[0] == ["search_document: cats"]


@pytest.mark.asyncio
async def test_generate_embedding_query_prefix_reaches_api() -> None:
    """Query embedding should send 'search_query: ...' to the API."""
    mock_client, captured = _make_mock_client()

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        await generate_embedding("cats", model="nomic-embed-text", is_query=True)

    assert len(captured) == 1
    assert captured[0] == ["search_query: cats"]

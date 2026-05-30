"""Tests for nomic task prefix application and model reload in embeddings."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

import gobby.search.embeddings as embeddings_mod
from gobby.search.embeddings import (
    _apply_prefix,
    _needs_nomic_prefix,
    clear_cache,
    generate_embedding,
)

pytestmark = pytest.mark.unit
LOCAL_API_BASE = "http://localhost:1234/v1"


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
        await generate_embedding(
            "cats",
            model="nomic-embed-text",
            api_base=LOCAL_API_BASE,
            is_query=False,
        )

    assert len(captured) == 1
    assert captured[0] == ["search_document: cats"]


@pytest.mark.asyncio
async def test_generate_embedding_query_prefix_reaches_api() -> None:
    """Query embedding should send 'search_query: ...' to the API."""
    mock_client, captured = _make_mock_client()

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        await generate_embedding(
            "cats",
            model="nomic-embed-text",
            api_base=LOCAL_API_BASE,
            is_query=True,
        )

    assert len(captured) == 1
    assert captured[0] == ["search_query: cats"]


# -- Model reload on eviction --


def _make_evicting_client(dim: int = 4) -> tuple[AsyncMock, list[list[str]]]:
    """Client that fails with 'no models loaded' on first call, succeeds on second."""
    from openai import BadRequestError

    mock_client = AsyncMock()
    captured: list[list[str]] = []
    call_count = 0

    async def fake_create(model: str, input: list[str]):
        nonlocal call_count
        call_count += 1
        captured.append(input)

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

        items = [FakeItem([0.1] * dim) for _ in input]
        return FakeResponse(items)

    mock_client.embeddings.create = fake_create
    return mock_client, captured


def _make_missing_model_client(dim: int = 4) -> tuple[AsyncMock, list[list[str]]]:
    """Client that fails with Ollama's missing-model 404 on first call, succeeds on second."""
    from openai import NotFoundError

    mock_client = AsyncMock()
    captured: list[list[str]] = []
    call_count = 0

    async def fake_create(model: str, input: list[str]):
        nonlocal call_count
        call_count += 1
        captured.append(input)

        if call_count == 1:
            raise NotFoundError(
                message="Error code: 404",
                response=AsyncMock(status_code=404, headers={}),
                body={
                    "message": f'model "{model}" not found, try pulling it first',
                    "type": "not_found_error",
                    "param": None,
                    "code": None,
                },
            )

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


def _make_connect_error_client(dim: int = 4) -> tuple[AsyncMock, list[list[str]]]:
    """Client that fails with a connection error on first call, then succeeds."""
    mock_client = AsyncMock()
    captured: list[list[str]] = []
    call_count = 0

    async def fake_create(model: str, input: list[str]):
        nonlocal call_count
        call_count += 1
        captured.append(input)

        if call_count == 1:
            raise httpx.ConnectError("refused")

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


@pytest.fixture(autouse=True)
def _reset_reload_cooldown():
    """Reset the reload cooldown between tests."""
    embeddings_mod._last_reload_attempt = 0.0
    embeddings_mod._last_local_lm_studio_recovery_attempt = 0.0
    yield
    embeddings_mod._last_reload_attempt = 0.0
    embeddings_mod._last_local_lm_studio_recovery_attempt = 0.0


@pytest.mark.asyncio
async def test_reload_on_eviction_lmstudio() -> None:
    """Model eviction triggers reload via try_autoload and retries."""
    mock_client, captured = _make_evicting_client()

    with (
        patch("openai.AsyncOpenAI", return_value=mock_client),
        patch("gobby.cli.services.try_autoload_embedding_model", return_value=True) as mock_reload,
    ):
        result = await generate_embedding(
            "test", model="nomic-embed-text", api_base="http://localhost:1234/v1"
        )

    assert result == [0.1] * 4
    mock_reload.assert_awaited_once_with("nomic-embed-text", "http://localhost:1234/v1")
    assert len(captured) == 2  # first call failed, retry succeeded


@pytest.mark.asyncio
async def test_reload_on_missing_model_ollama() -> None:
    """Ollama missing-model 404 triggers pull/retry via try_autoload."""
    mock_client, captured = _make_missing_model_client()

    with (
        patch("openai.AsyncOpenAI", return_value=mock_client),
        patch("gobby.cli.services.try_autoload_embedding_model", return_value=True) as mock_reload,
    ):
        result = await generate_embedding(
            "test", model="nomic-embed-text", api_base="http://localhost:11434/v1"
        )

    assert result == [0.1] * 4
    mock_reload.assert_awaited_once_with("nomic-embed-text", "http://localhost:11434/v1")
    assert len(captured) == 2  # first call failed, retry succeeded


@pytest.mark.asyncio
async def test_reload_skipped_during_cooldown() -> None:
    """Second eviction within cooldown period does not attempt reload."""
    from openai import BadRequestError

    mock_client = AsyncMock()

    async def always_fail(model: str, input: list[str]):
        raise BadRequestError(
            message="No models loaded.",
            response=AsyncMock(status_code=400, headers={}),
            body=None,
        )

    mock_client.embeddings.create = always_fail

    # Simulate a recent reload attempt
    embeddings_mod._last_reload_attempt = embeddings_mod.time.monotonic()

    with (
        patch("openai.AsyncOpenAI", return_value=mock_client),
        patch("gobby.cli.services.try_autoload_embedding_model", return_value=True) as mock_reload,
    ):
        with pytest.raises(RuntimeError, match="Embedding generation failed"):
            await generate_embedding(
                "test", model="nomic-embed-text", api_base="http://localhost:1234/v1"
            )

    mock_reload.assert_not_awaited()
    assert mock_reload.await_count == 0
    assert mock_reload.await_args is None


@pytest.mark.asyncio
async def test_reload_failure_raises() -> None:
    """If reload fails, the original error propagates."""
    from openai import BadRequestError

    mock_client = AsyncMock()

    async def always_fail(model: str, input: list[str]):
        raise BadRequestError(
            message="No models loaded.",
            response=AsyncMock(status_code=400, headers={}),
            body=None,
        )

    mock_client.embeddings.create = always_fail

    with (
        patch("openai.AsyncOpenAI", return_value=mock_client),
        patch("gobby.cli.services.try_autoload_embedding_model", return_value=False) as mock_reload,
    ):
        with pytest.raises(RuntimeError, match="Embedding generation failed"):
            await generate_embedding(
                "test", model="nomic-embed-text", api_base="http://localhost:1234/v1"
            )
    assert mock_reload.await_count == 1


@pytest.mark.asyncio
async def test_lmstudio_connection_recovery_retries_once() -> None:
    """Local LM Studio connection refusal triggers readiness helper and retry."""
    mock_client, captured = _make_connect_error_client()

    with (
        patch("openai.AsyncOpenAI", return_value=mock_client),
        patch(
            "gobby.cli.services.ensure_local_embedding_service_ready",
            new=AsyncMock(return_value=True),
        ) as mock_ready,
    ):
        result = await generate_embedding(
            "test",
            model="nomic-embed-text",
            api_base="http://localhost:1234/v1",
        )

    assert result == [0.1] * 4
    mock_ready.assert_awaited_once_with(
        model="nomic-embed-text",
        api_base="http://localhost:1234/v1",
        api_key=None,
        expected_dim=None,
    )
    assert len(captured) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_base", "error_match", "sdk_called"),
    [
        ("http://remote.example:1234/v1", "Embedding generation failed", True),
        (None, "embeddings.api_base", False),
    ],
)
async def test_connection_failures_do_not_trigger_lmstudio_recovery_for_remote_or_openai(
    api_base: str | None,
    error_match: str,
    sdk_called: bool,
) -> None:
    """Remote/OpenAI endpoints should fail fast without LM Studio recovery."""
    mock_client = AsyncMock()
    mock_client.embeddings.create = AsyncMock(side_effect=httpx.ConnectError("refused"))

    with (
        patch("openai.AsyncOpenAI", return_value=mock_client),
        patch(
            "gobby.cli.services.ensure_local_embedding_service_ready",
            new=AsyncMock(return_value=True),
        ) as mock_ready,
    ):
        with pytest.raises(RuntimeError, match=error_match):
            await generate_embedding(
                "test",
                model="nomic-embed-text",
                api_base=api_base,
            )

    mock_ready.assert_not_awaited()
    assert mock_ready.await_count == 0
    assert mock_ready.await_args is None
    if sdk_called:
        mock_client.embeddings.create.assert_awaited_once()
    else:
        mock_client.embeddings.create.assert_not_awaited()

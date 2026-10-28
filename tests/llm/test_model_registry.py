"""Tests for OpenRouter-backed model registry."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import psycopg
import pytest
from psycopg_pool import PoolTimeout

from gobby.llm.model_registry import (
    ModelInfo,
    ModelReasoningInfo,
    _parse_models_payload,
    fetch_models_async,
    lookup_context_window,
    normalize_model_id,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_trickling_model_fetch_has_a_total_deadline(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(100):
                next_chunk = asyncio.Event()
                timer = asyncio.get_running_loop().call_later(0.01, next_chunk.set)
                try:
                    await next_chunk.wait()
                finally:
                    timer.cancel()
                yield b" "

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Trickle(), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        task = asyncio.create_task(fetch_models_async(timeout=0.03, client=client))
        try:
            done, _ = await asyncio.wait({task}, timeout=0.2)
            assert task in done, "per-read timeouts let a trickling body stall the fetch"
            assert task.result() == []
            assert any(record.levelno >= logging.WARNING for record in caplog.records)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "db_error",
    [psycopg.OperationalError("database unavailable"), PoolTimeout("pool unavailable")],
)
def test_lookup_context_window_catalog_db_errors_degrade(
    db_error: Exception,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = MagicMock()
    db.fetchone.side_effect = db_error

    with caplog.at_level(logging.WARNING, logger="gobby.llm.model_registry"):
        result = lookup_context_window("openai/gpt-5.4", db=db)

    assert result is None
    assert "Catalog context-window database lookup failed" in caplog.text


@pytest.mark.parametrize(
    "db_error",
    [psycopg.OperationalError("database unavailable"), PoolTimeout("pool unavailable")],
)
def test_lookup_context_window_app_context_db_errors_degrade(
    db_error: Exception,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = MagicMock()
    db.fetchone.side_effect = db_error
    app_context = SimpleNamespace(database=db)

    with (
        patch("gobby.app_context.get_app_context", return_value=app_context),
        caplog.at_level(logging.WARNING, logger="gobby.llm.model_registry"),
    ):
        result = lookup_context_window("openai/gpt-5.4")

    assert result is None
    assert "App-context context-window database lookup failed" in caplog.text


def test_lookup_context_window_does_not_swallow_app_context_invariant_errors() -> None:
    db = MagicMock()
    db.fetchone.side_effect = AttributeError("invalid row invariant")
    app_context = SimpleNamespace(database=db)

    with (
        patch("gobby.app_context.get_app_context", return_value=app_context),
        pytest.raises(AttributeError, match="invalid row invariant"),
    ):
        lookup_context_window("openai/gpt-5.4")


# -- Fixtures ----------------------------------------------------------------

SAMPLE_OPENROUTER_RESPONSE = {
    "data": [
        {
            "id": "anthropic/claude-sonnet-4-6",
            "name": "Anthropic: Claude Sonnet 4.6",
            "context_length": 200000,
            "pricing": {
                "prompt": "0.000003",
                "completion": "0.000015",
                "input_cache_read": "0.0000003",
                "input_cache_write": "0.00000375",
            },
            "top_provider": {
                "context_length": 200000,
                "max_completion_tokens": 64000,
            },
        },
        {
            "id": "openai/gpt-4o",
            "name": "OpenAI: GPT-4o",
            "context_length": 128000,
            "pricing": {
                "prompt": "0.0000025",
                "completion": "0.00001",
            },
            "top_provider": {
                "context_length": 128000,
                "max_completion_tokens": 16384,
            },
        },
        {
            "id": "google/gemini-2.5-pro",
            "name": "Google: Gemini 2.5 Pro",
            "context_length": 1000000,
            "pricing": {
                "prompt": "0.00000125",
                "completion": "0.00001",
            },
            "top_provider": {
                "context_length": 1000000,
                "max_completion_tokens": 65536,
            },
        },
        {
            "id": "qwen/qwen3-coder",
            "name": "Qwen: Qwen3 Coder",
            "context_length": 262144,
            "top_provider": {"max_completion_tokens": 32768},
        },
        {
            "id": "z-ai/glm-5",
            "name": "Z.AI: GLM-5",
            "context_length": 128000,
            "top_provider": {"max_completion_tokens": 32768},
        },
        {
            "id": "moonshotai/kimi-k2.5",
            "name": "Moonshot AI: Kimi K2.5",
            "context_length": 256000,
            "top_provider": {"max_completion_tokens": 32768},
        },
        {
            "id": "minimax/minimax-m2.5",
            "name": "MiniMax: M2.5",
            "context_length": 200000,
            "top_provider": {"max_completion_tokens": 32768},
        },
        # Unknown vendors enter the catalog — metadata is provider-independent
        {
            "id": "mistral/mistral-large",
            "name": "Mistral: Large",
            "context_length": 128000,
            "pricing": {"prompt": "0.000002", "completion": "0.000006"},
            "top_provider": {"context_length": 128000},
        },
        # Not filtered — free models are no longer excluded (no cost tracking)
        {
            "id": "anthropic/claude-free",
            "name": "Free Claude",
            "context_length": 8000,
            "pricing": {"prompt": "0", "completion": "0"},
            "top_provider": {},
        },
    ]
}


def test_parses_distinct_reasoning_metadata_states() -> None:
    models = {
        model.id: model
        for model in _parse_models_payload(
            {
                "data": [
                    {"id": "vendor/absent", "context_length": 1},
                    {
                        "id": "vendor/null-efforts",
                        "context_length": 2,
                        "reasoning": {"supported_efforts": None},
                    },
                    {
                        "id": "vendor/empty-efforts",
                        "context_length": 3,
                        "reasoning": {"supported_efforts": []},
                    },
                    {
                        "id": "openai/gpt-5.6-luna",
                        "context_length": 4,
                        "reasoning": {
                            "supported_efforts": ["MAX", "medium", "none"],
                            "default_effort": "Medium",
                            "default_enabled": True,
                            "mandatory": False,
                        },
                    },
                ]
            }
        )
    }

    assert models["vendor/absent"].reasoning is None
    assert models["vendor/null-efforts"].reasoning == ModelReasoningInfo()
    assert models["vendor/empty-efforts"].reasoning == ModelReasoningInfo(supported_efforts=())
    assert models["openai/gpt-5.6-luna"].reasoning == ModelReasoningInfo(
        supported_efforts=("max", "medium", "none"),
        default_effort="medium",
        default_enabled=True,
        mandatory=False,
    )


# -- fetch_models_async over a mocked OpenRouter response ---------------------


def _json_handler(payload: object) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    return handler


async def _fetch(handler: Callable[[httpx.Request], httpx.Response]) -> list[ModelInfo]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await fetch_models_async(client=client)


class TestFetchModelsAsync:
    @pytest.mark.asyncio
    async def test_fetches_all_vendors(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG, logger="gobby.llm.model_registry"):
            models = await _fetch(_json_handler(SAMPLE_OPENROUTER_RESPONSE))

        # 9 valid models — no vendor filter, unknown vendors enter the catalog
        assert len(models) == 9
        ids = {m.id for m in models}
        assert "google/gemini-2.5-pro" in ids
        assert "mistral/mistral-large" in ids
        fetch_record = next(
            record
            for record in caplog.records
            if record.getMessage() == "Fetched 9 models from OpenRouter"
        )
        assert fetch_record.levelno == logging.DEBUG

    @pytest.mark.asyncio
    async def test_parses_model_fields(self) -> None:
        models = await _fetch(_json_handler(SAMPLE_OPENROUTER_RESPONSE))
        claude = next(m for m in models if m.id == "anthropic/claude-sonnet-4-6")

        assert claude.id == "anthropic/claude-sonnet-4-6"
        assert claude.name == "Anthropic: Claude Sonnet 4.6"
        assert claude.context_length == 200000
        assert claude.max_completion_tokens == 64000

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "context_length",
        [
            pytest.param(None, id="null"),
            pytest.param(0, id="zero"),
            pytest.param(-1, id="negative"),
            pytest.param(True, id="bool"),
            pytest.param("128000", id="string"),
            pytest.param(128000.0, id="float"),
        ],
    )
    async def test_skips_non_positive_integer_context_lengths(self, context_length: object) -> None:
        payload = {
            "data": [
                {
                    "id": "openai/gpt-invalid-window",
                    "name": "Invalid Window",
                    "context_length": context_length,
                }
            ]
        }

        assert await _fetch(_json_handler(payload)) == []

    @pytest.mark.asyncio
    async def test_skips_missing_context_length(self) -> None:
        payload = {"data": [{"id": "openai/gpt-missing-window", "name": "Missing Window"}]}

        assert await _fetch(_json_handler(payload)) == []

    @pytest.mark.asyncio
    async def test_network_failure_returns_empty(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        assert await _fetch(refuse) == []

    @pytest.mark.asyncio
    async def test_http_error_returns_empty(self) -> None:
        def server_error(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json=SAMPLE_OPENROUTER_RESPONSE, request=request)

        assert await _fetch(server_error) == []

    @pytest.mark.asyncio
    async def test_malformed_json_returns_empty(self) -> None:
        def not_json(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"{not json", request=request)

        assert await _fetch(not_json) == []

    @pytest.mark.asyncio
    async def test_empty_data_returns_empty(self) -> None:
        assert await _fetch(_json_handler({"data": []})) == []


@pytest.mark.asyncio
async def test_fetch_models_async_uses_shared_parser() -> None:
    response = MagicMock()
    response.json.return_value = SAMPLE_OPENROUTER_RESPONSE
    response.raise_for_status = MagicMock()
    client = MagicMock()
    client.get = AsyncMock(return_value=response)

    models = await fetch_models_async(client=client)

    assert len(models) == 9
    assert {model.id for model in models} >= {
        "anthropic/claude-sonnet-4-6",
        "google/gemini-2.5-pro",
        "mistral/mistral-large",
    }
    client.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_models_async_is_cancellable() -> None:
    client = MagicMock()
    entered = asyncio.Event()

    async def blocked_get(*_args: object, **_kwargs: object) -> object:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    client.get = AsyncMock(side_effect=blocked_get)
    task = asyncio.create_task(fetch_models_async(client=client))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# -- normalize_model_id ------------------------------------------------------


class TestNormalizeModelId:
    @pytest.mark.parametrize(
        ("model_id", "expected"),
        [
            ("anthropic/claude-opus-4-6", "claude-opus-4-6"),
            ("openai/gpt-4o", "gpt-4o"),
            ("qwen/qwen3-coder", "qwen3-coder"),
            ("z-ai/glm-5", "glm-5"),
            ("moonshotai/kimi-k2.5", "kimi-k2.5"),
            ("minimax/minimax-m2.5", "minimax-m2.5"),
            ("google/gemini-2.5-pro", "gemini-2.5-pro"),
        ],
    )
    def test_strips_known_vendor_prefix(self, model_id: str, expected: str) -> None:
        assert normalize_model_id(model_id) == expected

    def test_no_prefix(self) -> None:
        assert normalize_model_id("claude-opus-4-6") == "claude-opus-4-6"

    def test_unknown_prefix_kept(self) -> None:
        assert normalize_model_id("custom/my-model") == "custom/my-model"
        assert normalize_model_id("mistral/mistral-large") == "mistral/mistral-large"

    @pytest.mark.parametrize(
        ("model_id", "expected"),
        [
            ("endpoint:fast/anthropic/claude-opus-4-6", "claude-opus-4-6"),
            ("endpoint:fast/gpt-5.4", "gpt-5.4"),
            ("endpoint:fast/custom/my-model", "custom/my-model"),
        ],
    )
    def test_strips_endpoint_selector(self, model_id: str, expected: str) -> None:
        assert normalize_model_id(model_id) == expected


# -- duplicate normalized keys keep the larger context window ----------------


class TestDuplicateModelKeys:
    @pytest.mark.asyncio
    async def test_duplicate_normalized_keys_keep_larger_context_window(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        payload = {
            "data": [
                {
                    "id": "openai/gpt-5.4",
                    "name": "GPT-5.4 (standard tier)",
                    "context_length": 200_000,
                },
                {
                    "id": "gpt-5.4",
                    "name": "GPT-5.4 (extended tier)",
                    "context_length": 400_000,
                },
            ]
        }

        with caplog.at_level(logging.DEBUG, logger="gobby.llm.model_registry"):
            models = await _fetch(_json_handler(payload))

        assert len(models) == 1
        assert models[0].context_length == 400_000
        assert "differing context lengths" in caplog.text

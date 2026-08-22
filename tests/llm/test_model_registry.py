"""Tests for OpenRouter-backed model registry."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest
from psycopg_pool import PoolTimeout

from gobby.llm.model_registry import (
    ModelReasoningInfo,
    _parse_models_payload,
    fetch_models_async,
    fetch_models_sync,
    lookup_context_window,
    normalize_model_id,
)

pytestmark = pytest.mark.unit


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


# -- fetch_models_sync -------------------------------------------------------


class TestFetchModelsSync:
    @patch("gobby.llm.model_registry.httpx.get")
    def test_fetches_all_vendors(
        self,
        mock_get: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = SAMPLE_OPENROUTER_RESPONSE
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        with caplog.at_level(logging.DEBUG, logger="gobby.llm.model_registry"):
            models = fetch_models_sync()

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

    @patch("gobby.llm.model_registry.httpx.get")
    def test_parses_model_fields(self, mock_get: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = SAMPLE_OPENROUTER_RESPONSE
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        models = fetch_models_sync()
        claude = next(m for m in models if m.id == "anthropic/claude-sonnet-4-6")

        assert claude.id == "anthropic/claude-sonnet-4-6"
        assert claude.name == "Anthropic: Claude Sonnet 4.6"
        assert claude.context_length == 200000
        assert claude.max_completion_tokens == 64000

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
    @patch("gobby.llm.model_registry.httpx.get")
    def test_skips_non_positive_integer_context_lengths(
        self,
        mock_get: MagicMock,
        context_length: object,
    ) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": [
                {
                    "id": "openai/gpt-invalid-window",
                    "name": "Invalid Window",
                    "context_length": context_length,
                }
            ]
        }
        mock_get.return_value = mock_response

        assert fetch_models_sync() == []

    @patch("gobby.llm.model_registry.httpx.get")
    def test_skips_missing_context_length(self, mock_get: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": [{"id": "openai/gpt-missing-window", "name": "Missing Window"}]
        }
        mock_get.return_value = mock_response

        assert fetch_models_sync() == []

    @patch("gobby.llm.model_registry.httpx.get")
    def test_network_failure_returns_empty(self, mock_get: MagicMock) -> None:
        import httpx as _httpx

        mock_get.side_effect = _httpx.ConnectError("connection refused")
        models = fetch_models_sync()
        assert models == []

    @patch("gobby.llm.model_registry.httpx.get")
    def test_http_error_returns_empty(self, mock_get: MagicMock) -> None:
        import httpx as _httpx

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = _httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )
        mock_get.return_value = mock_response
        models = fetch_models_sync()
        assert models == []
        assert mock_response.json.call_count == 0

    @patch("gobby.llm.model_registry.httpx.get")
    def test_malformed_json_returns_empty(self, mock_get: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.side_effect = ValueError("bad json")
        mock_get.return_value = mock_response
        models = fetch_models_sync()
        assert models == []

    @patch("gobby.llm.model_registry.httpx.get")
    def test_empty_data_returns_empty(self, mock_get: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"data": []}
        mock_get.return_value = mock_response
        models = fetch_models_sync()
        assert models == []


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
    @patch("gobby.llm.model_registry.httpx.get")
    def test_duplicate_normalized_keys_keep_larger_context_window(
        self,
        mock_get: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {
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
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        with caplog.at_level(logging.DEBUG, logger="gobby.llm.model_registry"):
            models = fetch_models_sync()

        assert len(models) == 1
        assert models[0].context_length == 400_000
        assert "differing context lengths" in caplog.text

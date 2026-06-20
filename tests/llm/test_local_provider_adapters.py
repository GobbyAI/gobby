"""Tests for local provider-specific LLM adapters."""

from __future__ import annotations

from types import SimpleNamespace, TracebackType
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from gobby.config.ai import LocalGenerationEndpointConfig
from gobby.llm import local_provider_adapters as adapters
from gobby.llm.local_provider_adapters import (
    LMStudioLocalProviderAdapter,
    OllamaLocalProviderAdapter,
    OpenAICompatibleLocalProviderAdapter,
)

pytestmark = pytest.mark.unit


class _FakeResponse:
    def __init__(
        self,
        method: str,
        url: str,
        *,
        json_data: Any,
        status_code: int = 200,
    ) -> None:
        self.request = httpx.Request(method, url)
        self.status_code = status_code
        self._json_data = json_data
        self.text = ""

    def json(self) -> Any:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code} error",
                request=self.request,
                response=self,
            )


class _FakeAsyncClient:
    def __init__(self, responses: dict[tuple[str, str], list[_FakeResponse]]) -> None:
        self._responses = {key: list(value) for key, value in responses.items()}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def _next_response(self, method: str, url: str) -> _FakeResponse:
        responses = self._responses.get((method, url))
        if not responses:
            raise AssertionError(f"Unexpected {method} {url}")
        return responses.pop(0)

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(("POST", url, kwargs))
        return self._next_response("POST", url)


class _FakeOpenAICompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        content = '{"ok": true}' if kwargs.get("response_format") else "local reply"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        )


def test_openai_compatible_adapter_uses_openai_sdk() -> None:
    endpoint = LocalGenerationEndpointConfig(
        provider="openai-compatible",
        api_base="http://localhost:8000/v1",
        model="local-model",
        api_key="test-key",
    )

    with patch("openai.AsyncOpenAI") as mock_cls:
        adapter = OpenAICompatibleLocalProviderAdapter(endpoint)

    assert adapter.client is mock_cls.return_value
    mock_cls.assert_called_once_with(
        base_url="http://localhost:8000/v1",
        api_key="test-key",
    )


@pytest.mark.asyncio
async def test_openai_compatible_adapter_forwards_reasoning_effort() -> None:
    endpoint = LocalGenerationEndpointConfig(
        provider="openai-compatible",
        api_base="http://localhost:8000/v1",
        model="local-model",
        api_key="test-key",
    )
    completions = _FakeOpenAICompletions()
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with patch("openai.AsyncOpenAI", return_value=fake_client):
        adapter = OpenAICompatibleLocalProviderAdapter(endpoint)

    text_result = await adapter.generate_text_result(
        "hello",
        system_prompt="system",
        model="local-model",
        max_tokens=42,
        reasoning_effort="high",
    )
    json_result = await adapter.generate_json(
        "json",
        system_prompt="system",
        model="local-model",
        reasoning_effort="low",
    )

    assert text_result.text == "local reply"
    assert text_result.usage == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
    assert json_result == {"ok": True}
    assert completions.calls[0]["reasoning_effort"] == "high"
    assert completions.calls[1]["reasoning_effort"] == "low"
    assert completions.calls[1]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_openai_compatible_json_retries_without_unsupported_reasoning_effort() -> None:
    endpoint = LocalGenerationEndpointConfig(
        provider="openai-compatible",
        api_base="http://localhost:8000/v1",
        model="local-model",
        api_key="test-key",
    )

    class RejectingCompletions:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def create(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise RuntimeError("response_format not supported")
            if "reasoning_effort" in kwargs:
                raise RuntimeError("reasoning_effort unsupported")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
                usage=None,
            )

    completions = RejectingCompletions()
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with patch("openai.AsyncOpenAI", return_value=fake_client):
        adapter = OpenAICompatibleLocalProviderAdapter(endpoint)

    result = await adapter.generate_json(
        "json",
        system_prompt="custom system",
        model="local-model",
        reasoning_effort="low",
    )

    assert result == {"ok": True}
    assert len(completions.calls) == 3
    assert "response_format" not in completions.calls[1]
    assert "reasoning_effort" not in completions.calls[2]
    assert completions.calls[1]["messages"][0]["content"] == "custom system"
    assert completions.calls[2]["messages"][0]["content"] == "custom system"


@pytest.mark.asyncio
async def test_lmstudio_adapter_posts_native_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeAsyncClient(
        {
            ("POST", "http://localhost:1234/api/v1/chat"): [
                _FakeResponse(
                    "POST",
                    "http://localhost:1234/api/v1/chat",
                    json_data={
                        "output": [{"type": "message", "content": "native reply"}],
                        "stats": {"input_tokens": 5, "total_output_tokens": 7},
                    },
                )
            ]
        }
    )
    monkeypatch.setattr(adapters.httpx, "AsyncClient", lambda: fake_client)
    endpoint = LocalGenerationEndpointConfig(
        provider="lmstudio",
        api_base="http://localhost:1234/v1",
        model="google/gemma",
        api_key="token",
    )

    result = await LMStudioLocalProviderAdapter(endpoint).generate_text_result(
        "hello",
        system_prompt="system",
        model="google/gemma",
        max_tokens=42,
    )

    assert result.text == "native reply"
    assert result.usage == {
        "prompt_tokens": 5,
        "completion_tokens": 7,
        "total_tokens": 12,
    }
    assert fake_client.calls == [
        (
            "POST",
            "http://localhost:1234/api/v1/chat",
            {
                "headers": {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer token",
                },
                "json": {
                    "model": "google/gemma",
                    "input": "hello",
                    "system_prompt": "system",
                    "stream": False,
                    "store": False,
                    "max_output_tokens": 42,
                },
                "timeout": 300.0,
            },
        )
    ]


@pytest.mark.asyncio
async def test_ollama_adapter_posts_native_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeAsyncClient(
        {
            ("POST", "http://localhost:11434/api/chat"): [
                _FakeResponse(
                    "POST",
                    "http://localhost:11434/api/chat",
                    json_data={
                        "message": {"role": "assistant", "content": "ollama reply"},
                        "prompt_eval_count": 3,
                        "eval_count": 4,
                    },
                )
            ]
        }
    )
    monkeypatch.setattr(adapters.httpx, "AsyncClient", lambda: fake_client)
    endpoint = LocalGenerationEndpointConfig(
        provider="ollama",
        api_base="http://localhost:11434/v1",
        model="qwen3",
    )

    result = await OllamaLocalProviderAdapter(endpoint).generate_text_result(
        "hello",
        system_prompt="system",
        model="qwen3",
        max_tokens=99,
    )

    assert result.text == "ollama reply"
    assert result.usage == {
        "prompt_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 7,
    }
    assert fake_client.calls == [
        (
            "POST",
            "http://localhost:11434/api/chat",
            {
                "json": {
                    "model": "qwen3",
                    "messages": [
                        {"role": "system", "content": "system"},
                        {"role": "user", "content": "hello"},
                    ],
                    "stream": False,
                    "keep_alive": -1,
                    "options": {"num_predict": 99},
                },
                "timeout": 300.0,
            },
        )
    ]


@pytest.mark.asyncio
async def test_ollama_adapter_requests_json_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeAsyncClient(
        {
            ("POST", "http://localhost:11434/api/chat"): [
                _FakeResponse(
                    "POST",
                    "http://localhost:11434/api/chat",
                    json_data={"message": {"content": '{"ok": true}'}},
                )
            ]
        }
    )
    monkeypatch.setattr(adapters.httpx, "AsyncClient", lambda: fake_client)
    endpoint = LocalGenerationEndpointConfig(
        provider="ollama",
        api_base="http://localhost:11434",
        model="qwen3",
    )

    result = await OllamaLocalProviderAdapter(endpoint).generate_json(
        "json",
        system_prompt=None,
        model="qwen3",
    )

    assert result == {"ok": True}
    assert fake_client.calls[0][2]["json"]["format"] == "json"

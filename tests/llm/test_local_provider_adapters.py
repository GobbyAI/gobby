"""Tests for local provider-specific LLM adapters."""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any, get_args
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from openai import APIConnectionError, AuthenticationError, BadRequestError
from pydantic import ValidationError

from gobby.config.ai import (
    GenerationConfig,
    GenerationEndpointConfig,
    GenerationEndpointProtocol,
)
from gobby.llm import local_provider_adapters as adapters
from gobby.llm.base import (
    LLMProviderError,
    VisionInputError,
)
from gobby.llm.local_provider_adapters import (
    LMStudioLocalProviderAdapter,
    OllamaLocalProviderAdapter,
    OpenAICompatibleLocalProviderAdapter,
    create_local_provider_adapter,
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
    endpoint = GenerationEndpointConfig(
        protocol="openai-compatible",
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
        timeout=adapters._LOCAL_OPENAI_TIMEOUT,
        max_retries=0,
    )
    timeout = mock_cls.call_args.kwargs["timeout"]
    assert timeout.connect == 5.0
    assert timeout.read == 120.0
    assert timeout.write == 30.0
    assert timeout.pool == 5.0
    assert adapters._LOCAL_OPENAI_OVERALL_TIMEOUT_SECONDS == 120.0


def test_create_adapter_vllm() -> None:
    assert "vllm" in get_args(GenerationEndpointProtocol)
    config = GenerationConfig(
        endpoints={
            "local-vllm": GenerationEndpointConfig(
                protocol="vllm",
                api_base="http://127.0.0.1:8000/v1",
                model="qwen2.5-vl",
                api_key="test-key",
            )
        }
    )
    endpoint = config.endpoints["local-vllm"]
    assert endpoint.protocol == "vllm"

    with patch("openai.AsyncOpenAI") as mock_cls:
        adapter = create_local_provider_adapter(endpoint)

    assert isinstance(adapter, OpenAICompatibleLocalProviderAdapter)
    assert adapter.client is mock_cls.return_value
    assert adapter.client is not None
    vllm_adapter_classes = [
        name
        for name, value in vars(adapters).items()
        if isinstance(value, type) and "vllm" in name.lower()
    ]
    assert vllm_adapter_classes == []
    mock_cls.assert_called_once_with(
        base_url="http://127.0.0.1:8000/v1",
        api_key="test-key",
        timeout=adapters._LOCAL_OPENAI_TIMEOUT,
        max_retries=0,
    )


def test_vllm_rejects_responses_wire() -> None:
    with pytest.raises(
        ValidationError,
        match="wire_api='responses' requires protocol='openai-compatible'",
    ):
        GenerationEndpointConfig(
            protocol="vllm",
            wire_api="responses",
            api_base="http://127.0.0.1:8000/v1",
            model="qwen2.5-vl",
        )


@pytest.mark.asyncio
async def test_openai_compatible_adapter_forwards_reasoning_effort() -> None:
    endpoint = GenerationEndpointConfig(
        protocol="openai-compatible",
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


def _gif_data_url() -> str:
    encoded = base64.standard_b64encode(b"GIF89a").decode("utf-8")
    return f"data:image/gif;base64,{encoded}"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai-compatible", "vllm"])
async def test_generate_with_images(protocol: str) -> None:
    endpoint = GenerationEndpointConfig(
        protocol=protocol,
        api_base="http://localhost:8000/v1",
        model="local-vlm",
        api_key="test-key",
    )
    completions = _FakeOpenAICompletions()
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with patch("openai.AsyncOpenAI", return_value=fake_client):
        adapter = OpenAICompatibleLocalProviderAdapter(endpoint)

    text_only = await adapter.generate_text_result(
        "caption this",
        system_prompt="system",
        model="local-vlm",
        max_tokens=64,
    )
    image_result = await adapter.generate_text_result(
        "caption this",
        system_prompt="system",
        model="local-vlm",
        max_tokens=64,
        images=[_gif_data_url()],
    )

    assert text_only.text == "local reply"
    assert image_result.text == "local reply"
    text_messages = completions.calls[0]["messages"]
    image_messages = completions.calls[1]["messages"]
    assert text_messages[1] == {"role": "user", "content": "caption this"}
    content = image_messages[1]["content"]
    assert isinstance(content, list)
    image_block = next(part for part in content if part["type"] == "image_url")
    text_block = next(part for part in content if part["type"] == "text")
    assert image_block["image_url"]["url"].startswith("data:image/gif;base64,")
    assert text_block["text"] == "caption this"
    assert completions.calls[1]["model"] == "local-vlm"
    assert "auto" not in completions.calls[1]["model"]


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, "", " \t\n"])
async def test_openai_compatible_adapter_rejects_blank_text_content(
    content: str | None,
) -> None:
    endpoint = GenerationEndpointConfig(
        protocol="openai-compatible",
        api_base="http://localhost:8000/v1",
        model="local-model",
        api_key="test-key",
    )
    completions = AsyncMock()
    completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=None,
    )
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with patch("openai.AsyncOpenAI", return_value=fake_client):
        adapter = OpenAICompatibleLocalProviderAdapter(endpoint)

    with pytest.raises(
        LLMProviderError,
        match=r"OpenAI-compatible provider .*local-model.* returned blank content",
    ):
        await adapter.generate_text_result(
            "hello",
            system_prompt=None,
            model="local-model",
            max_tokens=None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "json_mode_error",
    [
        pytest.param(
            BadRequestError(
                message="response_format json_object not supported",
                response=httpx.Response(
                    400,
                    request=httpx.Request("POST", "http://test"),
                ),
                body=None,
            ),
            id="message",
        ),
        pytest.param(
            BadRequestError(
                message="Invalid request parameter",
                response=httpx.Response(
                    400,
                    request=httpx.Request("POST", "http://test"),
                ),
                body={"code": "unsupported_value", "param": "response_format"},
            ),
            id="code-and-param",
        ),
        pytest.param(
            BadRequestError(
                message="Invalid request parameter",
                response=httpx.Response(
                    400,
                    request=httpx.Request("POST", "http://test"),
                ),
                body={"code": "unsupported_response_format"},
            ),
            id="specific-code",
        ),
    ],
)
async def test_openai_compatible_json_retries_once_without_json_mode(
    json_mode_error: BadRequestError,
) -> None:
    endpoint = GenerationEndpointConfig(
        protocol="openai-compatible",
        api_base="http://localhost:8000/v1",
        model="local-model",
        api_key="test-key",
    )

    class RejectingCompletions:
        def __init__(self, error: BadRequestError) -> None:
            self.calls: list[dict[str, Any]] = []
            self.error = error

        async def create(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise self.error
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
                usage=None,
            )

    completions = RejectingCompletions(json_mode_error)
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with patch("openai.AsyncOpenAI", return_value=fake_client):
        adapter = OpenAICompatibleLocalProviderAdapter(endpoint)

    result = await adapter.generate_json(
        "json",
        system_prompt="custom system",
        model="local-model",
        max_tokens=321,
        reasoning_effort="low",
    )

    assert result == {"ok": True}
    assert len(completions.calls) == 2
    assert [call["max_tokens"] for call in completions.calls] == [321, 321]
    assert "response_format" not in completions.calls[1]
    assert completions.calls[1]["reasoning_effort"] == "low"
    assert completions.calls[1]["messages"][0]["content"] == "custom system"


@pytest.mark.asyncio
async def test_openai_compatible_json_defaults_to_8000_tokens() -> None:
    endpoint = GenerationEndpointConfig(
        protocol="openai-compatible",
        api_base="http://localhost:8000/v1",
        model="local-model",
    )
    completions = _FakeOpenAICompletions()
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with patch("openai.AsyncOpenAI", return_value=fake_client):
        adapter = OpenAICompatibleLocalProviderAdapter(endpoint)

    assert await adapter.generate_json(
        "json",
        system_prompt=None,
        model="local-model",
    ) == {"ok": True}
    assert completions.calls[0]["max_tokens"] == 8000


async def test_openai_compatible_json_does_not_retry_failed_fallback() -> None:
    endpoint = GenerationEndpointConfig(
        protocol="openai-compatible",
        api_base="http://localhost:8000/v1",
        model="local-model",
        api_key="test-key",
    )
    error = BadRequestError(
        message="response_format json_object not supported",
        response=httpx.Response(400, request=httpx.Request("POST", "http://test")),
        body=None,
    )
    completions = AsyncMock()
    completions.create.side_effect = [error, error]
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with patch("openai.AsyncOpenAI", return_value=fake_client):
        adapter = OpenAICompatibleLocalProviderAdapter(endpoint)

    with pytest.raises(BadRequestError):
        await adapter.generate_json(
            "json",
            system_prompt=None,
            model="local-model",
        )

    assert completions.create.await_count == 2


@pytest.mark.parametrize(
    "request_error",
    [
        pytest.param(
            BadRequestError(
                message="model does not exist",
                response=httpx.Response(
                    400,
                    request=httpx.Request("POST", "http://test"),
                ),
                body={"code": "model_not_found", "param": "model"},
            ),
            id="unrelated-bad-request",
        ),
        pytest.param(
            BadRequestError(
                message="response_format json_object not supported",
                response=httpx.Response(
                    422,
                    request=httpx.Request("POST", "http://test"),
                ),
                body=None,
            ),
            id="wrong-response-status",
        ),
        pytest.param(
            APIConnectionError(request=httpx.Request("POST", "http://test")),
            id="transport",
        ),
        pytest.param(
            AuthenticationError(
                message="invalid token",
                response=httpx.Response(
                    401,
                    request=httpx.Request("POST", "http://test"),
                ),
                body={"code": "invalid_api_key"},
            ),
            id="auth",
        ),
    ],
)
async def test_openai_compatible_json_unrelated_errors_do_not_retry(
    request_error: Exception,
) -> None:
    endpoint = GenerationEndpointConfig(
        protocol="openai-compatible",
        api_base="http://localhost:8000/v1",
        model="local-model",
        api_key="test-key",
    )
    completions = AsyncMock()
    completions.create.side_effect = request_error
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with patch("openai.AsyncOpenAI", return_value=fake_client):
        adapter = OpenAICompatibleLocalProviderAdapter(endpoint)

    with pytest.raises(type(request_error)):
        await adapter.generate_json(
            "json",
            system_prompt=None,
            model="local-model",
        )

    assert completions.create.await_count == 1


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
    endpoint = GenerationEndpointConfig(
        protocol="lmstudio",
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
    endpoint = GenerationEndpointConfig(
        protocol="ollama",
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
    endpoint = GenerationEndpointConfig(
        protocol="ollama",
        api_base="http://localhost:11434",
        model="qwen3",
    )

    result = await OllamaLocalProviderAdapter(endpoint).generate_json(
        "json",
        system_prompt=None,
        model="qwen3",
        max_tokens=42,
    )

    assert result == {"ok": True}
    assert fake_client.calls[0][2]["json"]["format"] == "json"
    assert fake_client.calls[0][2]["json"]["options"] == {"num_predict": 42}


@pytest.mark.asyncio
async def test_lmstudio_json_forwards_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _FakeAsyncClient(
        {
            ("POST", "http://localhost:1234/api/v1/chat"): [
                _FakeResponse(
                    "POST",
                    "http://localhost:1234/api/v1/chat",
                    json_data={"output": [{"type": "message", "content": '{"ok": true}'}]},
                )
            ]
        }
    )
    monkeypatch.setattr(adapters.httpx, "AsyncClient", lambda: fake_client)
    endpoint = GenerationEndpointConfig(
        protocol="lmstudio",
        api_base="http://localhost:1234/v1",
        model="google/gemma",
    )

    result = await LMStudioLocalProviderAdapter(endpoint).generate_json(
        "json",
        system_prompt=None,
        model="google/gemma",
        max_tokens=73,
    )

    assert result == {"ok": True}
    assert fake_client.calls[0][2]["json"]["max_output_tokens"] == 73


@pytest.mark.asyncio
async def test_native_image_serialization_ported(monkeypatch: pytest.MonkeyPatch) -> None:
    data_url = _gif_data_url()
    encoded = base64.standard_b64encode(b"GIF89a").decode("utf-8")

    lm_client = _FakeAsyncClient(
        {
            ("POST", "http://localhost:1234/api/v1/chat"): [
                _FakeResponse(
                    "POST",
                    "http://localhost:1234/api/v1/chat",
                    json_data={"output": [{"type": "message", "content": "lm vision"}]},
                )
            ]
        }
    )
    monkeypatch.setattr("gobby.llm.local_provider_adapters.httpx.AsyncClient", lambda: lm_client)
    lm_result = await LMStudioLocalProviderAdapter(
        GenerationEndpointConfig(
            protocol="lmstudio",
            api_base="http://localhost:1234/v1",
            model="google/gemma",
            api_key="token",
        )
    ).generate_text_result(
        "caption this",
        system_prompt="system",
        model="google/gemma",
        max_tokens=1024,
        images=[data_url],
    )
    lm_payload = lm_client.calls[0][2]["json"]
    assert lm_result.text == "lm vision"
    assert lm_payload["input"] == [
        {"type": "image", "data_url": data_url},
        {"type": "message", "content": "caption this"},
    ]
    assert lm_payload["system_prompt"] == "system"
    assert lm_payload["max_output_tokens"] == 1024

    ollama_client = _FakeAsyncClient(
        {
            ("POST", "http://localhost:11434/api/chat"): [
                _FakeResponse(
                    "POST",
                    "http://localhost:11434/api/chat",
                    json_data={"message": {"role": "assistant", "content": "ollama vision"}},
                )
            ]
        }
    )
    monkeypatch.setattr(
        "gobby.llm.local_provider_adapters.httpx.AsyncClient", lambda: ollama_client
    )
    ollama_result = await OllamaLocalProviderAdapter(
        GenerationEndpointConfig(
            protocol="ollama",
            api_base="http://localhost:11434/v1",
            model="llava",
        )
    ).generate_text_result(
        "caption this",
        system_prompt="system",
        model="llava",
        max_tokens=1024,
        images=[data_url],
    )
    ollama_payload = ollama_client.calls[0][2]["json"]
    user_message = ollama_payload["messages"][-1]
    assert ollama_result.text == "ollama vision"
    assert user_message["role"] == "user"
    assert user_message["content"] == "caption this"
    assert user_message["images"] == [encoded]


def _vision_adapter(provider: str) -> Any:
    endpoint = GenerationEndpointConfig(
        protocol=provider,
        api_base={
            "openai-compatible": "http://localhost:8000/v1",
            "lmstudio": "http://localhost:1234/v1",
            "ollama": "http://localhost:11434/v1",
        }[provider],
        model="vision-model",
        api_key="test-key",
    )
    if provider == "openai-compatible":
        adapter = OpenAICompatibleLocalProviderAdapter(endpoint)
        adapter._client = SimpleNamespace(
            chat=SimpleNamespace(completions=_FakeOpenAICompletions())
        )
        return adapter
    if provider == "lmstudio":
        return LMStudioLocalProviderAdapter(endpoint)
    return OllamaLocalProviderAdapter(endpoint)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai-compatible", "lmstudio", "ollama"])
async def test_local_vision_missing_file_raises_input_error(provider: str) -> None:
    adapter = _vision_adapter(provider)

    with pytest.raises(VisionInputError, match="Image not found"):
        await adapter.generate_text_result(
            "caption this",
            system_prompt=None,
            model="vision-model",
            max_tokens=1024,
            images=["/missing/image.png"],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai-compatible", "lmstudio", "ollama"])
async def test_local_vision_unreadable_file_raises_input_error(
    provider: str,
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"image")
    adapter = _vision_adapter(provider)

    with patch.object(Path, "open", side_effect=PermissionError("denied")):
        with pytest.raises(VisionInputError, match="Failed to read") as exc_info:
            await adapter.generate_text_result(
                "caption this",
                system_prompt=None,
                model="vision-model",
                max_tokens=1024,
                images=[str(image_path)],
            )

    assert isinstance(exc_info.value.__cause__, PermissionError)


@pytest.mark.asyncio
async def test_local_vision_uninitialised_client_raises_provider_error(tmp_path: Path) -> None:
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"image")
    adapter = _vision_adapter("openai-compatible")
    adapter._client = None

    with pytest.raises(RuntimeError, match="not initialised"):
        await adapter.generate_text_result(
            "caption this",
            system_prompt=None,
            model="vision-model",
            max_tokens=1024,
            images=[str(image_path)],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai-compatible", "lmstudio", "ollama"])
async def test_local_vision_provider_failure_raises_structured_error(
    provider: str,
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"image")
    adapter = _vision_adapter(provider)
    if provider == "openai-compatible":
        completions = AsyncMock()
        completions.create.side_effect = RuntimeError("provider failed")
        adapter._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    else:
        adapter._post_chat = AsyncMock(side_effect=RuntimeError("provider failed"))

    with pytest.raises(RuntimeError, match="provider failed"):
        await adapter.generate_text_result(
            "caption this",
            system_prompt=None,
            model="vision-model",
            max_tokens=1024,
            images=[str(image_path)],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai-compatible", "lmstudio", "ollama"])
async def test_local_vision_preserves_successful_output(provider: str, tmp_path: Path) -> None:
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"image")
    adapter = _vision_adapter(provider)
    expected = "local reply"
    if provider == "lmstudio":
        adapter._post_chat = AsyncMock(
            return_value={"output": [{"type": "message", "content": expected}]}
        )
    elif provider == "ollama":
        adapter._post_chat = AsyncMock(
            return_value={"message": {"role": "assistant", "content": expected}}
        )

    result = await adapter.generate_text_result(
        "caption this",
        system_prompt=None,
        model="vision-model",
        max_tokens=1024,
        images=[str(image_path)],
    )

    assert result.text == expected


@pytest.mark.parametrize(
    "api_base",
    [
        pytest.param("http://127.0.0.1:8000", id="bare-origin"),
        pytest.param("http://127.0.0.1:8000/", id="trailing-slash"),
        pytest.param("http://127.0.0.1:8000/v1/", id="v1-trailing-slash"),
    ],
)
def test_vllm_adapter_normalizes_client_base_url(api_base: str) -> None:
    """The generation client uses the same {origin}/v1 base the resolver discovers on."""
    endpoint = GenerationEndpointConfig(protocol="vllm", api_base=api_base, model="auto")

    with patch("openai.AsyncOpenAI") as mock_cls:
        create_local_provider_adapter(endpoint)

    assert mock_cls.call_args.kwargs["base_url"] == "http://127.0.0.1:8000/v1"


@pytest.mark.parametrize("protocol", ["openai-compatible", "vllm"])
def test_keyless_local_endpoint_sends_no_authorization_header(protocol: str) -> None:
    keyless = create_local_provider_adapter(
        GenerationEndpointConfig(
            protocol=protocol,
            api_base="http://localhost:8000/v1",
            model="local-model",
        )
    )
    keyed = create_local_provider_adapter(
        GenerationEndpointConfig(
            protocol=protocol,
            api_base="http://localhost:8000/v1",
            model="local-model",
            api_key="local-secret",
        )
    )

    assert keyless.client is not None
    assert keyless.client.api_key == ""
    assert keyless.client.auth_headers == {}
    assert keyed.client is not None
    assert keyed.client.auth_headers == {"Authorization": "Bearer local-secret"}

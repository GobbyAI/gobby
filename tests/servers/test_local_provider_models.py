from __future__ import annotations

from typing import Any

import httpx
import pytest

from gobby.config.ai import LocalGenerationEndpointConfig
from gobby.servers.local_provider_models import discover_local_endpoint_model_group

pytestmark = pytest.mark.unit


class _FakeResponse:
    def __init__(self, url: str, payload: dict[str, Any], status_code: int = 200) -> None:
        self.request = httpx.Request("GET", url)
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code} error",
                request=self.request,
                response=self,
            )


class _FakeAsyncClient:
    def __init__(self, responses: dict[str, _FakeResponse]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def get(self, url: str, **_kwargs: Any) -> _FakeResponse:
        self.urls.append(url)
        response = self.responses.get(url)
        if response is None:
            raise httpx.ConnectError("missing fixture", request=httpx.Request("GET", url))
        return response


@pytest.mark.asyncio
async def test_discovers_lmstudio_llm_models(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = LocalGenerationEndpointConfig(
        provider="lmstudio",
        api_base="http://localhost:1234/v1",
        model="auto",
    )
    fake_client = _FakeAsyncClient(
        {
            "http://localhost:1234/api/v1/models": _FakeResponse(
                "http://localhost:1234/api/v1/models",
                {
                    "data": [
                        {
                            "id": "google/gemma-4-26b-a4b-qat",
                            "display_name": "Gemma 4",
                            "type": "llm",
                            "max_context_length": 131072,
                        },
                        {"id": "embed-small", "display_name": "Embed", "type": "embedding"},
                    ]
                },
            )
        }
    )
    monkeypatch.setattr(
        "gobby.servers.local_provider_models.httpx.AsyncClient",
        lambda: fake_client,
    )

    group = await discover_local_endpoint_model_group("lm-studio", endpoint)

    assert group.source == "live"
    assert group.display_name == "Local: LM Studio"
    assert group.models == [
        {
            "value": "local:lm-studio",
            "label": "Default (auto)",
            "canonical_id": "auto",
            "is_default": True,
        },
        {
            "value": "local:lm-studio/google/gemma-4-26b-a4b-qat",
            "label": "Gemma 4",
            "canonical_id": "google/gemma-4-26b-a4b-qat",
            "context_length": 131072,
            "context_length_source": "provider_reported",
        },
    ]


@pytest.mark.asyncio
async def test_discovers_ollama_native_models(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = LocalGenerationEndpointConfig(
        provider="ollama",
        api_base="http://localhost:11434",
        model="llama3.2:latest",
    )
    fake_client = _FakeAsyncClient(
        {
            "http://localhost:11434/api/tags": _FakeResponse(
                "http://localhost:11434/api/tags",
                {
                    "models": [
                        {
                            "name": "qwen3-coder:latest",
                            "model": "qwen3-coder:latest",
                            "details": {
                                "family": "qwen3",
                                "parameter_size": "30B",
                                "quantization_level": "Q4_K_M",
                            },
                        }
                    ]
                },
            ),
            "http://localhost:11434/api/ps": _FakeResponse(
                "http://localhost:11434/api/ps",
                {"models": [{"name": "qwen3-coder:latest"}]},
            ),
        }
    )
    monkeypatch.setattr(
        "gobby.servers.local_provider_models.httpx.AsyncClient",
        lambda: fake_client,
    )

    group = await discover_local_endpoint_model_group("ollama", endpoint)

    assert group.source == "live"
    assert group.models[1]["value"] == "local:ollama/qwen3-coder:latest"
    assert group.models[1]["capabilities"] == {
        "family": "qwen3",
        "parameter_size": "30B",
        "quantization_level": "Q4_K_M",
    }


@pytest.mark.asyncio
async def test_ollama_falls_back_to_openai_compatible_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = LocalGenerationEndpointConfig(
        provider="ollama",
        api_base="http://localhost:11434",
        model="llama3.2:latest",
    )
    fake_client = _FakeAsyncClient(
        {
            "http://localhost:11434/v1/models": _FakeResponse(
                "http://localhost:11434/v1/models",
                {"data": [{"id": "ollama-cloud/qwen3-coder"}]},
            )
        }
    )
    monkeypatch.setattr(
        "gobby.servers.local_provider_models.httpx.AsyncClient",
        lambda: fake_client,
    )

    group = await discover_local_endpoint_model_group("ollama", endpoint)

    assert group.source == "live"
    assert group.models[1]["value"] == "local:ollama/ollama-cloud/qwen3-coder"


@pytest.mark.asyncio
async def test_discovers_openai_compatible_models(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = LocalGenerationEndpointConfig(
        provider="openai-compatible",
        api_base="http://localhost:8000/v1",
        model="configured-model",
        api_key="token",
    )
    fake_client = _FakeAsyncClient(
        {
            "http://localhost:8000/v1/models": _FakeResponse(
                "http://localhost:8000/v1/models",
                {
                    "data": [
                        {
                            "id": "qwen/qwen3-coder",
                            "name": "Qwen Coder",
                            "context_length": "32768",
                            "capabilities": ["chat"],
                        }
                    ]
                },
            )
        }
    )
    monkeypatch.setattr(
        "gobby.servers.local_provider_models.httpx.AsyncClient",
        lambda: fake_client,
    )

    group = await discover_local_endpoint_model_group("local-openai", endpoint)

    assert fake_client.urls == ["http://localhost:8000/v1/models"]
    assert group.source == "live"
    assert group.models == [
        {
            "value": "local:local-openai",
            "label": "Default (configured-model)",
            "canonical_id": "configured-model",
            "is_default": True,
        },
        {
            "value": "local:local-openai/qwen/qwen3-coder",
            "label": "Qwen Coder",
            "canonical_id": "qwen/qwen3-coder",
            "context_length": 32768,
            "context_length_source": "provider_reported",
            "capabilities": ["chat"],
        },
    ]


@pytest.mark.asyncio
async def test_openai_compatible_empty_discovery_uses_config_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = LocalGenerationEndpointConfig(
        provider="openai-compatible",
        api_base="http://localhost:8000/v1",
        model="configured-model",
    )
    fake_client = _FakeAsyncClient(
        {
            "http://localhost:8000/v1/models": _FakeResponse(
                "http://localhost:8000/v1/models",
                {"data": []},
            )
        }
    )
    monkeypatch.setattr(
        "gobby.servers.local_provider_models.httpx.AsyncClient",
        lambda: fake_client,
    )

    group = await discover_local_endpoint_model_group("local-openai", endpoint)

    assert group.source == "config"
    assert group.error is None
    assert group.models == [
        {
            "value": "local:local-openai",
            "label": "Default (configured-model)",
            "canonical_id": "configured-model",
            "is_default": True,
        }
    ]


@pytest.mark.asyncio
async def test_discovery_failure_falls_back_to_configured_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = LocalGenerationEndpointConfig(
        provider="lmstudio",
        api_base="http://localhost:1234/v1",
        model="gemma-local",
    )
    fake_client = _FakeAsyncClient({})
    monkeypatch.setattr(
        "gobby.servers.local_provider_models.httpx.AsyncClient",
        lambda: fake_client,
    )

    group = await discover_local_endpoint_model_group("lm-studio", endpoint)

    assert group.source == "config"
    assert group.error
    assert group.models == [
        {
            "value": "local:lm-studio",
            "label": "Default (gemma-local)",
            "canonical_id": "gemma-local",
            "is_default": True,
        }
    ]

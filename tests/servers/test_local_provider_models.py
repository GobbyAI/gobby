from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from gobby.config.ai import GenerationEndpointConfig
from gobby.servers.local_provider_models import discover_local_endpoint_model_group

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_ollama_detail_fanout_is_bounded_to_four(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.servers import local_provider_models

    active = 0
    max_active = 0
    first_wave_started = asyncio.Event()
    release_first_wave = asyncio.Event()

    async def fake_details(
        _client: Any,
        _endpoint: GenerationEndpointConfig,
        model_id: str,
    ) -> tuple[bool, dict[str, Any]]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if active == 4:
            first_wave_started.set()
        await release_first_wave.wait()
        active -= 1
        return True, {"model": model_id}

    monkeypatch.setattr(local_provider_models, "_ollama_model_details", fake_details)
    endpoint = GenerationEndpointConfig(
        protocol="ollama",
        api_base="http://localhost:11434",
        model="model-0",
    )

    batch_task = asyncio.create_task(
        local_provider_models._ollama_model_details_batch(
            MagicMock(), endpoint, [f"model-{index}" for index in range(9)]
        )
    )
    await asyncio.wait_for(first_wave_started.wait(), timeout=1)
    assert max_active == 4
    release_first_wave.set()
    results = await batch_task

    assert [details["model"] for _, details in results] == [f"model-{index}" for index in range(9)]


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
    def __init__(
        self,
        responses: dict[str, _FakeResponse],
        *,
        show_responses: dict[str, _FakeResponse] | None = None,
    ) -> None:
        self.responses = responses
        self.show_responses = show_responses or {}
        self.urls: list[str] = []
        self.shown_models: list[str] = []

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

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        payload = kwargs.get("json")
        model = payload.get("model") if isinstance(payload, dict) else None
        if not isinstance(model, str):
            raise AssertionError("Ollama /api/show request must include a model")
        self.urls.append(url)
        self.shown_models.append(model)
        response = self.show_responses.get(model)
        if response is None:
            raise httpx.ConnectError("missing fixture", request=httpx.Request("POST", url))
        return response


@pytest.mark.asyncio
async def test_discovers_lmstudio_llm_models(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = GenerationEndpointConfig(
        protocol="lmstudio",
        api_base="http://localhost:1234/v1",
        model="google/gemma-4-26b-a4b-qat",
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
                        {"id": "unclassified", "display_name": "Unknown"},
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
            "value": "endpoint:lm-studio",
            "label": "Default (google/gemma-4-26b-a4b-qat)",
            "canonical_id": "google/gemma-4-26b-a4b-qat",
            "is_default": True,
        },
        {
            "value": "endpoint:lm-studio/google/gemma-4-26b-a4b-qat",
            "label": "Gemma 4",
            "canonical_id": "google/gemma-4-26b-a4b-qat",
            "context_length": 131072,
            "context_length_source": "provider_reported",
        },
    ]


@pytest.mark.asyncio
async def test_lmstudio_rejects_an_ineligible_configured_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = GenerationEndpointConfig(
        protocol="lmstudio",
        api_base="http://localhost:1234/v1",
        model="embed-small",
    )
    fake_client = _FakeAsyncClient(
        {
            "http://localhost:1234/api/v1/models": _FakeResponse(
                "http://localhost:1234/api/v1/models",
                {"data": [{"id": "embed-small", "type": "embedding"}]},
            )
        }
    )
    monkeypatch.setattr(
        "gobby.servers.local_provider_models.httpx.AsyncClient",
        lambda: fake_client,
    )

    group = await discover_local_endpoint_model_group("lm-studio", endpoint)

    assert group.source == "live"
    assert group.models == []
    assert group.error == "No completion-capable models discovered"


@pytest.mark.asyncio
async def test_discovers_ollama_native_models(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = GenerationEndpointConfig(
        protocol="ollama",
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
                        },
                        {"name": "nomic-embed-text:latest"},
                    ]
                },
            ),
            "http://localhost:11434/api/ps": _FakeResponse(
                "http://localhost:11434/api/ps",
                {"models": [{"name": "qwen3-coder:latest"}]},
            ),
        },
        show_responses={
            "llama3.2:latest": _FakeResponse(
                "http://localhost:11434/api/show",
                {"capabilities": ["completion"]},
            ),
            "qwen3-coder:latest": _FakeResponse(
                "http://localhost:11434/api/show",
                {
                    "capabilities": ["completion", "tools"],
                    "details": {
                        "family": "qwen3",
                        "parameter_size": "30B",
                        "quantization_level": "Q4_K_M",
                    },
                },
            ),
            "nomic-embed-text:latest": _FakeResponse(
                "http://localhost:11434/api/show",
                {"capabilities": ["embedding"]},
            ),
        },
    )
    monkeypatch.setattr(
        "gobby.servers.local_provider_models.httpx.AsyncClient",
        lambda: fake_client,
    )

    group = await discover_local_endpoint_model_group("ollama", endpoint)

    assert group.source == "live"
    assert fake_client.shown_models == [
        "qwen3-coder:latest",
        "nomic-embed-text:latest",
        "llama3.2:latest",
    ]
    assert group.models[1]["value"] == "endpoint:ollama/qwen3-coder:latest"
    assert group.models[1]["capabilities"] == {
        "family": "qwen3",
        "parameter_size": "30B",
        "quantization_level": "Q4_K_M",
    }
    assert all("nomic-embed" not in model["value"] for model in group.models)


@pytest.mark.asyncio
async def test_ollama_rejects_a_default_without_completion_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = GenerationEndpointConfig(
        protocol="ollama",
        api_base="http://localhost:11434",
        model="nomic-embed-text:latest",
    )
    fake_client = _FakeAsyncClient(
        {
            "http://localhost:11434/api/tags": _FakeResponse(
                "http://localhost:11434/api/tags",
                {"models": [{"name": "nomic-embed-text:latest"}]},
            ),
            "http://localhost:11434/api/ps": _FakeResponse(
                "http://localhost:11434/api/ps",
                {"models": []},
            ),
        },
        show_responses={
            "nomic-embed-text:latest": _FakeResponse(
                "http://localhost:11434/api/show",
                {"capabilities": ["embedding"]},
            )
        },
    )
    monkeypatch.setattr(
        "gobby.servers.local_provider_models.httpx.AsyncClient",
        lambda: fake_client,
    )

    group = await discover_local_endpoint_model_group("ollama", endpoint)

    assert group.source == "live"
    assert group.models == []
    assert group.error == "No completion-capable models discovered"


@pytest.mark.asyncio
async def test_ollama_falls_back_to_openai_compatible_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = GenerationEndpointConfig(
        protocol="ollama",
        api_base="http://localhost:11434",
        model="llama3.2:latest",
    )
    fake_client = _FakeAsyncClient(
        {
            "http://localhost:11434/v1/models": _FakeResponse(
                "http://localhost:11434/v1/models",
                {"data": [{"id": "ollama-cloud/qwen3-coder"}]},
            )
        },
        show_responses={
            "ollama-cloud/qwen3-coder": _FakeResponse(
                "http://localhost:11434/api/show",
                {"capabilities": ["completion"]},
            )
        },
    )
    monkeypatch.setattr(
        "gobby.servers.local_provider_models.httpx.AsyncClient",
        lambda: fake_client,
    )

    group = await discover_local_endpoint_model_group("ollama", endpoint)

    assert group.source == "live"
    assert group.models == [
        {
            "value": "endpoint:ollama/ollama-cloud/qwen3-coder",
            "label": "ollama-cloud/qwen3-coder",
            "canonical_id": "ollama-cloud/qwen3-coder",
        }
    ]


@pytest.mark.asyncio
async def test_discovers_openai_compatible_models(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = GenerationEndpointConfig(
        protocol="openai-compatible",
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
            "value": "endpoint:local-openai",
            "label": "Default (configured-model)",
            "canonical_id": "configured-model",
            "is_default": True,
        },
        {
            "value": "endpoint:local-openai/qwen/qwen3-coder",
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
    endpoint = GenerationEndpointConfig(
        protocol="openai-compatible",
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
            "value": "endpoint:local-openai",
            "label": "Default (configured-model)",
            "canonical_id": "configured-model",
            "is_default": True,
        }
    ]


@pytest.mark.asyncio
async def test_discovery_failure_falls_back_to_configured_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = GenerationEndpointConfig(
        protocol="lmstudio",
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
    assert group.models == []

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from gobby.ai._text_generation_service import image_candidate_eligible
from gobby.ai.registry import AIAdapterStyle, AICapability, CapabilityBinding
from gobby.config.ai import GenerationEndpointConfig
from gobby.servers.local_provider_models import (
    LocalEndpointModelGroup,
    discover_local_endpoint_model_group,
)
from gobby.servers.routes.providers import _local_generation_provider_entries

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
            "input_modalities": ["text"],
        },
        {
            "value": "endpoint:lm-studio/google/gemma-4-26b-a4b-qat",
            "label": "Gemma 4",
            "canonical_id": "google/gemma-4-26b-a4b-qat",
            "context_length": 131072,
            "context_length_source": "provider_reported",
            "input_modalities": ["text"],
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
            "input_modalities": ["text"],
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


_VLLM_HEALTH_URL = "http://localhost:8000/health"
_VLLM_MODELS_URL = "http://localhost:8000/v1/models"
_VLLM_PROBED_ID = "Qwen/Qwen2.5-VL-7B-Instruct"
_VLLM_OTHER_ID = "meta-llama/Llama-3.1-8B-Instruct"


def _vllm_client(models_payload: dict[str, Any]) -> _FakeAsyncClient:
    return _FakeAsyncClient(
        {
            _VLLM_HEALTH_URL: _FakeResponse(_VLLM_HEALTH_URL, {}),
            _VLLM_MODELS_URL: _FakeResponse(_VLLM_MODELS_URL, models_payload),
        }
    )


def _patch_discovery_client(monkeypatch: pytest.MonkeyPatch, fake_client: _FakeAsyncClient) -> None:
    monkeypatch.setattr(
        "gobby.servers.local_provider_models.httpx.AsyncClient",
        lambda: fake_client,
    )


def _entry_by_canonical(models: list[dict[str, Any]], canonical_id: str) -> dict[str, Any]:
    matches = [
        entry
        for entry in models
        if entry.get("canonical_id") == canonical_id and entry.get("is_default") is not True
    ]
    assert len(matches) == 1, f"expected one served entry for {canonical_id!r}, got {matches!r}"
    return matches[0]


def _default_entry(models: list[dict[str, Any]]) -> dict[str, Any] | None:
    defaults = [entry for entry in models if entry.get("is_default") is True]
    assert len(defaults) <= 1
    return defaults[0] if defaults else None


def _two_vllm_models_payload() -> dict[str, Any]:
    return {
        "data": [
            {"id": _VLLM_PROBED_ID, "max_model_len": 32768},
            {"id": _VLLM_OTHER_ID, "max_model_len": 8192},
        ]
    }


@pytest.mark.asyncio
async def test_vllm_discovery_error_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = GenerationEndpointConfig(
        protocol="vllm",
        api_base="http://localhost:8000/v1",
        model=_VLLM_PROBED_ID,
    )
    fake_client = _FakeAsyncClient({})
    _patch_discovery_client(monkeypatch, fake_client)

    group = await discover_local_endpoint_model_group("vllm-local", endpoint)

    assert fake_client.urls == [_VLLM_HEALTH_URL]
    assert group.source == "config"
    assert group.error
    assert group.display_name == "Local: vLLM"
    assert group.models == []


@pytest.mark.asyncio
async def test_vllm_modalities_probed_model_only(monkeypatch: pytest.MonkeyPatch) -> None:
    vision_modalities = ["text", "image"]
    two_model_payload = _two_vllm_models_payload()

    probed_default_client = _vllm_client(two_model_payload)
    _patch_discovery_client(monkeypatch, probed_default_client)
    probed_default = await discover_local_endpoint_model_group(
        "vllm-local",
        GenerationEndpointConfig(
            protocol="vllm",
            api_base="http://localhost:8000/v1",
            model=_VLLM_PROBED_ID,
            probed_model=_VLLM_PROBED_ID,
            input_modalities=vision_modalities,
        ),
    )

    assert probed_default_client.urls == [_VLLM_HEALTH_URL, _VLLM_MODELS_URL]
    assert probed_default.source == "live"
    assert probed_default.display_name == "Local: vLLM"
    probed_entry = _entry_by_canonical(probed_default.models, _VLLM_PROBED_ID)
    other_entry = _entry_by_canonical(probed_default.models, _VLLM_OTHER_ID)
    default_entry = _default_entry(probed_default.models)
    assert default_entry is not None
    assert default_entry["value"] == "endpoint:vllm-local"
    assert probed_entry["context_length"] == 32768
    assert probed_entry["context_length_source"] == "provider_reported"
    assert probed_entry["input_modalities"] == vision_modalities
    assert other_entry["context_length"] == 8192
    assert other_entry.get("input_modalities") is None
    assert default_entry["input_modalities"] == vision_modalities

    unprobed_default_client = _vllm_client(two_model_payload)
    _patch_discovery_client(monkeypatch, unprobed_default_client)
    unprobed_default = await discover_local_endpoint_model_group(
        "vllm-local",
        GenerationEndpointConfig(
            protocol="vllm",
            api_base="http://localhost:8000/v1",
            model=_VLLM_OTHER_ID,
            probed_model=_VLLM_PROBED_ID,
            input_modalities=vision_modalities,
        ),
    )
    unprobed_alias = _default_entry(unprobed_default.models)
    assert unprobed_alias is not None
    assert unprobed_alias["canonical_id"] == _VLLM_OTHER_ID
    assert unprobed_alias.get("input_modalities") is None
    assert _entry_by_canonical(unprobed_default.models, _VLLM_PROBED_ID)["input_modalities"] == (
        vision_modalities
    )
    assert _entry_by_canonical(unprobed_default.models, _VLLM_OTHER_ID).get("input_modalities") is (
        None
    )

    auto_single_client = _vllm_client({"data": [{"id": _VLLM_PROBED_ID, "max_model_len": 32768}]})
    _patch_discovery_client(monkeypatch, auto_single_client)
    auto_single = await discover_local_endpoint_model_group(
        "vllm-local",
        GenerationEndpointConfig(
            protocol="vllm",
            api_base="http://localhost:8000/v1",
            model="auto",
            probed_model=_VLLM_PROBED_ID,
            input_modalities=vision_modalities,
        ),
    )
    auto_default = _default_entry(auto_single.models)
    assert auto_default is not None
    assert auto_default["value"] == "endpoint:vllm-local"
    assert auto_default["input_modalities"] == vision_modalities
    assert _entry_by_canonical(auto_single.models, _VLLM_PROBED_ID)["input_modalities"] == (
        vision_modalities
    )

    auto_multi_client = _vllm_client(two_model_payload)
    _patch_discovery_client(monkeypatch, auto_multi_client)
    auto_multi = await discover_local_endpoint_model_group(
        "vllm-local",
        GenerationEndpointConfig(
            protocol="vllm",
            api_base="http://localhost:8000/v1",
            model="auto",
            probed_model=_VLLM_PROBED_ID,
            input_modalities=vision_modalities,
        ),
    )
    assert _default_entry(auto_multi.models) is None
    assert _entry_by_canonical(auto_multi.models, _VLLM_PROBED_ID)["input_modalities"] == (
        vision_modalities
    )
    assert _entry_by_canonical(auto_multi.models, _VLLM_OTHER_ID).get("input_modalities") is None

    stale_probe_client = _vllm_client(two_model_payload)
    _patch_discovery_client(monkeypatch, stale_probe_client)
    stale_probe = await discover_local_endpoint_model_group(
        "vllm-local",
        GenerationEndpointConfig(
            protocol="vllm",
            api_base="http://localhost:8000/v1",
            model=_VLLM_OTHER_ID,
            probed_model="missing-served-id",
            input_modalities=vision_modalities,
        ),
    )
    assert _entry_by_canonical(stale_probe.models, _VLLM_PROBED_ID).get("input_modalities") is None
    assert _entry_by_canonical(stale_probe.models, _VLLM_OTHER_ID).get("input_modalities") is None
    stale_default = _default_entry(stale_probe.models)
    assert stale_default is not None
    assert stale_default.get("input_modalities") is None


_LMSTUDIO_MODELS_URL = "http://localhost:1234/api/v1/models"
_LMSTUDIO_VLM_ID = "qwen2.5-vl-7b"
_LMSTUDIO_LLM_ID = "qwen2.5-7b"
_LMSTUDIO_MLX_ID = "mlx-format"
_OLLAMA_VISION_ID = "llava:latest"
_OLLAMA_TEXT_ID = "llama3.2:latest"


def _lmstudio_catalog_client() -> _FakeAsyncClient:
    return _FakeAsyncClient(
        {
            _LMSTUDIO_MODELS_URL: _FakeResponse(
                _LMSTUDIO_MODELS_URL,
                {
                    "data": [
                        {
                            "id": _LMSTUDIO_LLM_ID,
                            "display_name": "Qwen text",
                            "type": "llm",
                        },
                        {
                            "id": _LMSTUDIO_VLM_ID,
                            "display_name": "Qwen VL",
                            "type": "vlm",
                        },
                        {
                            "id": "nomic-embed",
                            "display_name": "Embed",
                            "type": "embedding",
                        },
                        {"id": "unclassified", "display_name": "Unknown"},
                        {
                            "id": _LMSTUDIO_MLX_ID,
                            "display_name": "MLX weights",
                            "type": "mlx",
                        },
                    ]
                },
            )
        }
    )


def _ollama_catalog_client() -> _FakeAsyncClient:
    return _FakeAsyncClient(
        {
            "http://localhost:11434/api/tags": _FakeResponse(
                "http://localhost:11434/api/tags",
                {
                    "models": [
                        {"name": _OLLAMA_TEXT_ID, "model": _OLLAMA_TEXT_ID},
                        {"name": _OLLAMA_VISION_ID, "model": _OLLAMA_VISION_ID},
                        {"name": "nomic-embed-text:latest"},
                    ]
                },
            ),
            "http://localhost:11434/api/ps": _FakeResponse(
                "http://localhost:11434/api/ps",
                {"models": []},
            ),
        },
        show_responses={
            _OLLAMA_TEXT_ID: _FakeResponse(
                "http://localhost:11434/api/show",
                {"capabilities": ["completion"]},
            ),
            _OLLAMA_VISION_ID: _FakeResponse(
                "http://localhost:11434/api/show",
                {"capabilities": ["completion", "vision"]},
            ),
            "nomic-embed-text:latest": _FakeResponse(
                "http://localhost:11434/api/show",
                {"capabilities": ["embedding"]},
            ),
        },
    )


def _catalog_payload_models(group: LocalEndpointModelGroup) -> list[dict[str, Any]]:
    entries = _local_generation_provider_entries(
        [group],
        codex_installed=True,
        codex_available=True,
        codex_unavailable_reason=None,
    )
    assert len(entries) == 1
    models = entries[0]["models"]
    assert isinstance(models, list)
    return models


def _routing_binding(
    protocol: str,
    endpoint_name: str,
    entry: dict[str, Any],
) -> CapabilityBinding:
    return CapabilityBinding(
        capability=AICapability.TEXT_GENERATE,
        provider=f"endpoint:{endpoint_name}",
        adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
        available=True,
        models=(str(entry["canonical_id"]),),
        metadata={
            "endpoint": endpoint_name,
            "protocol": protocol,
            "wire_api": "chat-completions",
            "input_modalities": entry.get("input_modalities"),
        },
    )


def _assert_modalities_match_predicate(
    protocol: str,
    endpoint_name: str,
    models: list[dict[str, Any]],
) -> None:
    for entry in models:
        assert "input_modalities" in entry
        modalities = entry.get("input_modalities")
        expect_image = isinstance(modalities, list) and "image" in modalities
        binding = _routing_binding(protocol, endpoint_name, entry)
        model_id = str(entry["canonical_id"])
        assert image_candidate_eligible(binding, model=model_id) is expect_image
        if entry.get("is_default") is True:
            assert image_candidate_eligible(binding) is expect_image


@pytest.mark.asyncio
async def test_lmstudio_vlm_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    vision_modalities = ["text", "image"]
    text_modalities = ["text"]

    advertised_client = _lmstudio_catalog_client()
    _patch_discovery_client(monkeypatch, advertised_client)
    advertised = await discover_local_endpoint_model_group(
        "studio",
        GenerationEndpointConfig(
            protocol="lmstudio",
            api_base="http://localhost:1234/v1",
            model=_LMSTUDIO_VLM_ID,
        ),
    )

    vlm_entry = _entry_by_canonical(advertised.models, _LMSTUDIO_VLM_ID)
    llm_entry = _entry_by_canonical(advertised.models, _LMSTUDIO_LLM_ID)
    mlx_entry = _entry_by_canonical(advertised.models, _LMSTUDIO_MLX_ID)
    default_entry = _default_entry(advertised.models)
    assert vlm_entry["input_modalities"] == vision_modalities
    assert llm_entry["input_modalities"] == text_modalities
    assert mlx_entry.get("input_modalities") is None
    assert all("nomic-embed" not in str(model["value"]) for model in advertised.models)
    assert all("unclassified" not in str(model["value"]) for model in advertised.models)
    assert default_entry is not None
    assert default_entry["value"] == "endpoint:studio"
    assert default_entry["input_modalities"] == vision_modalities

    degrade_client = _lmstudio_catalog_client()
    _patch_discovery_client(monkeypatch, degrade_client)
    degrade = await discover_local_endpoint_model_group(
        "studio",
        GenerationEndpointConfig(
            protocol="lmstudio",
            api_base="http://localhost:1234/v1",
            model=_LMSTUDIO_VLM_ID,
            probed_model=_LMSTUDIO_VLM_ID,
            input_modalities=text_modalities,
        ),
    )
    degrade_default = _default_entry(degrade.models)
    assert _entry_by_canonical(degrade.models, _LMSTUDIO_VLM_ID)["input_modalities"] == (
        text_modalities
    )
    assert _entry_by_canonical(degrade.models, _LMSTUDIO_LLM_ID)["input_modalities"] == (
        text_modalities
    )
    assert degrade_default is not None
    assert degrade_default["input_modalities"] == text_modalities


@pytest.mark.asyncio
async def test_modalities_match_routing_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    vision_modalities = ["text", "image"]

    _patch_discovery_client(monkeypatch, _lmstudio_catalog_client())
    lmstudio = await discover_local_endpoint_model_group(
        "studio",
        GenerationEndpointConfig(
            protocol="lmstudio",
            api_base="http://localhost:1234/v1",
            model=_LMSTUDIO_VLM_ID,
        ),
    )
    lmstudio_models = _catalog_payload_models(lmstudio)
    lmstudio_default = _default_entry(lmstudio_models)
    assert lmstudio_default is not None
    assert lmstudio_default["input_modalities"] == vision_modalities
    assert _entry_by_canonical(lmstudio_models, _LMSTUDIO_VLM_ID)["input_modalities"] == (
        vision_modalities
    )
    _assert_modalities_match_predicate("lmstudio", "studio", lmstudio_models)

    _patch_discovery_client(monkeypatch, _ollama_catalog_client())
    ollama = await discover_local_endpoint_model_group(
        "ollama-local",
        GenerationEndpointConfig(
            protocol="ollama",
            api_base="http://localhost:11434",
            model=_OLLAMA_VISION_ID,
        ),
    )
    ollama_models = _catalog_payload_models(ollama)
    ollama_default = _default_entry(ollama_models)
    assert ollama_default is not None
    assert ollama_default["input_modalities"] == vision_modalities
    assert _entry_by_canonical(ollama_models, _OLLAMA_VISION_ID)["input_modalities"] == (
        vision_modalities
    )
    assert _entry_by_canonical(ollama_models, _OLLAMA_TEXT_ID)["input_modalities"] == ["text"]
    _assert_modalities_match_predicate("ollama", "ollama-local", ollama_models)

    _patch_discovery_client(monkeypatch, _vllm_client(_two_vllm_models_payload()))
    vllm = await discover_local_endpoint_model_group(
        "vllm-local",
        GenerationEndpointConfig(
            protocol="vllm",
            api_base="http://localhost:8000/v1",
            model=_VLLM_PROBED_ID,
            probed_model=_VLLM_PROBED_ID,
            input_modalities=vision_modalities,
        ),
    )
    vllm_models = _catalog_payload_models(vllm)
    vllm_default = _default_entry(vllm_models)
    assert vllm_default is not None
    assert vllm_default["input_modalities"] == vision_modalities
    assert _entry_by_canonical(vllm_models, _VLLM_PROBED_ID)["input_modalities"] == (
        vision_modalities
    )
    assert _entry_by_canonical(vllm_models, _VLLM_OTHER_ID).get("input_modalities") is None
    _assert_modalities_match_predicate("vllm", "vllm-local", vllm_models)

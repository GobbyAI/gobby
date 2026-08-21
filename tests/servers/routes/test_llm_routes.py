from __future__ import annotations

import base64
import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import gobby.servers.routes.llm as llm_module
from gobby.ai import (
    AIAdapterStyle,
    AICapability,
    AICapabilityRegistry,
    CapabilityBinding,
    CapabilityUnavailableError,
    TextGenerationRequest,
    TextGenerationService,
    ToolChatRequest,
    ToolChatResult,
    ToolLoopLimits,
    VisionExtractRequest,
    VisionExtractResult,
    VisionExtractService,
)
from gobby.config.ai import AIConfig, GenerationConfig
from gobby.config.app import DaemonConfig
from gobby.config.feature_base import FeatureCandidateConfig
from gobby.llm.base import LLMTextResult, VisionInputError, VisionProviderError
from gobby.llm.image_payloads import MAX_IMAGE_BYTES
from gobby.runner_init.services import AIServiceBundle
from gobby.servers.http import HTTPServer
from gobby.servers.routes.llm import _server_operation, create_llm_router

pytestmark = pytest.mark.unit


class _FakeVisionService:
    def __init__(
        self,
        *,
        text: str = "Screen text",
        ocr_text: str | None = "Button label",
    ) -> None:
        self.request: VisionExtractRequest | None = None
        self.text = text
        self.ocr_text = ocr_text

    async def extract(self, request: VisionExtractRequest) -> VisionExtractResult:
        assert Path(request.image_path).exists()
        self.request = request
        return VisionExtractResult(
            text=self.text,
            capability=AICapability.VISION_EXTRACT,
            provider=request.provider or "endpoint:lm-studio",
            model=request.model or "llava",
            ocr_text=self.ocr_text,
        )


class _FailingVisionService:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def extract(self, request: VisionExtractRequest) -> VisionExtractResult:
        raise self.error


class _FakeTextAdapter:
    def __init__(self, text: str = "Generated text", usage: dict[str, int] | None = None) -> None:
        self.requests: list[TextGenerationRequest] = []
        self.text = text
        self.usage = usage

    async def generate(self, request: TextGenerationRequest) -> str | LLMTextResult:
        self.requests.append(request)
        if self.usage is not None:
            return LLMTextResult(text=self.text, usage=self.usage)
        return self.text


@pytest.fixture
def server_with_llm() -> MagicMock:
    server = MagicMock()
    server.config = DaemonConfig(
        ai=AIConfig(
            generation=GenerationConfig(
                endpoints={
                    "lm-studio": {
                        "api_base": "http://localhost:1234/v1",
                        "model": "llava",
                        "probed_model": "llava",
                        "input_modalities": ["text", "image"],
                    }
                }
            )
        )
    )
    server.services = SimpleNamespace(text_generation_service=None)
    server.auth_service.verified_agent_claims.return_value = None
    return server


@pytest.fixture
def client(server_with_llm: MagicMock) -> TestClient:
    app = FastAPI()
    app.include_router(create_llm_router(server_with_llm))
    return TestClient(
        app,
        headers={"X-Gobby-Session-Id": "019fc08a-1d63-4b23-bbc8-659d56bc4168"},
    )


def test_server_operation_captures_one_runtime_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    server = HTTPServer.__new__(HTTPServer)
    server.startup_config = DaemonConfig()
    server.services = MagicMock()
    active_config = DaemonConfig()
    text_service = MagicMock()
    bundle = MagicMock()
    bundle.snapshot.active = active_config
    bundle.services = {
        "ai_services": AIServiceBundle(
            text_generation_service=text_service,
            llm_service=MagicMock(),
            tool_chat_service=MagicMock(),
        )
    }
    capture = MagicMock(return_value=bundle)
    monkeypatch.setattr(server, "capture_runtime_bundle", capture)

    config, service = _server_operation(server, "text_generation_service")

    assert config is active_config
    assert service is text_service
    capture.assert_called_once_with()


def test_finish_reason_returns_unmapped_stop_reason() -> None:
    assert llm_module._finish_reason_from_stop_reason("content_filter") == "content_filter"


def test_llm_status_returns_registry_snapshot(client: TestClient) -> None:
    response = client.get("/api/llm/status")

    assert response.status_code == 200
    data = response.json()
    assert "capabilities" in data
    assert "text_generate" in data["capabilities"]
    assert "vision_extract" in data["capabilities"]
    assert data["capabilities"]["vision_extract"]["capability"] == "vision_extract"
    for capability in ("text_generate", "vision_extract"):
        providers = {
            binding["provider"] for binding in data["capabilities"][capability]["bindings"]
        }
        assert "endpoint:lm-studio" in providers


def test_create_llm_router_does_not_run_vision_temp_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    server_with_llm: MagicMock,
) -> None:
    cleanup = MagicMock()
    monkeypatch.setattr(llm_module, "_run_vision_temp_cleanup_once", cleanup)

    router = create_llm_router(server_with_llm)

    assert router.prefix == "/api/llm"
    assert any(getattr(route, "path", "").endswith("/status") for route in router.routes)
    cleanup.assert_not_called()


def test_generate_selects_explicit_provider_model(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    adapter = _FakeTextAdapter()
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="qwen",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("qwen3-coder",),
            )
        ]
    )
    service = TextGenerationService(registry, {"qwen": adapter})
    server_with_llm.services.text_generation_service = service

    response = client.post(
        "/api/llm/generate",
        json={
            "prompt": "Summarize this",
            "provider": "qwen",
            "model": "qwen3-coder",
            "system_prompt": "Be concise",
            "max_tokens": 128,
            "cwd": "/tmp/project",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "text": "Generated text",
        "capability": "text_generate",
        "provider": "qwen",
        "model": "qwen3-coder",
    }
    assert adapter.requests == [
        TextGenerationRequest(
            prompt="Summarize this",
            provider="qwen",
            model="qwen3-coder",
            system_prompt="Be concise",
            max_tokens=128,
            caller="llm-generate-route",
            cwd="/tmp/project",
        )
    ]


def _gif_data_url() -> str:
    encoded = base64.standard_b64encode(b"GIF89a").decode("utf-8")
    return f"data:image/gif;base64,{encoded}"


def test_generate_route_forwards_images(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    adapter = _FakeTextAdapter()
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="endpoint:local-vllm",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("qwen-vl",),
                metadata={
                    "endpoint": "local-vllm",
                    "protocol": "vllm",
                    "wire_api": "chat-completions",
                    "input_modalities": ["text", "image"],
                },
            )
        ]
    )
    server_with_llm.services.text_generation_service = TextGenerationService(
        registry,
        {"endpoint:local-vllm": adapter},
    )
    images = [_gif_data_url()]

    response = client.post(
        "/api/llm/generate",
        json={
            "prompt": "caption this",
            "provider": "endpoint:local-vllm",
            "model": "qwen-vl",
            "images": images,
        },
    )

    assert response.status_code == 200
    assert response.json()["provider"] == "endpoint:local-vllm"
    assert response.json()["model"] == "qwen-vl"
    assert adapter.requests[0].images == images
    assert adapter.requests[0].prompt == "caption this"


def test_generate_route_image_rejections(
    client: TestClient,
    server_with_llm: MagicMock,
    tmp_path: Path,
) -> None:
    adapter = _FakeTextAdapter()
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="endpoint:local-vllm",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("qwen-vl",),
                metadata={
                    "endpoint": "local-vllm",
                    "protocol": "vllm",
                    "wire_api": "chat-completions",
                    "input_modalities": ["text", "image"],
                },
            )
        ]
    )
    server_with_llm.services.text_generation_service = TextGenerationService(
        registry,
        {"endpoint:local-vllm": adapter},
    )
    oversized = []
    for index in range(5):
        path = tmp_path / f"huge-{index}.png"
        path.write_bytes(b"x" * MAX_IMAGE_BYTES)
        oversized.append(str(path))
    cases = [
        (["data:image/png"], "Malformed data URL"),
        (["data:image/bmp;base64,Qk0="], "Disallowed image MIME type"),
        (["data:image/png;base64,!!!!"], "Invalid image base64"),
        (["relative.png"], "Image path must be absolute: relative.png"),
        (["/missing/does-not-exist.png"], "Image not found"),
        ([_gif_data_url()] * 9, "Too many images (max 8)"),
        (oversized, "aggregate limit"),
    ]
    for images, match in cases:
        response = client.post(
            "/api/llm/generate",
            json={
                "prompt": "caption this",
                "provider": "endpoint:local-vllm",
                "model": "qwen-vl",
                "images": images,
            },
        )
        assert response.status_code == 400, (images[0][:40], response.status_code, response.text)
        assert match in response.json()["detail"]
        assert adapter.requests == []


def test_generate_caps_and_propagates_timeout_overrides(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    adapter = _FakeTextAdapter()
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="qwen",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("qwen3-coder",),
            )
        ]
    )
    server_with_llm.services.text_generation_service = TextGenerationService(
        registry,
        {"qwen": adapter},
    )

    # Callers may raise per-candidate budgets above the tight configured
    # defaults for long-running generations (#18288); the total attempt budget
    # is the only cap. total itself still clamps to the configured total.
    response = client.post(
        "/api/llm/generate",
        json={
            "prompt": "Summarize this",
            "provider": "qwen",
            "model": "qwen3-coder",
            "candidate_timeout_seconds": 90,
            "cli_candidate_timeout_seconds": 300,
            "total_timeout_seconds": 2000,
        },
    )

    assert response.status_code == 200
    assert len(adapter.requests) == 1
    request = adapter.requests[0]
    assert request.candidate_timeout_seconds == 90
    assert request.cli_candidate_timeout_seconds == 300
    assert request.total_timeout_seconds == 1200

    # Per-candidate values never exceed the total budget; with no total in the
    # payload the configured total (1200) is the cap.
    response = client.post(
        "/api/llm/generate",
        json={
            "prompt": "Summarize this",
            "provider": "qwen",
            "model": "qwen3-coder",
            "candidate_timeout_seconds": 5000,
            "cli_candidate_timeout_seconds": 5000,
        },
    )

    assert response.status_code == 200
    request = adapter.requests[1]
    assert request.candidate_timeout_seconds == 1200
    assert request.cli_candidate_timeout_seconds == 1200
    assert request.total_timeout_seconds is None

    # Omitted per-candidate fields keep the tight configured defaults (#17710),
    # clamped to a payload-supplied total.
    response = client.post(
        "/api/llm/generate",
        json={
            "prompt": "Summarize this",
            "provider": "qwen",
            "model": "qwen3-coder",
            "total_timeout_seconds": 45,
        },
    )

    assert response.status_code == 200
    request = adapter.requests[2]
    assert request.candidate_timeout_seconds == 30
    assert request.cli_candidate_timeout_seconds == 45
    assert request.total_timeout_seconds == 45

    # Explicit caller budgets retain precedence over the restored 600-second
    # spawn-cold default.
    response = client.post(
        "/api/llm/generate",
        json={
            "prompt": "Summarize this",
            "provider": "qwen",
            "model": "qwen3-coder",
            "cli_candidate_timeout_seconds": 60,
        },
    )

    assert response.status_code == 200
    request = adapter.requests[3]
    assert request.candidate_timeout_seconds is None
    assert request.cli_candidate_timeout_seconds == 60
    assert request.total_timeout_seconds is None


@pytest.mark.parametrize(
    "field",
    [
        "candidate_timeout_seconds",
        "cli_candidate_timeout_seconds",
        "total_timeout_seconds",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_generate_rejects_non_positive_timeout_overrides(
    client: TestClient,
    field: str,
    value: int,
) -> None:
    response = client.post(
        "/api/llm/generate",
        json={"prompt": "Summarize this", field: value},
    )

    assert response.status_code == 422


def test_generate_returns_503_without_text_generation_service(client: TestClient) -> None:
    response = client.post("/api/llm/generate", json={"prompt": "Summarize this"})

    assert response.status_code == 503
    assert response.json() == {"detail": "Text generation service not initialized"}


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": "Summarize this", "provider": "droid"},
        {"prompt": "Summarize this", "provider": "agy"},
        {"prompt": "Summarize this", "model": "qwen/qwen3.6-35b-a3b"},
    ],
)
def test_generate_rejects_partial_explicit_routing(
    client: TestClient,
    server_with_llm: MagicMock,
    payload: dict[str, str],
) -> None:
    droid = _FakeTextAdapter()
    local = _FakeTextAdapter()
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="droid",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("qwen/qwen3.6-35b-a3b",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="endpoint:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("qwen/qwen3.6-35b-a3b",),
            ),
        ]
    )
    service = TextGenerationService(registry, {"droid": droid, "endpoint:lm-studio": local})
    server_with_llm.services.text_generation_service = service

    response = client.post("/api/llm/generate", json=payload)

    assert response.status_code == 400
    assert response.json() == {
        "detail": "provider and model must be supplied together for explicit text generation routing"
    }
    assert droid.requests == []
    assert local.requests == []


def test_generate_defaults_to_feature_low_and_accepts_system_alias(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    codex = _FakeTextAdapter()
    local = _FakeTextAdapter()
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("gpt-5.6-luna", "gpt-5.4-mini"),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="endpoint:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("Qwen3-Coder-30B-A3B-Instruct",),
            ),
        ]
    )
    service = TextGenerationService(registry, {"codex": codex, "endpoint:lm-studio": local})
    server_with_llm.services.text_generation_service = service

    response = client.post(
        "/api/llm/generate",
        json={"prompt": "Summarize this", "system": "Be concise"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "text": "Generated text",
        "capability": "text_generate",
        "provider": "codex",
        "model": "gpt-5.6-luna",
    }
    assert codex.requests == [
        TextGenerationRequest(
            prompt="Summarize this",
            provider="codex",
            profile="feature_low",
            model="gpt-5.6-luna",
            system_prompt="Be concise",
            caller="llm-generate-route",
        )
    ]
    assert local.requests == []


def test_generate_explicit_candidates_bypass_default_profile_and_provider(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    codex = _FakeTextAdapter()
    local = _FakeTextAdapter()
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("gpt-5.3-codex-spark",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="endpoint:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("Qwen3-Coder-30B-A3B-Instruct",),
            ),
        ]
    )
    service = TextGenerationService(registry, {"codex": codex, "endpoint:lm-studio": local})
    server_with_llm.services.text_generation_service = service

    response = client.post(
        "/api/llm/generate",
        json={
            "prompt": "Summarize this",
            "provider": "codex",
            "model": "gpt-5.3-codex-spark",
            "candidates": ["endpoint:lm-studio/Qwen3-Coder-30B-A3B-Instruct"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "text": "Generated text",
        "capability": "text_generate",
        "provider": "endpoint:lm-studio",
        "model": "Qwen3-Coder-30B-A3B-Instruct",
    }
    assert codex.requests == []
    assert local.requests == [
        TextGenerationRequest(
            prompt="Summarize this",
            provider="endpoint:lm-studio",
            candidates=("endpoint:lm-studio/Qwen3-Coder-30B-A3B-Instruct",),
            model="Qwen3-Coder-30B-A3B-Instruct",
            caller="llm-generate-route",
        )
    ]


def test_generate_selects_candidate_with_slashed_local_model_id(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    local = _FakeTextAdapter()
    model = "qwen/qwen3-coder-30b"
    candidate = f"endpoint:lm-studio/{model}"
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="endpoint:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=(model,),
            )
        ]
    )
    service = TextGenerationService(registry, {"endpoint:lm-studio": local})
    server_with_llm.services.text_generation_service = service

    response = client.post(
        "/api/llm/generate",
        json={
            "prompt": "Summarize this",
            "candidates": [candidate],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "text": "Generated text",
        "capability": "text_generate",
        "provider": "endpoint:lm-studio",
        "model": model,
    }
    assert local.requests == [
        TextGenerationRequest(
            prompt="Summarize this",
            provider="endpoint:lm-studio",
            candidates=(candidate,),
            model=model,
            caller="llm-generate-route",
        )
    ]


def test_generate_falls_back_when_feature_mid_candidate_echoes_prompt(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    prompt = "Summarize this module once from lower-level summaries."
    candidates = ("qwen/qwen3-coder", "claude/sonnet")
    qwen = _FakeTextAdapter(text=prompt)
    claude = _FakeTextAdapter(text="Fallback prose")
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="qwen",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("qwen3-coder",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="claude",
                adapter_style=AIAdapterStyle.LLM_PROVIDER,
                available=True,
                models=("sonnet",),
            ),
        ]
    )
    service = TextGenerationService(registry, {"qwen": qwen, "claude": claude})
    server_with_llm.services.text_generation_service = service

    response = client.post(
        "/api/llm/generate",
        json={
            "prompt": prompt,
            "profile": "feature_mid",
            "candidates": list(candidates),
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "text": "Fallback prose",
        "capability": "text_generate",
        "provider": "claude",
        "model": "sonnet",
    }
    assert qwen.requests == [
        TextGenerationRequest(
            prompt=prompt,
            provider="qwen",
            profile="feature_mid",
            candidates=candidates,
            model="qwen3-coder",
            caller="llm-generate-route",
        )
    ]
    assert claude.requests == [
        TextGenerationRequest(
            prompt=prompt,
            provider="claude",
            profile="feature_mid",
            candidates=candidates,
            model="sonnet",
            caller="llm-generate-route",
        )
    ]


def test_generate_candidates_override_partial_top_level_provider(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    droid = _FakeTextAdapter()
    local = _FakeTextAdapter()
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="droid",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("qwen/qwen3.6-35b-a3b",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="endpoint:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("Qwen3-Coder-30B-A3B-Instruct",),
            ),
        ]
    )
    service = TextGenerationService(registry, {"droid": droid, "endpoint:lm-studio": local})
    server_with_llm.services.text_generation_service = service

    response = client.post(
        "/api/llm/generate",
        json={
            "prompt": "Summarize this",
            "provider": "droid",
            "candidates": ["endpoint:lm-studio/Qwen3-Coder-30B-A3B-Instruct"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "text": "Generated text",
        "capability": "text_generate",
        "provider": "endpoint:lm-studio",
        "model": "Qwen3-Coder-30B-A3B-Instruct",
    }
    assert droid.requests == []
    assert local.requests == [
        TextGenerationRequest(
            prompt="Summarize this",
            provider="endpoint:lm-studio",
            candidates=("endpoint:lm-studio/Qwen3-Coder-30B-A3B-Instruct",),
            model="Qwen3-Coder-30B-A3B-Instruct",
            caller="llm-generate-route",
        )
    ]


def test_generate_accepts_structured_candidates_and_returns_applied_reasoning_effort(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    codex = _FakeTextAdapter()
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("gpt-5.6-sol",),
            ),
        ]
    )
    service = TextGenerationService(registry, {"codex": codex})
    server_with_llm.services.text_generation_service = service

    response = client.post(
        "/api/llm/generate",
        json={
            "prompt": "Summarize this",
            "candidates": [{"candidate": "codex/gpt-5.6-sol", "reasoning_effort": "xhigh"}],
            "reasoning_effort": "low",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "text": "Generated text",
        "capability": "text_generate",
        "provider": "codex",
        "model": "gpt-5.6-sol",
        "applied_reasoning_effort": "low",
    }
    assert codex.requests == [
        TextGenerationRequest(
            prompt="Summarize this",
            provider="codex",
            candidates=(
                FeatureCandidateConfig(candidate="codex/gpt-5.6-sol", reasoning_effort="xhigh"),
            ),
            model="gpt-5.6-sol",
            reasoning_effort="low",
            caller="llm-generate-route",
        )
    ]


def test_generate_includes_usage_when_available(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    usage = {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}
    adapter = _FakeTextAdapter(usage=usage)
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="endpoint:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("local-model",),
            )
        ]
    )
    service = TextGenerationService(registry, {"endpoint:lm-studio": adapter})
    server_with_llm.services.text_generation_service = service

    response = client.post(
        "/api/llm/generate",
        json={
            "prompt": "Summarize this",
            "provider": "endpoint:lm-studio",
            "model": "local-model",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "text": "Generated text",
        "capability": "text_generate",
        "provider": "endpoint:lm-studio",
        "model": "local-model",
        "usage": usage,
    }


def test_generate_returns_real_usage_for_claude_path(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    usage = {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
        "input_tokens": 120,
        "output_tokens": 30,
    }
    adapter = _FakeTextAdapter(usage=usage)
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="claude",
                adapter_style=AIAdapterStyle.LLM_PROVIDER,
                available=True,
                models=("claude-sonnet-4-5",),
            )
        ]
    )
    service = TextGenerationService(registry, {"claude": adapter})
    server_with_llm.services.text_generation_service = service

    response = client.post(
        "/api/llm/generate",
        json={
            "prompt": "Summarize this",
            "provider": "claude",
            "model": "claude-sonnet-4-5",
            "max_tokens": 64,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "claude"
    assert body["usage"] == usage
    # The caller's max_tokens is forwarded to the provider.
    assert adapter.requests[0].max_tokens == 64


def test_generate_returns_deterministic_unavailable_error(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding.unavailable(
                AICapability.TEXT_GENERATE,
                "agy",
                adapter_style=AIAdapterStyle.CLI,
                reason="AGY CLI is not installed.",
                models=("gemini-3.5-flash",),
            )
        ]
    )
    service = TextGenerationService(registry, {})
    server_with_llm.services.text_generation_service = service

    response = client.post(
        "/api/llm/generate",
        json={
            "prompt": "Summarize this",
            "provider": "agy",
            "model": "gemini-3.5-flash",
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "code": "capability_unavailable",
        "capability": "text_generate",
        "provider": "agy",
        "model": "gemini-3.5-flash",
        "reason": "AGY CLI is not installed.",
    }


def test_generate_explicit_agy_model_succeeds(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    adapter = _FakeTextAdapter(text="AGY text")
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="agy",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("gemini-3.5-flash",),
                strict_models=True,
            )
        ]
    )
    service = TextGenerationService(registry, {"agy": adapter})
    server_with_llm.services.text_generation_service = service

    response = client.post(
        "/api/llm/generate",
        json={
            "prompt": "Summarize this",
            "provider": "agy",
            "model": "gemini-3.5-flash",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "text": "AGY text",
        "capability": "text_generate",
        "provider": "agy",
        "model": "gemini-3.5-flash",
        "applied_reasoning_effort": "low",
    }
    assert adapter.requests == [
        TextGenerationRequest(
            prompt="Summarize this",
            provider="agy",
            model="gemini-3.5-flash",
            reasoning_effort="low",
            caller="llm-generate-route",
        )
    ]


def test_generate_explicit_agy_bad_model_fails_before_adapter(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    adapter = _FakeTextAdapter(text="should not run")
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="agy",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("gemini-3.5-flash",),
                strict_models=True,
            )
        ]
    )
    service = TextGenerationService(registry, {"agy": adapter})
    server_with_llm.services.text_generation_service = service

    response = client.post(
        "/api/llm/generate",
        json={"prompt": "Summarize this", "provider": "agy", "model": "bad-model"},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "capability_unavailable"
    assert body["provider"] == "agy"
    assert body["model"] == "bad-model"
    assert "does not support requested model" in body["reason"]
    assert adapter.requests == []


def test_generate_explicit_agy_reasoning_effort_reaches_adapter(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    adapter = _FakeTextAdapter(text="AGY text")
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="agy",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("gemini-3.5-flash",),
                strict_models=True,
            )
        ]
    )
    service = TextGenerationService(registry, {"agy": adapter})
    server_with_llm.services.text_generation_service = service

    response = client.post(
        "/api/llm/generate",
        json={
            "prompt": "Summarize this",
            "provider": "agy",
            "model": "gemini-3.5-flash",
            "reasoning_effort": "medium",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "text": "AGY text",
        "capability": "text_generate",
        "provider": "agy",
        "model": "gemini-3.5-flash",
        "applied_reasoning_effort": "medium",
    }
    assert adapter.requests == [
        TextGenerationRequest(
            prompt="Summarize this",
            provider="agy",
            model="gemini-3.5-flash",
            reasoning_effort="medium",
            caller="llm-generate-route",
        )
    ]


def test_generate_returns_aggregated_unavailable_error_for_profile_candidates(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding.unavailable(
                AICapability.TEXT_GENERATE,
                "claude",
                adapter_style=AIAdapterStyle.LLM_PROVIDER,
                reason="Claude CLI is not installed.",
                models=("haiku",),
            ),
            CapabilityBinding.unavailable(
                AICapability.TEXT_GENERATE,
                "codex",
                adapter_style=AIAdapterStyle.DAEMON,
                reason="Codex app server is not available.",
                models=("gpt-5.4-mini",),
            ),
        ]
    )
    service = TextGenerationService(registry, {})
    server_with_llm.services.text_generation_service = service

    response = client.post(
        "/api/llm/generate",
        json={
            "prompt": "Summarize this",
            "profile": "feature_low",
            "candidates": ("claude/haiku", "codex/gpt-5.4-mini"),
        },
    )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "capability_unavailable"
    assert body["capability"] == "text_generate"
    assert body["provider"] is None
    assert body["model"] is None
    assert body["reason"].startswith("All text generation candidates unavailable:")
    assert "provider=claude" in body["reason"]
    assert "provider=codex" in body["reason"]


def test_vision_status_lists_only_proven_providers_as_available(
    client: TestClient,
) -> None:
    response = client.get("/api/llm/vision/status")

    assert response.status_code == 200
    data = response.json()
    assert data["capability"] == "vision_extract"
    assert data["available"] is True

    available_providers = {
        binding["provider"] for binding in data["bindings"] if binding["available"]
    }
    providers = {binding["provider"] for binding in data["bindings"]}
    assert "endpoint:lm-studio" in available_providers
    assert "local" not in providers
    assert not {"codex", "droid", "grok", "qwen"} & available_providers


def test_vision_extract_upload_executes_service(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    service = _FakeVisionService()

    with patch(
        "gobby.servers.routes.llm.build_daemon_vision_extract_service",
        return_value=service,
    ) as build_service:
        response = client.post(
            "/api/llm/vision/extract",
            data={
                "provider": "endpoint:lm-studio",
                "model": "llava",
                "context": "settings screenshot",
            },
            files={"file": ("screen.png", b"image bytes", "image/png")},
        )

    assert response.status_code == 200
    build_service.assert_called_once_with(server_with_llm.config)
    data = response.json()
    assert data == {
        "text": "Screen text",
        "description": "Screen text",
        "ocr_text": "Button label",
        "bytes": len(b"image bytes"),
        "content_type": "image/png",
        "capability": "vision_extract",
        "provider": "endpoint:lm-studio",
        "model": "llava",
    }

    assert service.request is not None
    assert service.request.provider == "endpoint:lm-studio"
    assert service.request.model == "llava"
    assert service.request.context == "settings screenshot"
    assert Path(service.request.image_path).exists() is False


def test_vision_extract_upload_preserves_missing_ocr_text(
    client: TestClient,
) -> None:
    service = _FakeVisionService(ocr_text=None)

    with patch(
        "gobby.servers.routes.llm.build_daemon_vision_extract_service",
        return_value=service,
    ):
        response = client.post(
            "/api/llm/vision/extract",
            data={
                "provider": "endpoint:lm-studio",
                "model": "llava",
                "context": "no visible text",
            },
            files={"file": ("screen.png", b"image bytes", "image/png")},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["description"] == "Screen text"
    assert data["ocr_text"] is None
    assert data["ocr_text"] != data["description"]


@pytest.mark.asyncio
async def test_bounded_vision_upload_read_accepts_exact_limit() -> None:
    remaining = bytearray(b"x" * MAX_IMAGE_BYTES)

    async def read(size: int) -> bytes:
        chunk = bytes(remaining[:size])
        del remaining[:size]
        return chunk

    upload = MagicMock()
    upload.read = AsyncMock(side_effect=read)

    image_bytes = await llm_module._read_bounded_image_upload(upload)

    assert len(image_bytes) == MAX_IMAGE_BYTES
    assert upload.read.await_count > 1
    assert max(call.args[0] for call in upload.read.await_args_list) <= 1024 * 1024


def test_vision_extract_rejects_oversize_before_temp_write(client: TestClient) -> None:
    with patch.object(llm_module, "_write_temp_image") as write_temp_image:
        response = client.post(
            "/api/llm/vision/extract",
            files={
                "file": (
                    "oversize.png",
                    b"x" * (MAX_IMAGE_BYTES + 1),
                    "image/png",
                )
            },
        )

    assert response.status_code == 413
    assert response.json() == {"detail": f"Image exceeds {MAX_IMAGE_BYTES} byte limit"}
    write_temp_image.assert_not_called()


@pytest.mark.parametrize(
    ("error", "status_code", "detail"),
    [
        (VisionInputError("Image not found"), 400, "Image not found"),
        (VisionProviderError("provider crashed"), 500, "Vision extraction failed"),
    ],
)
def test_vision_extract_maps_structured_provider_errors(
    client: TestClient,
    error: Exception,
    status_code: int,
    detail: str,
) -> None:
    with patch(
        "gobby.servers.routes.llm.build_daemon_vision_extract_service",
        return_value=_FailingVisionService(error),
    ):
        response = client.post(
            "/api/llm/vision/extract",
            files={"file": ("screen.png", b"image bytes", "image/png")},
        )

    assert response.status_code == status_code
    assert response.json() == {"detail": detail}
    assert "description" not in response.json()


def test_vision_extract_never_returns_error_sentinel_as_200(client: TestClient) -> None:
    service = _FakeVisionService(text="Image description failed: provider crashed")

    with patch(
        "gobby.servers.routes.llm.build_daemon_vision_extract_service",
        return_value=service,
    ):
        response = client.post(
            "/api/llm/vision/extract",
            files={"file": ("screen.png", b"image bytes", "image/png")},
        )

    assert response.status_code == 500
    assert response.json() == {"detail": "Vision extraction failed"}
    assert "Image description failed" not in response.text


def test_vision_extract_rejects_unproven_provider(client: TestClient) -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding.unavailable(
                AICapability.VISION_EXTRACT,
                "droid",
                adapter_style=AIAdapterStyle.CLI,
                reason=(
                    "No daemon vision_extract adapter has proven image payload support for Droid."
                ),
            )
        ]
    )
    service = VisionExtractService(registry, {})

    with patch(
        "gobby.servers.routes.llm.build_daemon_vision_extract_service",
        return_value=service,
    ):
        response = client.post(
            "/api/llm/vision/extract",
            data={"provider": "droid"},
            files={"file": ("screen.png", b"image bytes", "image/png")},
        )

    assert response.status_code == 400
    assert response.json() == {
        "code": "capability_unavailable",
        "capability": "vision_extract",
        "provider": "droid",
        "model": None,
        "reason": "No daemon vision_extract adapter has proven image payload support for Droid.",
    }


def test_vision_extract_temp_write_failure_returns_500(client: TestClient) -> None:
    with patch.object(llm_module, "_write_temp_image", side_effect=RuntimeError("temp failed")):
        response = client.post(
            "/api/llm/vision/extract",
            files={"file": ("screen.png", b"fake image", "image/png")},
        )

    assert response.status_code == 500
    assert response.json()["detail"] == "Vision upload failed"


def test_write_temp_image_uses_dedicated_restrictive_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    image_path = llm_module._write_temp_image(b"image bytes", "screen.jpg")
    try:
        assert image_path.parent == tmp_path / "gobby-vision"
        assert image_path.name.startswith("vision-")
        assert image_path.suffix == ".jpg"
        assert image_path.read_bytes() == b"image bytes"
        assert stat.S_IMODE(image_path.stat().st_mode) == 0o600
    finally:
        image_path.unlink(missing_ok=True)


def test_vision_temp_dir_enforces_restrictive_mode_on_existing_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    temp_dir = tmp_path / "gobby-vision"
    temp_dir.mkdir(mode=0o755)
    temp_dir.chmod(0o755)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    assert llm_module._vision_temp_dir() == temp_dir
    assert stat.S_IMODE(temp_dir.stat().st_mode) == 0o700


def test_vision_temp_cleanup_task_skips_without_running_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    app = FastAPI()

    llm_module.start_vision_temp_cleanup_task(app)

    assert getattr(app.state, "vision_temp_cleanup_task", None) is None


def test_write_temp_image_raises_contextual_error_on_temp_dir_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_gettempdir() -> str:
        raise OSError("no temp")

    monkeypatch.setattr(tempfile, "gettempdir", fail_gettempdir)

    with pytest.raises(RuntimeError, match="gobby-vision"):
        llm_module._write_temp_image(b"image bytes", "screen.jpg")


def test_write_temp_image_wraps_temp_file_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        llm_module,
        "NamedTemporaryFile",
        MagicMock(side_effect=OSError("disk full")),
    )

    with pytest.raises(RuntimeError, match="Failed to write vision temp image"):
        llm_module._write_temp_image(b"image bytes", "screen.jpg")


def test_cleanup_stale_vision_temp_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    temp_dir = tmp_path / "gobby-vision"
    temp_dir.mkdir()
    old_file = temp_dir / "old.png"
    new_file = temp_dir / "new.png"
    old_file.write_bytes(b"old")
    new_file.write_bytes(b"new")
    now = 10_000.0
    old_timestamp = now - llm_module._VISION_TEMP_MAX_AGE_SECONDS - 1
    os.utime(old_file, (old_timestamp, old_timestamp))
    os.utime(new_file, (now, now))

    llm_module._cleanup_stale_vision_temp_files(now=now)

    assert not old_file.exists()
    assert new_file.exists()


@pytest.mark.asyncio
async def test_vision_temp_cleanup_task_cancels_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    app = FastAPI()

    llm_module.start_vision_temp_cleanup_task(app)
    task = app.state.vision_temp_cleanup_task

    assert not task.done()

    await llm_module.stop_vision_temp_cleanup_task(app)

    assert app.state.vision_temp_cleanup_task is None
    assert task.cancelled()


@pytest.mark.asyncio
async def test_vision_temp_cleanup_task_replaces_stale_loop_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _StaleTask:
        cancelled = False

        def done(self) -> bool:
            return False

        def get_loop(self) -> object:
            return object()

        def cancel(self) -> None:
            self.cancelled = True

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    old_task = _StaleTask()
    app = FastAPI()
    app.state.vision_temp_cleanup_task = old_task

    llm_module.start_vision_temp_cleanup_task(app)
    new_task = app.state.vision_temp_cleanup_task

    assert old_task.cancelled is True
    assert new_task is not old_task
    assert not new_task.done()

    await llm_module.stop_vision_temp_cleanup_task(app)


class _FakeToolChatService:
    """Stand-in for ToolChatService.chat_result in chat-completions route tests."""

    def __init__(
        self, result: ToolChatResult | None = None, *, error: Exception | None = None
    ) -> None:
        self.result = result
        self.error = error
        self.requests: list[ToolChatRequest] = []

    async def chat_result(self, request: ToolChatRequest) -> ToolChatResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


_READONLY_TOOL_POLICY = {"cli": "gcode", "tools": ["search", "outline"]}
_TOOL_CHAT_CALLER = "gwiki.ask.deep"
_TOOL_CHAT_REQUEST_ID = "019fc08a-1d63-4b23-bbc8-659d56bc4168"


def _valid_tool_chat_payload() -> dict[str, object]:
    return {
        "caller": _TOOL_CHAT_CALLER,
        "request_id": _TOOL_CHAT_REQUEST_ID,
        "messages": [{"role": "user", "content": "Investigate."}],
        "project_path": "/repo",
        "tool_policy": _READONLY_TOOL_POLICY,
    }


@pytest.mark.parametrize("missing_field", ["caller", "request_id"])
def test_chat_completions_requires_correlation_fields(
    client: TestClient,
    missing_field: str,
) -> None:
    payload = _valid_tool_chat_payload()
    payload.pop(missing_field)

    response = client.post("/api/llm/chat/completions", json=payload)

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("caller", "   "),
        ("request_id", "malformed"),
        ("request_id", "019fc08a-1d63-1b23-bbc8-659d56bc4168"),
    ],
)
def test_chat_completions_rejects_invalid_correlation_fields(
    client: TestClient,
    field: str,
    value: str,
) -> None:
    payload = _valid_tool_chat_payload()
    payload[field] = value

    response = client.post("/api/llm/chat/completions", json=payload)

    assert response.status_code == 422


def _chat_result(**overrides: object) -> ToolChatResult:
    base: dict[str, object] = {
        "text": "## Overview\n\nGrounded narrative citing src/app.py:12.",
        "provider": "claude",
        "model": "opus",
        "adapter_style": "llm_provider",
        "tool_use_count": 33,
        "turns": 39,
        "tools": {"gcode_search": 28, "gcode_outline": 5},
        "usage": {"input_tokens": 1000, "output_tokens": 200},
        "applied_reasoning_effort": "high",
        "stop_reason": "completed",
        "speed": {
            "requested": "standard",
            "effective": "standard",
            "status": "standard",
            "reason": None,
        },
    }
    base.update(overrides)
    return ToolChatResult(**base)  # type: ignore[arg-type]


def test_chat_completions_returns_openai_shape_with_investigation(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    service = _FakeToolChatService(_chat_result())
    server_with_llm.services.tool_chat_service = service

    response = client.post(
        "/api/llm/chat/completions",
        json={
            "messages": [
                {"role": "system", "content": "You write grounded code docs."},
                {"role": "user", "content": "Document the auth module."},
                {"role": "assistant", "content": "Which auth surface?"},
                {"role": "user", "content": "The login handler."},
            ],
            "caller": _TOOL_CHAT_CALLER,
            "request_id": _TOOL_CHAT_REQUEST_ID,
            "project_path": "/repo",
            "tool_policy": _READONLY_TOOL_POLICY,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "## Overview\n\nGrounded narrative citing src/app.py:12.",
                },
                "finish_reason": "stop",
            }
        ],
        "model": "opus",
        "investigation": {
            "caller": _TOOL_CHAT_CALLER,
            "request_id": _TOOL_CHAT_REQUEST_ID,
            "tool_use_count": 33,
            "turns": 39,
            "tools": {"gcode_search": 28, "gcode_outline": 5},
            "adapter_style": "llm_provider",
            "stop_reason": "completed",
        },
        "usage": {"input_tokens": 1000, "output_tokens": 200},
        "applied_reasoning_effort": "high",
        "speed": {
            "requested": "standard",
            "effective": "standard",
            "status": "standard",
            "reason": None,
        },
    }
    assert len(service.requests) == 1
    request = service.requests[0]
    assert request.system_prompt == "You write grounded code docs."
    assert request.prompt == (
        "User:\nDocument the auth module.\n\n"
        "Assistant:\nWhich auth surface?\n\n"
        "User:\nThe login handler."
    )
    assert request.project_path == "/repo"
    assert request.tool_policy.cli == "gcode"
    assert request.tool_policy.tools == ("search", "outline")
    assert request.tool_policy.allow_mutation is False
    # The route sends no provider name; the default profile drives selection.
    assert request.profile == "feature_high"
    assert request.provider is None
    assert request.caller == _TOOL_CHAT_CALLER
    assert request.request_id == _TOOL_CHAT_REQUEST_ID
    assert request.session_id == UUID("019fc08a-1d63-4b23-bbc8-659d56bc4168")
    assert request.speed_mode == "standard"


def test_chat_completions_uses_verified_agent_session_claim(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    claimed_session_id = UUID("129fc08a-1d63-4b23-bbc8-659d56bc4168")
    server_with_llm.auth_service.verified_agent_claims.return_value = SimpleNamespace(
        session_id=str(claimed_session_id),
        project_id="project-claimed",
    )
    service = _FakeToolChatService(_chat_result())
    server_with_llm.services.tool_chat_service = service

    response = client.post("/api/llm/chat/completions", json=_valid_tool_chat_payload())

    assert response.status_code == 200, response.text
    assert service.requests[0].session_id == claimed_session_id


@pytest.mark.parametrize(
    "tool_policy",
    [
        {"cli": "gwiki", "tools": ["search"]},
        {"cli": "gcode", "tools": ["index"], "allow_mutation": True},
    ],
)
def test_chat_completions_rejects_unmanaged_tool_policy(
    client: TestClient,
    server_with_llm: MagicMock,
    tool_policy: dict[str, object],
) -> None:
    server_with_llm.services.tool_chat_service = _FakeToolChatService(_chat_result())
    payload = _valid_tool_chat_payload()
    payload["tool_policy"] = tool_policy

    response = client.post("/api/llm/chat/completions", json=payload)

    assert response.status_code == 422


def test_chat_completions_requires_authenticated_session_header(client: TestClient) -> None:
    client.headers.pop("X-Gobby-Session-Id")

    response = client.post("/api/llm/chat/completions", json=_valid_tool_chat_payload())

    assert response.status_code == 401
    assert response.json() == {"detail": "Authenticated session header is required"}


def test_chat_completions_requires_a_tool_policy(client: TestClient) -> None:
    response = client.post(
        "/api/llm/chat/completions",
        json={
            "messages": [{"role": "user", "content": "Go."}],
            "caller": _TOOL_CHAT_CALLER,
            "request_id": _TOOL_CHAT_REQUEST_ID,
            "project_path": "/repo",
        },
    )
    # The caller must declare its tools; the route invents no policy.
    assert response.status_code == 422


def test_chat_completions_accepts_complete_limits_without_clamping(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    service = _FakeToolChatService(
        _chat_result(
            tool_use_count=0,
            turns=1,
            tools={},
            usage=None,
            applied_reasoning_effort=None,
            stop_reason="max_turns",
        )
    )
    server_with_llm.services.tool_chat_service = service

    response = client.post(
        "/api/llm/chat/completions",
        json={
            "messages": [{"role": "user", "content": "Investigate."}],
            "caller": _TOOL_CHAT_CALLER,
            "request_id": _TOOL_CHAT_REQUEST_ID,
            "project_path": "/repo",
            "tool_policy": _READONLY_TOOL_POLICY,
            "limits": {
                "max_turns": 500,
                "max_tool_calls": 24,
                "max_bytes_per_tool_result": 16_384,
                "tool_timeout_seconds": 300,
                "loop_timeout_seconds": 1_200,
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert "usage" not in body
    assert "applied_reasoning_effort" not in body
    assert body["choices"][0]["finish_reason"] == "length"
    assert service.requests[0].limits == ToolLoopLimits(max_turns=500, max_tool_calls=24)


def test_chat_completions_forwards_explicit_routing(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    service = _FakeToolChatService(_chat_result())
    server_with_llm.services.tool_chat_service = service

    response = client.post(
        "/api/llm/chat/completions",
        json={
            "messages": [{"role": "user", "content": "Go."}],
            "caller": _TOOL_CHAT_CALLER,
            "request_id": _TOOL_CHAT_REQUEST_ID,
            "project_path": "/repo",
            "tool_policy": _READONLY_TOOL_POLICY,
            "provider": "endpoint:lm-studio",
            "model": "gemma",
            "speed_mode": "fast",
        },
    )

    assert response.status_code == 200
    request = service.requests[0]
    assert request.provider == "endpoint:lm-studio"
    assert request.model == "gemma"
    assert request.speed_mode == "fast"
    # Explicit routing leaves the profile unset (no default override).
    assert request.profile is None


def test_chat_completions_rejects_messages_without_user_content(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    service = _FakeToolChatService(_chat_result())
    server_with_llm.services.tool_chat_service = service

    response = client.post(
        "/api/llm/chat/completions",
        json={
            "messages": [{"role": "system", "content": "Only a system prompt."}],
            "caller": _TOOL_CHAT_CALLER,
            "request_id": _TOOL_CHAT_REQUEST_ID,
            "project_path": "/repo",
            "tool_policy": _READONLY_TOOL_POLICY,
        },
    )

    assert response.status_code == 400
    assert "at least one non-empty user or assistant message" in response.json()["detail"]
    assert service.requests == []


def test_chat_completions_maps_capability_unavailable_to_400(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    service = _FakeToolChatService(
        error=CapabilityUnavailableError(AICapability.TOOL_CHAT, reason="No tool-capable binding")
    )
    server_with_llm.services.tool_chat_service = service

    response = client.post(
        "/api/llm/chat/completions",
        json={
            "messages": [{"role": "user", "content": "Go."}],
            "caller": _TOOL_CHAT_CALLER,
            "request_id": _TOOL_CHAT_REQUEST_ID,
            "project_path": "/repo",
            "tool_policy": _READONLY_TOOL_POLICY,
        },
    )

    assert response.status_code == 400
    assert AICapability.TOOL_CHAT.value in str(response.json())


def test_chat_completions_maps_value_error_to_400(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    service = _FakeToolChatService(error=ValueError("bad candidate"))
    server_with_llm.services.tool_chat_service = service

    response = client.post(
        "/api/llm/chat/completions",
        json={
            "messages": [{"role": "user", "content": "Go."}],
            "caller": _TOOL_CHAT_CALLER,
            "request_id": _TOOL_CHAT_REQUEST_ID,
            "project_path": "/repo",
            "tool_policy": _READONLY_TOOL_POLICY,
        },
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "bad candidate"}


def test_chat_completions_maps_unexpected_error_to_500(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    service = _FakeToolChatService(error=RuntimeError("boom"))
    server_with_llm.services.tool_chat_service = service

    response = client.post(
        "/api/llm/chat/completions",
        json={
            "messages": [{"role": "user", "content": "Go."}],
            "caller": _TOOL_CHAT_CALLER,
            "request_id": _TOOL_CHAT_REQUEST_ID,
            "project_path": "/repo",
            "tool_policy": _READONLY_TOOL_POLICY,
        },
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "tool_chat failed"}


def test_chat_completions_returns_503_without_config(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    server_with_llm.config = None

    response = client.post(
        "/api/llm/chat/completions",
        json={
            "messages": [{"role": "user", "content": "Go."}],
            "caller": _TOOL_CHAT_CALLER,
            "request_id": _TOOL_CHAT_REQUEST_ID,
            "project_path": "/repo",
            "tool_policy": _READONLY_TOOL_POLICY,
        },
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Daemon config not found"}


def test_chat_completions_returns_503_without_service(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    server_with_llm.services.tool_chat_service = None

    response = client.post(
        "/api/llm/chat/completions",
        json={
            "messages": [{"role": "user", "content": "Go."}],
            "caller": _TOOL_CHAT_CALLER,
            "request_id": _TOOL_CHAT_REQUEST_ID,
            "project_path": "/repo",
            "tool_policy": _READONLY_TOOL_POLICY,
        },
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Tool chat service not initialized"}


def test_strip_leading_preamble_drops_non_markdown_prefix() -> None:
    from gobby.llm.claude_payloads import strip_leading_preamble

    prefixed = "Now I have the evidence I need.\n\n## Overview\n\nBody."
    assert strip_leading_preamble(prefixed) == "## Overview\n\nBody."

    top_level = "Preamble line\n# Title\n\nBody."
    assert strip_leading_preamble(top_level) == "# Title\n\nBody."

    no_heading = "   Just prose, no heading.   "
    assert strip_leading_preamble(no_heading) == "Just prose, no heading."

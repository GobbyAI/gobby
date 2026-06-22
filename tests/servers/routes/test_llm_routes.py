from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import gobby.servers.routes.llm as llm_module
from gobby.ai import (
    AIAdapterStyle,
    AICapability,
    AICapabilityRegistry,
    CapabilityBinding,
    TextGenerationRequest,
    TextGenerationService,
    VisionExtractRequest,
    VisionExtractResult,
    VisionExtractService,
)
from gobby.config.ai import AIConfig, GenerationConfig, LocalGenerationConfig
from gobby.config.app import DaemonConfig
from gobby.config.feature_base import FeatureCandidateConfig
from gobby.llm.base import LLMTextResult
from gobby.servers.routes.llm import create_llm_router

pytestmark = pytest.mark.unit


class _FakeVisionService:
    def __init__(self, *, ocr_text: str | None = "Button label") -> None:
        self.request: VisionExtractRequest | None = None
        self.ocr_text = ocr_text

    async def extract(self, request: VisionExtractRequest) -> VisionExtractResult:
        assert Path(request.image_path).exists()
        self.request = request
        return VisionExtractResult(
            text="Screen text",
            capability=AICapability.VISION_EXTRACT,
            provider=request.provider or "local:lm-studio",
            model=request.model or "llava",
            ocr_text=self.ocr_text,
        )


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
                local=LocalGenerationConfig(
                    endpoints={
                        "lm-studio": {
                            "api_base": "http://localhost:1234/v1",
                            "model": "llava",
                            "vision_extract": True,
                        }
                    }
                )
            )
        )
    )
    server.services = SimpleNamespace(text_generation_service=None)
    return server


@pytest.fixture
def client(server_with_llm: MagicMock) -> TestClient:
    app = FastAPI()
    app.include_router(create_llm_router(server_with_llm))
    return TestClient(app)


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
        assert "local:lm-studio" in providers


def test_create_llm_router_does_not_run_vision_temp_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    server_with_llm: MagicMock,
) -> None:
    cleanup = MagicMock()
    monkeypatch.setattr(llm_module, "_run_vision_temp_cleanup_once", cleanup)

    router = create_llm_router(server_with_llm)

    assert router.prefix == "/api/llm"
    assert any(route.path.endswith("/status") for route in router.routes)
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
                provider="local:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("qwen/qwen3.6-35b-a3b",),
            ),
        ]
    )
    service = TextGenerationService(registry, {"droid": droid, "local:lm-studio": local})
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
                models=("gpt-5.4-mini",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="local:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("Qwen3-Coder-30B-A3B-Instruct",),
            ),
        ]
    )
    service = TextGenerationService(registry, {"codex": codex, "local:lm-studio": local})
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
        "model": "gpt-5.4-mini",
    }
    assert codex.requests == [
        TextGenerationRequest(
            prompt="Summarize this",
            provider="codex",
            profile="feature_low",
            model="gpt-5.4-mini",
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
                provider="local:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("Qwen3-Coder-30B-A3B-Instruct",),
            ),
        ]
    )
    service = TextGenerationService(registry, {"codex": codex, "local:lm-studio": local})
    server_with_llm.services.text_generation_service = service

    response = client.post(
        "/api/llm/generate",
        json={
            "prompt": "Summarize this",
            "provider": "codex",
            "model": "gpt-5.3-codex-spark",
            "candidates": ["local:lm-studio/Qwen3-Coder-30B-A3B-Instruct"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "text": "Generated text",
        "capability": "text_generate",
        "provider": "local:lm-studio",
        "model": "Qwen3-Coder-30B-A3B-Instruct",
    }
    assert codex.requests == []
    assert local.requests == [
        TextGenerationRequest(
            prompt="Summarize this",
            provider="local:lm-studio",
            candidates=("local:lm-studio/Qwen3-Coder-30B-A3B-Instruct",),
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
    candidate = f"local:lm-studio/{model}"
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="local:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=(model,),
            )
        ]
    )
    service = TextGenerationService(registry, {"local:lm-studio": local})
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
        "provider": "local:lm-studio",
        "model": model,
    }
    assert local.requests == [
        TextGenerationRequest(
            prompt="Summarize this",
            provider="local:lm-studio",
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
                provider="local:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("Qwen3-Coder-30B-A3B-Instruct",),
            ),
        ]
    )
    service = TextGenerationService(registry, {"droid": droid, "local:lm-studio": local})
    server_with_llm.services.text_generation_service = service

    response = client.post(
        "/api/llm/generate",
        json={
            "prompt": "Summarize this",
            "provider": "droid",
            "candidates": ["local:lm-studio/Qwen3-Coder-30B-A3B-Instruct"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "text": "Generated text",
        "capability": "text_generate",
        "provider": "local:lm-studio",
        "model": "Qwen3-Coder-30B-A3B-Instruct",
    }
    assert droid.requests == []
    assert local.requests == [
        TextGenerationRequest(
            prompt="Summarize this",
            provider="local:lm-studio",
            candidates=("local:lm-studio/Qwen3-Coder-30B-A3B-Instruct",),
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
                models=("gpt-5.5",),
            ),
        ]
    )
    service = TextGenerationService(registry, {"codex": codex})
    server_with_llm.services.text_generation_service = service

    response = client.post(
        "/api/llm/generate",
        json={
            "prompt": "Summarize this",
            "candidates": [{"candidate": "codex/gpt-5.5", "reasoning_effort": "xhigh"}],
            "reasoning_effort": "low",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "text": "Generated text",
        "capability": "text_generate",
        "provider": "codex",
        "model": "gpt-5.5",
        "applied_reasoning_effort": "low",
    }
    assert codex.requests == [
        TextGenerationRequest(
            prompt="Summarize this",
            provider="codex",
            candidates=(
                FeatureCandidateConfig(candidate="codex/gpt-5.5", reasoning_effort="xhigh"),
            ),
            model="gpt-5.5",
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
                provider="local:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("local-model",),
            )
        ]
    )
    service = TextGenerationService(registry, {"local:lm-studio": adapter})
    server_with_llm.services.text_generation_service = service

    response = client.post(
        "/api/llm/generate",
        json={
            "prompt": "Summarize this",
            "provider": "local:lm-studio",
            "model": "local-model",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "text": "Generated text",
        "capability": "text_generate",
        "provider": "local:lm-studio",
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
                models=("gemini-3.5-flash-low",),
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
            "model": "gemini-3.5-flash-low",
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "code": "capability_unavailable",
        "capability": "text_generate",
        "provider": "agy",
        "model": "gemini-3.5-flash-low",
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
                models=("gemini-3.5-flash-low",),
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
            "model": "gemini-3.5-flash-low",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "text": "AGY text",
        "capability": "text_generate",
        "provider": "agy",
        "model": "gemini-3.5-flash-low",
    }
    assert adapter.requests == [
        TextGenerationRequest(
            prompt="Summarize this",
            provider="agy",
            model="gemini-3.5-flash-low",
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
                models=("gemini-3.5-flash-low",),
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
    assert "local:lm-studio" in available_providers
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
                "provider": "local:lm-studio",
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
        "provider": "local:lm-studio",
        "model": "llava",
    }

    assert service.request is not None
    assert service.request.provider == "local:lm-studio"
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
                "provider": "local:lm-studio",
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
    monkeypatch.setattr(llm_module.tempfile, "gettempdir", lambda: str(tmp_path))

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
    monkeypatch.setattr(llm_module.tempfile, "gettempdir", lambda: str(tmp_path))

    assert llm_module._vision_temp_dir() == temp_dir
    assert stat.S_IMODE(temp_dir.stat().st_mode) == 0o700


def test_vision_temp_cleanup_task_skips_without_running_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(llm_module.tempfile, "gettempdir", lambda: str(tmp_path))
    app = FastAPI()

    llm_module.start_vision_temp_cleanup_task(app)

    assert getattr(app.state, "vision_temp_cleanup_task", None) is None


def test_write_temp_image_raises_contextual_error_on_temp_dir_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_gettempdir() -> str:
        raise OSError("no temp")

    monkeypatch.setattr(llm_module.tempfile, "gettempdir", fail_gettempdir)

    with pytest.raises(RuntimeError, match="gobby-vision"):
        llm_module._write_temp_image(b"image bytes", "screen.jpg")


def test_write_temp_image_wraps_temp_file_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(llm_module.tempfile, "gettempdir", lambda: str(tmp_path))
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
    monkeypatch.setattr(llm_module.tempfile, "gettempdir", lambda: str(tmp_path))
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
    monkeypatch.setattr(llm_module.tempfile, "gettempdir", lambda: str(tmp_path))
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

    monkeypatch.setattr(llm_module.tempfile, "gettempdir", lambda: str(tmp_path))
    old_task = _StaleTask()
    app = FastAPI()
    app.state.vision_temp_cleanup_task = old_task

    llm_module.start_vision_temp_cleanup_task(app)
    new_task = app.state.vision_temp_cleanup_task

    assert old_task.cancelled is True
    assert new_task is not old_task
    assert not new_task.done()

    await llm_module.stop_vision_temp_cleanup_task(app)

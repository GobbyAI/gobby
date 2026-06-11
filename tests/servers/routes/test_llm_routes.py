from __future__ import annotations

import os
import stat
from pathlib import Path
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
from gobby.llm.base import LLMTextResult
from gobby.servers.routes.llm import create_llm_router

pytestmark = pytest.mark.unit


class _FakeVisionService:
    def __init__(self) -> None:
        self.request: VisionExtractRequest | None = None

    async def extract(self, request: VisionExtractRequest) -> VisionExtractResult:
        assert Path(request.image_path).exists()
        self.request = request
        return VisionExtractResult(
            text="Screen text",
            capability=AICapability.VISION_EXTRACT,
            provider=request.provider or "local:lm-studio",
            model=request.model or "llava",
            ocr_text="Button label",
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
        assert "local" not in providers


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


def test_generate_selects_acp_backed_provider(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    adapter = _FakeTextAdapter()
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="gemini",
                adapter_style=AIAdapterStyle.ACP,
                available=True,
                models=("gemini-pro",),
            )
        ]
    )
    service = TextGenerationService(registry, {"gemini": adapter})

    with patch(
        "gobby.servers.routes.llm.build_daemon_text_generation_service",
        return_value=service,
    ) as build_service:
        response = client.post(
            "/api/llm/generate",
            json={
                "prompt": "Summarize this",
                "provider": "gemini",
                "model": "gemini-pro",
                "system_prompt": "Be concise",
                "max_tokens": 128,
                "cwd": "/tmp/project",
            },
        )

    assert response.status_code == 200
    build_service.assert_called_once_with(server_with_llm.config)
    assert response.json() == {
        "text": "Generated text",
        "capability": "text_generate",
        "provider": "gemini",
        "model": "gemini-pro",
    }
    assert adapter.requests == [
        TextGenerationRequest(
            prompt="Summarize this",
            provider="gemini",
            model="gemini-pro",
            system_prompt="Be concise",
            max_tokens=128,
            caller="llm-generate-route",
            cwd="/tmp/project",
        )
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": "Summarize this", "provider": "droid"},
        {"prompt": "Summarize this", "model": "qwen/qwen3.6-35b-a3b"},
    ],
)
def test_generate_rejects_partial_explicit_routing(
    client: TestClient,
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

    with patch(
        "gobby.servers.routes.llm.build_daemon_text_generation_service",
        return_value=service,
    ):
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

    with patch(
        "gobby.servers.routes.llm.build_daemon_text_generation_service",
        return_value=service,
    ) as build_service:
        response = client.post(
            "/api/llm/generate",
            json={"prompt": "Summarize this", "system": "Be concise"},
        )

    assert response.status_code == 200
    build_service.assert_called_once_with(server_with_llm.config)
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

    with patch(
        "gobby.servers.routes.llm.build_daemon_text_generation_service",
        return_value=service,
    ):
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


def test_generate_falls_back_when_feature_mid_candidate_echoes_prompt(
    client: TestClient,
    server_with_llm: MagicMock,
) -> None:
    prompt = "Summarize this module once from lower-level summaries."
    candidates = ("codex/gpt-5.3-codex-spark", "claude/sonnet")
    codex = _FakeTextAdapter(text=prompt)
    claude = _FakeTextAdapter(text="Fallback prose")
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
                provider="claude",
                adapter_style=AIAdapterStyle.LLM_PROVIDER,
                available=True,
                models=("sonnet",),
            ),
        ]
    )
    service = TextGenerationService(registry, {"codex": codex, "claude": claude})

    with patch(
        "gobby.servers.routes.llm.build_daemon_text_generation_service",
        return_value=service,
    ) as build_service:
        response = client.post(
            "/api/llm/generate",
            json={
                "prompt": prompt,
                "profile": "feature_mid",
                "candidates": list(candidates),
            },
        )

    assert response.status_code == 200
    build_service.assert_called_once_with(server_with_llm.config)
    assert response.json() == {
        "text": "Fallback prose",
        "capability": "text_generate",
        "provider": "claude",
        "model": "sonnet",
    }
    assert codex.requests == [
        TextGenerationRequest(
            prompt=prompt,
            provider="codex",
            profile="feature_mid",
            candidates=candidates,
            model="gpt-5.3-codex-spark",
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

    with patch(
        "gobby.servers.routes.llm.build_daemon_text_generation_service",
        return_value=service,
    ):
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

    with patch(
        "gobby.servers.routes.llm.build_daemon_text_generation_service",
        return_value=service,
    ) as build_service:
        response = client.post(
            "/api/llm/generate",
            json={
                "prompt": "Summarize this",
                "provider": "local:lm-studio",
                "model": "local-model",
            },
        )

    assert response.status_code == 200
    build_service.assert_called_once_with(server_with_llm.config)
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

    with patch(
        "gobby.servers.routes.llm.build_daemon_text_generation_service",
        return_value=service,
    ):
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


def test_generate_returns_deterministic_unavailable_error(client: TestClient) -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding.unavailable(
                AICapability.TEXT_GENERATE,
                "gemini",
                adapter_style=AIAdapterStyle.ACP,
                reason="Gemini CLI is not installed.",
            )
        ]
    )
    service = TextGenerationService(registry, {})

    with patch(
        "gobby.servers.routes.llm.build_daemon_text_generation_service",
        return_value=service,
    ):
        response = client.post(
            "/api/llm/generate",
            json={"prompt": "Summarize this", "provider": "gemini", "model": "gemini-pro"},
        )

    assert response.status_code == 400
    assert response.json() == {
        "code": "capability_unavailable",
        "capability": "text_generate",
        "provider": "gemini",
        "model": "gemini-pro",
        "reason": "Gemini CLI is not installed.",
    }


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
    assert not {"codex", "droid", "gemini", "grok", "qwen"} & available_providers


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

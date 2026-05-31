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
from gobby.config.app import DaemonConfig
from gobby.config.local import LocalConfig
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
            provider=request.provider or "local",
            model=request.model or "llava",
        )


class _FakeTextAdapter:
    def __init__(self) -> None:
        self.requests: list[TextGenerationRequest] = []

    async def generate(self, request: TextGenerationRequest) -> str:
        self.requests.append(request)
        return "Generated text"


@pytest.fixture
def server_with_llm() -> MagicMock:
    server = MagicMock()
    server.config = DaemonConfig(local=LocalConfig(url="http://localhost:1234/v1", model="llava"))
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
            json={"prompt": "Summarize this", "provider": "gemini"},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "Gemini CLI is not installed. (provider=gemini)"}


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
    assert "local" in available_providers
    assert not {"droid", "gemini", "grok", "qwen"} & available_providers


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
                "provider": "local",
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
        "bytes": len(b"image bytes"),
        "content_type": "image/png",
        "capability": "vision_extract",
        "provider": "local",
        "model": "llava",
    }

    assert service.request is not None
    assert service.request.provider == "local"
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
        "detail": (
            "No daemon vision_extract adapter has proven image payload support for Droid. "
            "(provider=droid)"
        )
    }


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

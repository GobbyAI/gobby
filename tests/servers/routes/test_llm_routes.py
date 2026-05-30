from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.ai import AICapability, VisionExtractRequest, VisionExtractResult
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

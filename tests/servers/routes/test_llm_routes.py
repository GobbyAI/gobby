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
            ocr_text="Button label",
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
    assert response.json() == {
        "code": "capability_unavailable",
        "capability": "text_generate",
        "provider": "gemini",
        "model": None,
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
        "description": "Screen text",
        "ocr_text": "Button label",
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

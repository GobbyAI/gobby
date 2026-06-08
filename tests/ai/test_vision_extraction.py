from __future__ import annotations

from typing import ClassVar

import pytest

from gobby.ai import (
    AIAdapterStyle,
    AICapability,
    AICapabilityRegistry,
    CapabilityBinding,
    CapabilityUnavailableError,
    ClaudeVisionExtractAdapter,
    LocalVisionExtractAdapter,
    VisionExtractRequest,
    VisionExtractService,
)
from gobby.config.app import DaemonConfig

pytestmark = pytest.mark.unit


class _FakeVisionAdapter:
    def __init__(self) -> None:
        self.requests: list[VisionExtractRequest] = []

    async def extract(self, request: VisionExtractRequest) -> str:
        self.requests.append(request)
        return f"extracted:{request.image_path}"


class _FakeNativeVisionProvider:
    last_instance: ClassVar[_FakeNativeVisionProvider | None] = None

    def __init__(self, config: DaemonConfig) -> None:
        self.config = config
        self.calls: list[tuple[str, str | None, str | None]] = []
        self.__class__.last_instance = self

    async def describe_image(
        self,
        image_path: str,
        context: str | None = None,
        model: str | None = None,
    ) -> str:
        self.calls.append((image_path, context, model))
        return "described"


def _registry() -> AICapabilityRegistry:
    return AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.VISION_EXTRACT,
                provider="local",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("llava",),
            ),
            CapabilityBinding.unavailable(
                AICapability.VISION_EXTRACT,
                "droid",
                adapter_style=AIAdapterStyle.CLI,
                reason="No daemon vision_extract adapter has proven image payload support.",
            ),
        ]
    )


@pytest.mark.asyncio
async def test_vision_service_selects_extract_provider() -> None:
    adapter = _FakeVisionAdapter()
    service = VisionExtractService(_registry(), {"local": adapter})

    result = await service.extract(
        VisionExtractRequest(
            image_path="/tmp/image.png",
            provider="local",
            model="llava",
            context="screenshot",
        )
    )

    assert result.text == "extracted:/tmp/image.png"
    assert result.capability == AICapability.VISION_EXTRACT
    assert result.provider == "local"
    assert result.model == "llava"
    assert result.ocr_text == "extracted:/tmp/image.png"
    assert adapter.requests == [
        VisionExtractRequest(
            image_path="/tmp/image.png",
            provider="local",
            model="llava",
            context="screenshot",
        )
    ]


@pytest.mark.asyncio
async def test_vision_service_rejects_unproven_provider() -> None:
    service = VisionExtractService(_registry(), {"local": _FakeVisionAdapter()})

    with pytest.raises(CapabilityUnavailableError, match="proven image payload support"):
        await service.extract(VisionExtractRequest(image_path="/tmp/image.png", provider="droid"))


@pytest.mark.asyncio
async def test_claude_vision_adapter_forwards_image_context_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeNativeVisionProvider.last_instance = None
    monkeypatch.setattr("gobby.llm.claude.ClaudeLLMProvider", _FakeNativeVisionProvider)
    config = DaemonConfig()
    adapter = ClaudeVisionExtractAdapter(config)

    result = await adapter.extract(
        VisionExtractRequest(
            image_path="/tmp/screenshot.png",
            context="settings page",
            model="vision-model",
        )
    )

    provider = _FakeNativeVisionProvider.last_instance
    assert provider is not None
    assert provider.config is config
    assert result == "described"
    assert provider.calls == [("/tmp/screenshot.png", "settings page", "vision-model")]


@pytest.mark.asyncio
async def test_local_vision_adapter_forwards_image_context_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeNativeVisionProvider.last_instance = None
    monkeypatch.setattr("gobby.llm.local.LocalLLMProvider", _FakeNativeVisionProvider)
    config = DaemonConfig()
    adapter = LocalVisionExtractAdapter(config)

    result = await adapter.extract(
        VisionExtractRequest(
            image_path="/tmp/screenshot.png",
            context="settings page",
            model="vision-model",
        )
    )

    provider = _FakeNativeVisionProvider.last_instance
    assert provider is not None
    assert provider.config is config
    assert result == "described"
    assert provider.calls == [("/tmp/screenshot.png", "settings page", "vision-model")]

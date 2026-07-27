from __future__ import annotations

from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

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
from gobby.ai.vision import CodexEndpointVisionExtractAdapter
from gobby.config.app import DaemonConfig
from gobby.llm.base import VisionProviderError

pytestmark = pytest.mark.unit


class _FakeVisionAdapter:
    def __init__(self, text: str | None = None) -> None:
        self.requests: list[VisionExtractRequest] = []
        self.text = text

    async def extract(self, request: VisionExtractRequest) -> str:
        self.requests.append(request)
        return self.text or f"extracted:{request.image_path}"


@pytest.mark.asyncio
async def test_codex_endpoint_vision_stop_only_stops_connected_client() -> None:
    client = MagicMock()
    client.is_connected = False
    client.stop = AsyncMock()
    adapter = object.__new__(CodexEndpointVisionExtractAdapter)
    adapter._client = client

    await adapter.stop()
    client.stop.assert_not_awaited()

    client.is_connected = True
    await adapter.stop()
    client.stop.assert_awaited_once_with()


class _FakeNativeVisionProvider:
    last_instance: ClassVar[_FakeNativeVisionProvider | None] = None

    def __init__(self, config: DaemonConfig, endpoint_name: str | None = None) -> None:
        self.config = config
        self.endpoint_name = endpoint_name
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
                provider="endpoint:lm-studio",
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
async def test_vision_service_does_not_invent_ocr_text_from_description() -> None:
    adapter = _FakeVisionAdapter()
    service = VisionExtractService(_registry(), {"endpoint:lm-studio": adapter})

    assert service.is_available is True
    result = await service.extract(
        VisionExtractRequest(
            image_path="/tmp/image.png",
            provider="endpoint:lm-studio",
            model="llava",
            context="screenshot",
        )
    )

    assert result.text == "extracted:/tmp/image.png"
    assert result.capability == AICapability.VISION_EXTRACT
    assert result.provider == "endpoint:lm-studio"
    assert result.model == "llava"
    assert result.ocr_text is None
    assert result.ocr_text != result.text
    assert adapter.requests == [
        VisionExtractRequest(
            image_path="/tmp/image.png",
            provider="endpoint:lm-studio",
            model="llava",
            context="screenshot",
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sentinel",
    [
        "Image description unavailable (Claude CLI not found)",
        "Image description failed: provider crashed",
        "Image not found: /missing/image.png",
        "Failed to read image: denied",
    ],
)
async def test_vision_service_never_returns_provider_error_sentinels(sentinel: str) -> None:
    registry = _registry()
    service = VisionExtractService(registry, {"endpoint:lm-studio": _FakeVisionAdapter(sentinel)})

    with pytest.raises(VisionProviderError, match="error sentinel"):
        await service.extract(
            VisionExtractRequest(
                image_path="/tmp/image.png",
                provider="endpoint:lm-studio",
                model="llava",
            )
        )


@pytest.mark.asyncio
async def test_vision_service_rejects_unproven_provider() -> None:
    service = VisionExtractService(_registry(), {"endpoint:lm-studio": _FakeVisionAdapter()})

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
    assert provider.endpoint_name is None
    assert result == "described"
    assert provider.calls == [("/tmp/screenshot.png", "settings page", "vision-model")]


@pytest.mark.asyncio
async def test_local_vision_adapter_forwards_image_context_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeNativeVisionProvider.last_instance = None
    monkeypatch.setattr("gobby.llm.local.LocalLLMProvider", _FakeNativeVisionProvider)
    config = DaemonConfig()
    adapter = LocalVisionExtractAdapter(config, "lm-studio")

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
    assert provider.endpoint_name == "lm-studio"
    assert result == "described"
    assert provider.calls == [("/tmp/screenshot.png", "settings page", "vision-model")]

"""Daemon-owned vision extraction execution adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from gobby.ai.registry import (
    AICapability,
    AICapabilityRegistry,
    build_daemon_ai_capability_registry,
)
from gobby.config.app import DaemonConfig
from gobby.llm.base import LLMProvider


@dataclass(frozen=True, kw_only=True)
class VisionExtractRequest:
    """One daemon vision_extract request."""

    image_path: str
    provider: str | None = None
    model: str | None = None
    context: str | None = None
    caller: str | None = None


@dataclass(frozen=True, kw_only=True)
class VisionExtractResult:
    """Result from a selected daemon vision_extract binding."""

    text: str
    capability: AICapability
    provider: str
    model: str | None = None
    ocr_text: str | None = None


class VisionExtractAdapter(Protocol):
    """Adapter for one provider's vision_extract execution path."""

    async def extract(self, request: VisionExtractRequest) -> str:
        """Extract text description from an image."""


class VisionExtractService:
    """Select and execute daemon vision_extract capability bindings."""

    def __init__(
        self,
        registry: AICapabilityRegistry,
        adapters: Mapping[str, VisionExtractAdapter],
    ) -> None:
        self._registry = registry
        self._adapters = dict(adapters)

    @property
    def registry(self) -> AICapabilityRegistry:
        """Return the capability registry used for selection."""
        return self._registry

    async def extract(self, request: VisionExtractRequest) -> VisionExtractResult:
        """Select a vision_extract binding and invoke its adapter."""
        binding = self._registry.select(
            AICapability.VISION_EXTRACT,
            provider=request.provider,
            model=request.model,
        )
        adapter = self._adapters.get(binding.provider)
        if adapter is None:
            raise RuntimeError(
                f"No vision_extract adapter registered for provider {binding.provider!r}"
            )
        text = await adapter.extract(request)
        return VisionExtractResult(
            text=text,
            capability=AICapability.VISION_EXTRACT,
            provider=binding.provider,
            model=request.model or next(iter(binding.models), None),
        )


class LLMProviderVisionExtractAdapter:
    """Vision adapter for existing LLMProvider implementations."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    async def extract(self, request: VisionExtractRequest) -> str:
        return await self._provider.describe_image(
            request.image_path,
            context=request.context,
            model=request.model,
        )


def build_daemon_vision_extract_service(
    config: DaemonConfig,
    *,
    registry: AICapabilityRegistry | None = None,
) -> VisionExtractService:
    """Build the daemon vision_extract service from configured bindings."""
    return VisionExtractService(
        registry or build_daemon_ai_capability_registry(config),
        _daemon_vision_extract_adapters(config),
    )


def _daemon_vision_extract_adapters(config: DaemonConfig) -> dict[str, VisionExtractAdapter]:
    from gobby.llm.claude import ClaudeLLMProvider
    from gobby.llm.local import LocalLLMProvider

    adapters: dict[str, VisionExtractAdapter] = {
        "claude": LLMProviderVisionExtractAdapter(ClaudeLLMProvider(config)),
    }
    if config.local:
        adapters["local"] = LLMProviderVisionExtractAdapter(LocalLLMProvider(config))
    return adapters


__all__ = [
    "LLMProviderVisionExtractAdapter",
    "VisionExtractAdapter",
    "VisionExtractRequest",
    "VisionExtractResult",
    "VisionExtractService",
    "build_daemon_vision_extract_service",
]

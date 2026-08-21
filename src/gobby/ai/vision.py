"""Daemon-owned vision extraction execution adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from gobby.adapters.codex_impl.client import CodexAppServerClient
from gobby.ai.codex_endpoint import (
    codex_endpoint_app_server_env,
    codex_endpoint_config_overrides,
    codex_event_text,
)
from gobby.ai.endpoints import endpoint_provider
from gobby.ai.registry import (
    AICapability,
    AICapabilityRegistry,
    build_daemon_ai_capability_registry,
)
from gobby.config.app import DaemonConfig
from gobby.llm.base import validate_vision_description


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

    @property
    def is_available(self) -> bool:
        """Return whether at least one vision extraction binding is available."""
        return self._registry.status(AICapability.VISION_EXTRACT).available

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
        text = validate_vision_description(await adapter.extract(request))
        return VisionExtractResult(
            text=text,
            capability=AICapability.VISION_EXTRACT,
            provider=binding.provider,
            model=request.model or next(iter(binding.models), None),
        )

    async def stop(self) -> None:
        """Stop any lazily started daemon-backed vision adapters."""
        for adapter in self._adapters.values():
            if isinstance(adapter, CodexEndpointVisionExtractAdapter):
                await adapter.stop()


def _extraction_prompt(context: str | None, default: str) -> str:
    if context:
        return f"{context}\n\n{default}"
    return default


class ClaudeVisionExtractAdapter:
    """Native vision_extract adapter backed by Claude SDK primitives."""

    def __init__(self, config: DaemonConfig) -> None:
        from gobby.llm.claude import ClaudeLLMProvider

        self._provider = ClaudeLLMProvider(config)

    async def extract(self, request: VisionExtractRequest) -> str:
        prompt = _extraction_prompt(
            request.context,
            "Please describe this image in detail, focusing on the key visual "
            "elements and any text visible.",
        )
        result = await self._provider.generate_text_result(
            prompt,
            system_prompt="You are a vision assistant that describes images in detail.",
            model=request.model,
            images=[request.image_path],
        )
        return result.text


class LocalVisionExtractAdapter:
    """Native vision_extract adapter backed by a local OpenAI-compatible endpoint."""

    def __init__(self, config: DaemonConfig, endpoint_name: str) -> None:
        from gobby.llm.local import LocalLLMProvider

        self._provider = LocalLLMProvider(config, endpoint_name=endpoint_name)

    async def extract(self, request: VisionExtractRequest) -> str:
        prompt = _extraction_prompt(
            request.context,
            "Please describe this image in detail, focusing on key visual elements, "
            "any text visible, and the overall context or meaning.",
        )
        result = await self._provider.generate_text_result(
            prompt,
            model=request.model,
            max_tokens=1024,
            images=[request.image_path],
        )
        return result.text


class CodexEndpointVisionExtractAdapter:
    """Vision extraction through a dedicated Responses endpoint app-server."""

    def __init__(self, config: DaemonConfig, endpoint_name: str) -> None:
        self._endpoint = config.ai.generation.endpoints[endpoint_name]
        self._client = CodexAppServerClient(
            config_overrides=codex_endpoint_config_overrides(
                endpoint_name,
                self._endpoint,
            ),
            env_overrides=codex_endpoint_app_server_env(endpoint_name, self._endpoint),
        )
        self._start_lock = asyncio.Lock()

    async def _ensure_started(self) -> None:
        if self._client.is_connected:
            return
        async with self._start_lock:
            if not self._client.is_connected:
                await self._client.start()

    async def stop(self) -> None:
        if self._client.is_connected:
            await self._client.stop()

    async def extract(self, request: VisionExtractRequest) -> str:
        await self._ensure_started()
        thread = await self._client.start_thread(
            model=request.model or self._endpoint.model,
            approval_policy="never",
            sandbox="read-only",
            ephemeral=True,
        )
        prompt = request.context or "Describe this image accurately and concisely."
        deltas: list[str] = []
        completed: list[str] = []
        async for event in self._client.run_turn(
            thread.id,
            prompt,
            images=[str(request.image_path)],
        ):
            text = codex_event_text(event)
            if not text:
                continue
            if event.get("type") == "item/agentMessage/delta":
                deltas.append(text)
            elif event.get("type") == "item/completed":
                completed.append(text)
        result = "".join(deltas).strip() or "".join(completed).strip()
        if not result:
            raise RuntimeError("Codex Responses vision extraction produced no text")
        return result


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
    adapters: dict[str, VisionExtractAdapter] = {
        "claude": ClaudeVisionExtractAdapter(config),
    }
    for name, endpoint in config.ai.generation.endpoints.items():
        if endpoint.input_modalities is not None and "image" in endpoint.input_modalities:
            adapter: VisionExtractAdapter
            if endpoint.wire_api == "responses":
                adapter = CodexEndpointVisionExtractAdapter(config, name)
            else:
                adapter = LocalVisionExtractAdapter(config, name)
            adapters[endpoint_provider(name)] = adapter
    return adapters


__all__ = [
    "ClaudeVisionExtractAdapter",
    "LocalVisionExtractAdapter",
    "VisionExtractAdapter",
    "VisionExtractRequest",
    "VisionExtractResult",
    "VisionExtractService",
    "build_daemon_vision_extract_service",
]

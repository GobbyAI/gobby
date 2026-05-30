"""Daemon-owned text generation execution adapters."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import shutil
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from gobby.adapters.acp_client import ACPClient, StreamEvent
from gobby.adapters.gemini_acp_client import GeminiACPClient
from gobby.adapters.grok_acp_client import GrokACPClient
from gobby.adapters.qwen_acp_client import QwenACPClient
from gobby.ai.registry import (
    AICapability,
    AICapabilityRegistry,
    build_daemon_ai_capability_registry,
)
from gobby.config.app import DaemonConfig
from gobby.llm.base import LLMProvider


@dataclass(frozen=True, kw_only=True)
class TextGenerationRequest:
    """One daemon text_generate request."""

    prompt: str
    provider: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    max_tokens: int | None = None
    caller: str | None = None
    cwd: str | None = None


class TextGenerateAdapter(Protocol):
    """Adapter for one provider's text_generate execution path."""

    async def generate(self, request: TextGenerationRequest) -> str:
        """Generate text for the request."""


class TextGenerationService:
    """Select and execute daemon text_generate capability bindings."""

    def __init__(
        self,
        registry: AICapabilityRegistry,
        adapters: Mapping[str, TextGenerateAdapter],
    ) -> None:
        self._registry = registry
        self._adapters = dict(adapters)

    @property
    def registry(self) -> AICapabilityRegistry:
        """Return the capability registry used for selection."""
        return self._registry

    async def generate(self, request: TextGenerationRequest) -> str:
        """Select a text_generate binding and invoke its adapter."""
        binding = self._registry.select(
            AICapability.TEXT_GENERATE,
            provider=request.provider,
            model=request.model,
        )
        adapter = self._adapters.get(binding.provider)
        if adapter is None:
            raise RuntimeError(
                f"No text_generate adapter registered for provider {binding.provider!r}"
            )
        return await adapter.generate(request)


class LLMProviderTextGenerateAdapter:
    """Adapter for existing LLMProvider implementations."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    async def generate(self, request: TextGenerationRequest) -> str:
        return await self._provider.generate_text(
            request.prompt,
            system_prompt=request.system_prompt,
            model=request.model,
            max_tokens=request.max_tokens,
            caller=request.caller,
        )


ACPClientFactory = Callable[[], ACPClient]


class ACPTextGenerateAdapter:
    """One-shot text_generate adapter for ACP-backed CLIs."""

    def __init__(self, client_factory: ACPClientFactory) -> None:
        self._client_factory = client_factory

    async def generate(self, request: TextGenerationRequest) -> str:
        client = self._client_factory()
        await client.start(
            auto_session=True,
            cwd=request.cwd,
            model=request.model,
        )
        try:
            prompt = _compose_prompt(request)
            return await _collect_acp_text(client.send(prompt, model=request.model))
        finally:
            await client.stop()


class DroidCLITextGenerateAdapter:
    """One-shot text_generate adapter for Droid's noninteractive exec transport."""

    def __init__(
        self,
        *,
        command_path: str | None = None,
        timeout_seconds: float = 600.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._command_path = command_path
        self._timeout_seconds = timeout_seconds
        self._env = dict(env or {})

    def build_command(self, request: TextGenerationRequest) -> list[str]:
        """Build the Droid exec command for a request."""
        path = self._command_path or shutil.which("droid")
        if not path:
            raise FileNotFoundError("Droid CLI not found in PATH")

        command = [path, "exec", "--output-format", "text"]
        if request.model:
            command.extend(["--model", request.model])
        command.append(_compose_prompt(request))
        return command

    async def generate(self, request: TextGenerationRequest) -> str:
        command = self.build_command(request)
        env = os.environ.copy()
        env.update(self._env)
        env["GOBBY_HOOKS_DISABLED"] = "1"
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=request.cwd,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=2.0)
            raise RuntimeError(
                f"Droid exec timed out after {self._timeout_seconds:g}s: {shlex.join(command)}"
            ) from exc
        returncode = process.returncode
        if returncode:
            message = _decode(stderr).strip() or _decode(stdout).strip()
            raise RuntimeError(f"Droid exec failed with exit code {returncode}: {message}")
        return _decode(stdout).strip()


def build_daemon_text_generation_service(
    config: DaemonConfig,
    *,
    registry: AICapabilityRegistry | None = None,
) -> TextGenerationService:
    """Build the daemon text_generate service from configured capability bindings."""
    return TextGenerationService(
        registry or build_daemon_ai_capability_registry(config),
        _daemon_text_generation_adapters(config),
    )


def _daemon_text_generation_adapters(config: DaemonConfig) -> dict[str, TextGenerateAdapter]:
    from gobby.llm.claude import ClaudeLLMProvider
    from gobby.llm.codex import CodexProvider
    from gobby.llm.local import LocalLLMProvider

    adapters: dict[str, TextGenerateAdapter] = {
        "claude": LLMProviderTextGenerateAdapter(ClaudeLLMProvider(config)),
        "codex": LLMProviderTextGenerateAdapter(CodexProvider(config)),
        "gemini": ACPTextGenerateAdapter(GeminiACPClient),
        "grok": ACPTextGenerateAdapter(GrokACPClient),
        "qwen": ACPTextGenerateAdapter(QwenACPClient),
        "droid": DroidCLITextGenerateAdapter(),
    }
    if config.local:
        adapters["local"] = LLMProviderTextGenerateAdapter(LocalLLMProvider(config))
    return adapters


def _compose_prompt(request: TextGenerationRequest) -> str:
    if not request.system_prompt:
        return request.prompt
    return f"{request.system_prompt}\n\n{request.prompt}"


async def _collect_acp_text(events: AsyncIterator[StreamEvent]) -> str:
    chunks: list[str] = []
    result_chunks: list[str] = []
    async for event in events:
        text = _stream_event_text(event)
        if not text:
            continue
        if event.event_type == "content_delta":
            chunks.append(text)
        elif event.event_type == "result":
            result_chunks.append(text)

    return "".join(chunks or result_chunks).strip()


def _stream_event_text(event: StreamEvent) -> str:
    for key in ("content", "output", "text", "message"):
        value = event.data.get(key)
        if isinstance(value, str):
            return value
    return ""


def _decode(data: bytes | str) -> str:
    if isinstance(data, str):
        return data
    return data.decode(errors="replace")


__all__ = [
    "ACPTextGenerateAdapter",
    "DroidCLITextGenerateAdapter",
    "LLMProviderTextGenerateAdapter",
    "TextGenerateAdapter",
    "TextGenerationRequest",
    "TextGenerationService",
    "build_daemon_text_generation_service",
]

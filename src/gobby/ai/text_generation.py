"""Daemon-owned text generation execution adapters."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import shutil
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

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


TextGenerateAdapterFactory = Callable[[], TextGenerateAdapter]


class TextGenerationService:
    """Select and execute daemon text_generate capability bindings."""

    def __init__(
        self,
        registry: AICapabilityRegistry,
        adapters: Mapping[str, TextGenerateAdapter] | None = None,
        adapter_factories: Mapping[str, TextGenerateAdapterFactory] | None = None,
    ) -> None:
        self._registry = registry
        self._adapters = dict(adapters or {})
        self._adapter_factories = dict(adapter_factories or {})

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
            factory = self._adapter_factories.get(binding.provider)
            if factory is None:
                raise RuntimeError(
                    f"No text_generate adapter registered for provider {binding.provider!r}"
                )
            adapter = factory()
            self._adapters[binding.provider] = adapter
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


class CodexAppServerClientLike(Protocol):
    """Subset of Codex app-server client used by text_generate."""

    async def start(self) -> None:
        """Start the app-server process."""

    async def stop(self) -> None:
        """Stop the app-server process."""

    async def start_thread(
        self,
        cwd: str | None = None,
        model: str | None = None,
        approval_policy: str | None = None,
        sandbox: str | None = None,
        terminal_context: dict[str, Any] | None = None,
    ) -> Any:
        """Start a Codex app-server thread."""

    def run_turn(
        self,
        thread_id: str,
        prompt: str,
        images: list[str] | None = None,
        **config_overrides: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run one Codex app-server turn."""


CodexAppServerClientFactory = Callable[[], CodexAppServerClientLike]


class CodexAppServerTextGenerateAdapter:
    """One-shot text_generate adapter backed by Codex app-server."""

    def __init__(self, client_factory: CodexAppServerClientFactory | None = None) -> None:
        self._client_factory = client_factory or _codex_app_server_client

    async def generate(self, request: TextGenerationRequest) -> str:
        client = self._client_factory()
        await client.start()
        try:
            thread = await client.start_thread(cwd=request.cwd, model=request.model)
            chunks: list[str] = []
            fallback_chunks: list[str] = []
            async for event in client.run_turn(
                thread.id,
                request.prompt,
                context_prefix=request.system_prompt,
            ):
                event_type = event.get("type")
                text = _codex_event_text(event)
                if not text:
                    continue
                if event_type in {"agent/messageDelta", "item/agentMessage/delta"}:
                    chunks.append(text)
                elif event_type == "item/completed":
                    fallback_chunks.append(text)
            return "".join(chunks or fallback_chunks).strip()
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
        adapter_factories=_daemon_text_generation_adapter_factories(config),
    )


def _daemon_text_generation_adapter_factories(
    config: DaemonConfig,
) -> dict[str, TextGenerateAdapterFactory]:
    factories: dict[str, TextGenerateAdapterFactory] = {
        "claude": lambda: _claude_text_generate_adapter(config),
        "codex": CodexAppServerTextGenerateAdapter,
        "gemini": lambda: ACPTextGenerateAdapter(GeminiACPClient),
        "grok": lambda: ACPTextGenerateAdapter(GrokACPClient),
        "qwen": lambda: ACPTextGenerateAdapter(QwenACPClient),
        "droid": DroidCLITextGenerateAdapter,
    }
    if config.local:
        factories["local"] = lambda: _local_text_generate_adapter(config)
    return factories


def _claude_text_generate_adapter(config: DaemonConfig) -> TextGenerateAdapter:
    from gobby.llm.claude import ClaudeLLMProvider

    return LLMProviderTextGenerateAdapter(ClaudeLLMProvider(config))


def _local_text_generate_adapter(config: DaemonConfig) -> TextGenerateAdapter:
    from gobby.llm.local import LocalLLMProvider

    return LLMProviderTextGenerateAdapter(LocalLLMProvider(config))


def _codex_app_server_client() -> CodexAppServerClientLike:
    from gobby.adapters.codex_impl.client import CodexAppServerClient

    return CodexAppServerClient()


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


def _codex_event_text(event: dict[str, Any]) -> str:
    delta = event.get("delta")
    if isinstance(delta, str) and delta:
        return delta

    item = event.get("item")
    if not isinstance(item, dict):
        return ""

    content = item.get("content")
    if isinstance(content, str) and content:
        return content
    if not isinstance(content, list):
        return ""

    chunks: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text") or block.get("delta") or ""
            if isinstance(text, str) and text:
                chunks.append(text)
    return "".join(chunks)


def _decode(data: bytes | str) -> str:
    if isinstance(data, str):
        return data
    return data.decode(errors="replace")


__all__ = [
    "ACPTextGenerateAdapter",
    "CodexAppServerTextGenerateAdapter",
    "DroidCLITextGenerateAdapter",
    "LLMProviderTextGenerateAdapter",
    "TextGenerateAdapter",
    "TextGenerationRequest",
    "TextGenerationService",
    "build_daemon_text_generation_service",
]

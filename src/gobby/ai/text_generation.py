"""Daemon-owned text generation execution adapters."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import shutil
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol, cast

from gobby.ai.registry import (
    AICapability,
    AICapabilityRegistry,
    CapabilityBinding,
    build_daemon_ai_capability_registry,
)
from gobby.config.app import DaemonConfig
from gobby.config.feature_base import default_candidates_for_profile, normalize_feature_candidate

if TYPE_CHECKING:
    from gobby.llm.base import LLMTextResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class TextGenerationRequest:
    """One daemon text_generate request."""

    prompt: str
    provider: str | None = None
    profile: str | None = None
    candidates: tuple[str, ...] = ()
    system_prompt: str | None = None
    model: str | None = None
    max_tokens: int | None = None
    caller: str | None = None
    cwd: str | None = None


class TextGenerateAdapter(Protocol):
    """Adapter for one provider's text_generate execution path."""

    async def generate(self, request: TextGenerationRequest) -> str | LLMTextResult:
        """Generate text for the request."""


class TextGenerateJSONAdapter(Protocol):
    """Adapter with provider-native JSON generation support."""

    async def generate_json(self, request: TextGenerationRequest) -> dict[str, Any]:
        """Generate and parse JSON for the request."""


TextGenerateAdapterFactory = Callable[[], TextGenerateAdapter]


class ACPStreamEventLike(Protocol):
    """Subset of normalized ACP stream events used by text generation."""

    @property
    def event_type(self) -> str:
        """Return the normalized event type."""

    @property
    def data(self) -> Mapping[str, Any]:
        """Return the normalized event payload."""


class ACPClientLike(Protocol):
    """Subset of ACP clients used by text generation."""

    async def start(
        self,
        session_id: str | None = None,
        model: str | None = None,
        *,
        auto_session: bool = True,
        cwd: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        """Start the ACP client."""

    def send(
        self,
        message: str,
        *,
        session_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[ACPStreamEventLike]:
        """Send a prompt and stream normalized events."""

    async def stop(self) -> None:
        """Stop the ACP client."""


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
        return (await self.generate_result(request)).text

    async def generate_result(self, request: TextGenerationRequest) -> LLMTextResult:
        """Select a text_generate binding and invoke its adapter with usage."""
        candidates = self._candidate_requests(request)
        attempted_candidates: list[str] = []
        candidate_errors: dict[str, str] = {}
        text_result, last_error = await self._try_generate_result_candidates(
            candidates,
            attempted_candidates=attempted_candidates,
            candidate_errors=candidate_errors,
        )
        if text_result is not None:
            return text_result

        if len(candidates) == 1 and last_error is not None:
            raise last_error
        raise RuntimeError(
            "No text generation candidate succeeded "
            f"(tried: {attempted_candidates}; errors: {candidate_errors})"
        ) from last_error

    async def generate_json(self, request: TextGenerationRequest) -> dict[str, Any]:
        """Select a text_generate binding and return structured JSON."""
        candidates = self._candidate_requests(request)
        attempted_candidates: list[str] = []
        candidate_errors: dict[str, str] = {}
        result, last_error = await self._try_generate_json_candidates(
            candidates,
            attempted_candidates=attempted_candidates,
            candidate_errors=candidate_errors,
        )
        if result is not None:
            return result

        if len(candidates) == 1 and last_error is not None:
            raise last_error
        raise RuntimeError(
            "No JSON generation candidate succeeded; "
            f"attempted candidates: {attempted_candidates}; errors: {candidate_errors}"
        ) from last_error

    async def _try_generate_result_candidates(
        self,
        candidates: tuple[TextGenerationRequest, ...],
        *,
        attempted_candidates: list[str],
        candidate_errors: dict[str, str],
    ) -> tuple[LLMTextResult | None, Exception | None]:
        last_error: Exception | None = None
        for candidate in candidates:
            candidate_label = _candidate_debug_label(candidate)
            attempted_candidates.append(candidate_label)
            start = time.perf_counter()
            binding: CapabilityBinding | None = None
            try:
                binding = self._select_binding(candidate)
                adapter = self._adapter_for_provider(binding.provider)
                result = await adapter.generate(candidate)
                text_result = _coerce_text_result(result)
                self._log_generation_event(
                    request=candidate,
                    binding=binding,
                    latency_ms=_elapsed_ms(start),
                    success=True,
                )
                return (
                    replace(
                        text_result,
                        provider=binding.provider,
                        model=candidate.model or next(iter(binding.models), None),
                        profile=candidate.profile,
                    ),
                    None,
                )
            except Exception as exc:
                last_error = exc
                candidate_errors[candidate_label] = f"{type(exc).__name__}: {exc}"
                self._log_generation_event(
                    request=candidate,
                    binding=binding,
                    latency_ms=_elapsed_ms(start),
                    success=False,
                    error=exc,
                )
                continue
        return None, last_error

    async def _try_generate_json_candidates(
        self,
        candidates: tuple[TextGenerationRequest, ...],
        *,
        attempted_candidates: list[str],
        candidate_errors: dict[str, str],
    ) -> tuple[dict[str, Any] | None, Exception | None]:
        last_error: Exception | None = None
        for candidate in candidates:
            candidate_label = _candidate_debug_label(candidate)
            attempted_candidates.append(candidate_label)
            start = time.perf_counter()
            binding: CapabilityBinding | None = None
            parse_outcome = "not_attempted"
            try:
                binding = self._select_binding(candidate)
                adapter = self._adapter_for_provider(binding.provider)
                json_adapter = getattr(adapter, "generate_json", None)
                if callable(json_adapter):
                    typed_json_adapter = cast(
                        Callable[[TextGenerationRequest], Awaitable[dict[str, Any]]],
                        json_adapter,
                    )
                    result = await typed_json_adapter(candidate)
                    parse_outcome = "provider_structured"
                else:
                    text = await adapter.generate(_json_request(candidate))
                    raw = _coerce_text_result(text).text
                    result = _parse_json_text(raw)
                    parse_outcome = "parsed_text"
                self._log_generation_event(
                    request=candidate,
                    binding=binding,
                    latency_ms=_elapsed_ms(start),
                    success=True,
                    json_parse_outcome=parse_outcome,
                )
                return result, None
            except Exception as exc:
                last_error = exc
                candidate_errors[candidate_label] = f"{type(exc).__name__}: {exc}"
                if parse_outcome == "not_attempted" and isinstance(
                    exc, (ValueError, json.JSONDecodeError)
                ):
                    parse_outcome = "parse_failed"
                self._log_generation_event(
                    request=candidate,
                    binding=binding,
                    latency_ms=_elapsed_ms(start),
                    success=False,
                    error=exc,
                    json_parse_outcome=parse_outcome,
                )
        return None, last_error

    def _candidate_requests(
        self, request: TextGenerationRequest
    ) -> tuple[TextGenerationRequest, ...]:
        if request.candidates:
            return tuple(
                replace(request, provider=provider, model=model)
                for provider, model in (
                    _parse_candidate(normalize_feature_candidate(candidate))
                    for candidate in request.candidates
                )
            )
        has_provider = request.provider is not None
        has_model = request.model is not None
        if has_provider != has_model:
            raise ValueError(
                "provider and model must be supplied together for explicit text generation routing"
            )
        if has_provider and has_model:
            return (request,)
        if request.profile:
            candidates = default_candidates_for_profile(request.profile)
            return tuple(
                replace(request, provider=provider, model=model)
                for provider, model in (
                    _parse_candidate(normalize_feature_candidate(candidate))
                    for candidate in candidates
                )
            )
        return (request,)

    def _select_binding(self, request: TextGenerationRequest) -> CapabilityBinding:
        return self._registry.select(
            AICapability.TEXT_GENERATE,
            provider=request.provider,
            model=request.model,
        )

    def _adapter_for_provider(self, provider: str) -> TextGenerateAdapter:
        adapter = self._adapters.get(provider)
        if adapter is not None:
            return adapter

        factory = self._adapter_factories.get(provider)
        if factory is None:
            raise RuntimeError(f"No text_generate adapter registered for provider {provider!r}")
        try:
            adapter = factory()
        except Exception as exc:
            raise RuntimeError(
                f"Failed to initialize text_generate adapter for provider {provider!r}"
            ) from exc
        if adapter is None:
            raise RuntimeError(
                f"text_generate adapter factory for provider {provider!r} returned None"
            )
        self._adapters[provider] = adapter
        return adapter

    def _log_generation_event(
        self,
        *,
        request: TextGenerationRequest,
        binding: CapabilityBinding | None,
        latency_ms: float,
        success: bool,
        error: Exception | None = None,
        json_parse_outcome: str | None = None,
    ) -> None:
        provider = binding.provider if binding else request.provider
        model = request.model or (next(iter(binding.models), None) if binding else None)
        log_event = logger.debug if success else logger.info
        log_event(
            "feature_llm_call",
            extra={
                "feature": request.caller,
                "profile": request.profile,
                "provider": provider,
                "model": model,
                "latency_ms": round(latency_ms, 2),
                "success": success,
                "error": str(error) if error else None,
                "json_parse_outcome": json_parse_outcome,
            },
        )


class ClaudeTextGenerateAdapter:
    """Native text_generate adapter backed by Claude SDK primitives."""

    def __init__(self, config: DaemonConfig) -> None:
        from gobby.llm.claude import ClaudeLLMProvider

        self._provider = ClaudeLLMProvider(config)

    async def generate(self, request: TextGenerationRequest) -> LLMTextResult:
        return await self._provider.generate_text_result(
            request.prompt,
            system_prompt=request.system_prompt,
            model=request.model,
            max_tokens=request.max_tokens,
            caller=request.caller,
        )

    async def generate_json(self, request: TextGenerationRequest) -> dict[str, Any]:
        return await self._provider.generate_json(
            request.prompt,
            system_prompt=request.system_prompt,
            model=request.model,
            caller=request.caller,
        )


class LocalTextGenerateAdapter:
    """Native text_generate adapter backed by a local OpenAI-compatible endpoint."""

    def __init__(self, config: DaemonConfig) -> None:
        from gobby.llm.local import LocalLLMProvider

        self._provider = LocalLLMProvider(config)

    async def generate(self, request: TextGenerationRequest) -> LLMTextResult:
        return await self._provider.generate_text_result(
            request.prompt,
            system_prompt=request.system_prompt,
            model=request.model,
            max_tokens=request.max_tokens,
            caller=request.caller,
        )

    async def generate_json(self, request: TextGenerationRequest) -> dict[str, Any]:
        return await self._provider.generate_json(
            request.prompt,
            system_prompt=request.system_prompt,
            model=request.model,
            caller=request.caller,
        )


ACPClientFactory = Callable[[], ACPClientLike]


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
        "gemini": lambda: ACPTextGenerateAdapter(_gemini_acp_client),
        "grok": lambda: ACPTextGenerateAdapter(_grok_acp_client),
        "qwen": lambda: ACPTextGenerateAdapter(_qwen_acp_client),
        "droid": DroidCLITextGenerateAdapter,
    }
    if config.ai.generation.local.enabled:
        factories["local"] = lambda: _local_text_generate_adapter(config)
    return factories


def _claude_text_generate_adapter(config: DaemonConfig) -> TextGenerateAdapter:
    return ClaudeTextGenerateAdapter(config)


def _local_text_generate_adapter(config: DaemonConfig) -> TextGenerateAdapter:
    return LocalTextGenerateAdapter(config)


def _codex_app_server_client() -> CodexAppServerClientLike:
    from gobby.adapters.codex_impl.client import CodexAppServerClient

    return CodexAppServerClient()


def _gemini_acp_client() -> ACPClientLike:
    from gobby.adapters.gemini_acp_client import GeminiACPClient

    return GeminiACPClient()


def _grok_acp_client() -> ACPClientLike:
    from gobby.adapters.grok_acp_client import GrokACPClient

    return GrokACPClient()


def _qwen_acp_client() -> ACPClientLike:
    from gobby.adapters.qwen_acp_client import QwenACPClient

    return QwenACPClient()


def _llm_text_result_type() -> type[LLMTextResult]:
    from gobby.llm.base import LLMTextResult

    return LLMTextResult


def _coerce_text_result(result: str | LLMTextResult) -> LLMTextResult:
    text_result_type = _llm_text_result_type()
    if isinstance(result, text_result_type):
        return result
    return text_result_type(text=cast(str, result))


def _candidate_debug_label(candidate: TextGenerationRequest) -> str:
    provider = candidate.provider or "<auto>"
    model = candidate.model or "<auto>"
    return f"{provider}/{model}"


def _parse_candidate(candidate: str) -> tuple[str, str]:
    provider, separator, model = candidate.rpartition("/")
    if not separator or not provider.strip() or not model.strip():
        raise ValueError(f"Feature candidate must use provider/model format: {candidate!r}")
    return provider.strip(), model.strip()


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def _json_request(request: TextGenerationRequest) -> TextGenerationRequest:
    system_prompt = request.system_prompt
    json_instruction = "Respond with a single valid JSON object. Do not include markdown."
    if system_prompt:
        system_prompt = f"{system_prompt}\n\n{json_instruction}"
    else:
        system_prompt = json_instruction
    return replace(request, system_prompt=system_prompt)


def _parse_json_text(raw: str) -> dict[str, Any]:
    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise ValueError("Generated JSON must be an object")
    return parsed


def _compose_prompt(request: TextGenerationRequest) -> str:
    if not request.system_prompt:
        return request.prompt
    return f"{request.system_prompt}\n\n{request.prompt}"


async def _collect_acp_text(events: AsyncIterator[ACPStreamEventLike]) -> str:
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


def _stream_event_text(event: ACPStreamEventLike) -> str:
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
    "ClaudeTextGenerateAdapter",
    "CodexAppServerTextGenerateAdapter",
    "DroidCLITextGenerateAdapter",
    "LocalTextGenerateAdapter",
    "TextGenerateAdapter",
    "TextGenerationRequest",
    "TextGenerationService",
    "build_daemon_text_generation_service",
]

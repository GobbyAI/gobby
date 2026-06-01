"""Daemon-owned audio capability execution adapters."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from gobby.ai.registry import (
    AICapability,
    AICapabilityRegistry,
    build_daemon_ai_capability_registry,
    normalize_capability,
)
from gobby.config.app import DaemonConfig
from gobby.config.voice import OpenAICompatibleAudioBindingConfig


@dataclass(frozen=True, kw_only=True)
class AudioCapabilityRequest:
    """One daemon audio capability request."""

    audio_bytes: bytes
    mime_type: str = "audio/webm"
    filename: str | None = None
    capability: AICapability | str = AICapability.AUDIO_TRANSCRIBE
    provider: str | None = None
    model: str | None = None
    language: str | None = None
    prompt: str | None = None
    caller: str | None = None


@dataclass(frozen=True, kw_only=True)
class AudioSegment:
    """One audio transcript segment with second-based offsets."""

    text: str
    start: float | None = None
    end: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"text": self.text}
        if self.start is not None:
            data["start"] = self.start
        if self.end is not None:
            data["end"] = self.end
        return data


@dataclass(frozen=True, kw_only=True)
class AudioCapabilityOutput:
    """Adapter output with optional transcription metadata."""

    text: str
    segments: tuple[AudioSegment, ...] = ()
    language: str | None = None
    task: str | None = None


@dataclass(frozen=True, kw_only=True)
class AudioCapabilityResult:
    """Result from a selected daemon audio capability binding."""

    text: str
    capability: AICapability
    provider: str
    model: str | None = None
    segments: tuple[AudioSegment, ...] = ()
    language: str | None = None
    task: str | None = None


class AudioProviderUnavailableError(RuntimeError):
    """Raised when a selected audio adapter cannot execute locally."""


class AudioCapabilityAdapter(Protocol):
    """Adapter for one provider's audio capability execution path."""

    async def transcribe(self, request: AudioCapabilityRequest) -> str | AudioCapabilityOutput:
        """Transcribe audio bytes."""

    async def translate(self, request: AudioCapabilityRequest) -> str | AudioCapabilityOutput:
        """Translate audio bytes."""


class WhisperSTTProtocol(Protocol):
    """WhisperSTT surface used by the daemon audio adapter."""

    @property
    def is_available(self) -> bool:
        """Return whether the local Whisper runtime is importable."""

    async def transcribe(self, audio_bytes: bytes, mime_type: str = "audio/webm") -> str:
        """Transcribe audio bytes."""

    async def translate(self, audio_bytes: bytes, mime_type: str = "audio/webm") -> str:
        """Translate audio bytes."""


class AudioCapabilityService:
    """Select and execute daemon audio capability bindings."""

    def __init__(
        self,
        registry: AICapabilityRegistry,
        adapters: Mapping[str, AudioCapabilityAdapter],
    ) -> None:
        self._registry = registry
        self._adapters = {
            _provider_key(provider): adapter for provider, adapter in adapters.items()
        }

    @property
    def registry(self) -> AICapabilityRegistry:
        """Return the capability registry used for selection."""
        return self._registry

    async def transcribe(self, request: AudioCapabilityRequest) -> AudioCapabilityResult:
        """Select audio_transcribe and invoke its adapter."""
        return await self.execute(
            AudioCapabilityRequest(
                audio_bytes=request.audio_bytes,
                mime_type=request.mime_type,
                filename=request.filename,
                capability=AICapability.AUDIO_TRANSCRIBE,
                provider=request.provider,
                model=request.model,
                language=request.language,
                prompt=request.prompt,
                caller=request.caller,
            )
        )

    async def translate(self, request: AudioCapabilityRequest) -> AudioCapabilityResult:
        """Select audio_translate and invoke its adapter."""
        return await self.execute(
            AudioCapabilityRequest(
                audio_bytes=request.audio_bytes,
                mime_type=request.mime_type,
                filename=request.filename,
                capability=AICapability.AUDIO_TRANSLATE,
                provider=request.provider,
                model=request.model,
                language=request.language,
                prompt=request.prompt,
                caller=request.caller,
            )
        )

    async def execute(self, request: AudioCapabilityRequest) -> AudioCapabilityResult:
        """Select the requested audio capability and invoke its adapter."""
        capability = normalize_capability(request.capability)
        if capability not in (AICapability.AUDIO_TRANSCRIBE, AICapability.AUDIO_TRANSLATE):
            raise ValueError(f"{capability.value} is not an audio capability")

        binding = self._registry.select(
            capability,
            provider=request.provider,
            model=request.model,
        )
        adapter = self._adapters.get(_provider_key(binding.provider))
        if adapter is None:
            raise RuntimeError(
                f"No {capability.value} adapter registered for provider {binding.provider!r}"
            )

        if capability == AICapability.AUDIO_TRANSCRIBE:
            output = _coerce_audio_output(
                await adapter.transcribe(request),
                task=_audio_task(capability),
            )
        else:
            output = _coerce_audio_output(
                await adapter.translate(request),
                task=_audio_task(capability),
            )
        return AudioCapabilityResult(
            text=output.text,
            capability=capability,
            provider=binding.provider,
            model=request.model or next(iter(binding.models), None),
            segments=output.segments,
            language=output.language,
            task=output.task or _audio_task(capability),
        )


class WhisperAudioAdapter:
    """Audio capability adapter for local WhisperSTT."""

    def __init__(
        self,
        stt: WhisperSTTProtocol,
        *,
        timeout_seconds: float,
    ) -> None:
        self._stt = stt
        self._timeout_seconds = timeout_seconds

    async def transcribe(self, request: AudioCapabilityRequest) -> str | AudioCapabilityOutput:
        self._ensure_available()
        verbose = getattr(self._stt, "transcribe_verbose", None)
        if inspect.iscoroutinefunction(verbose):
            return await asyncio.wait_for(
                verbose(request.audio_bytes, request.mime_type),
                timeout=self._timeout_seconds,
            )
        text = await asyncio.wait_for(
            self._stt.transcribe(request.audio_bytes, request.mime_type),
            timeout=self._timeout_seconds,
        )
        return AudioCapabilityOutput(text=text, task="transcribe")

    async def translate(self, request: AudioCapabilityRequest) -> str | AudioCapabilityOutput:
        self._ensure_available()
        verbose = getattr(self._stt, "translate_verbose", None)
        if inspect.iscoroutinefunction(verbose):
            return await asyncio.wait_for(
                verbose(request.audio_bytes, request.mime_type),
                timeout=self._timeout_seconds,
            )
        text = await asyncio.wait_for(
            self._stt.translate(request.audio_bytes, request.mime_type),
            timeout=self._timeout_seconds,
        )
        return AudioCapabilityOutput(text=text, task="translate")

    def _ensure_available(self) -> None:
        if not self._stt.is_available:
            raise AudioProviderUnavailableError("faster-whisper not installed")


class OpenAICompatibleAudioAdapter:
    """Audio adapter for OpenAI-compatible HTTP endpoints."""

    def __init__(self, config: OpenAICompatibleAudioBindingConfig) -> None:
        self._config = config

    async def transcribe(self, request: AudioCapabilityRequest) -> AudioCapabilityOutput:
        return await self._post_audio(
            "transcriptions",
            request,
            include_language=True,
            task="transcribe",
        )

    async def translate(self, request: AudioCapabilityRequest) -> AudioCapabilityOutput:
        return await self._post_audio(
            "translations",
            request,
            include_language=False,
            task="translate",
        )

    async def _post_audio(
        self,
        endpoint: str,
        request: AudioCapabilityRequest,
        *,
        include_language: bool,
        task: str,
    ) -> AudioCapabilityOutput:
        data = {
            "model": request.model or self._config.model,
            "response_format": "verbose_json",
        }
        if request.prompt:
            data["prompt"] = request.prompt
        if include_language and request.language:
            data["language"] = request.language

        files = {
            "file": (
                request.filename or "audio.webm",
                request.audio_bytes,
                request.mime_type,
            )
        }
        headers = _auth_headers(self._config.api_key)
        async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
            response = await client.post(
                _audio_endpoint(self._config.url, endpoint),
                data=data,
                files=files,
                headers=headers,
            )
        response.raise_for_status()
        payload = response.json()
        return _extract_audio_output(payload, task=task)


def build_daemon_audio_service(
    config: DaemonConfig,
    *,
    registry: AICapabilityRegistry | None = None,
) -> AudioCapabilityService:
    """Build the daemon audio capability service from configured bindings."""
    return AudioCapabilityService(
        registry or build_daemon_ai_capability_registry(config),
        _daemon_audio_adapters(config),
    )


def _daemon_audio_adapters(config: DaemonConfig) -> dict[str, AudioCapabilityAdapter]:
    from gobby.voice.stt import WhisperSTT

    adapters: dict[str, AudioCapabilityAdapter] = {
        "whisper": WhisperAudioAdapter(
            WhisperSTT(config.voice),
            timeout_seconds=config.voice.transcription_timeout_seconds,
        ),
    }
    for binding_config in config.voice.openai_compatible_audio:
        adapters[_provider_key(binding_config.provider)] = OpenAICompatibleAudioAdapter(
            binding_config
        )
    return adapters


def _provider_key(provider: str) -> str:
    return provider.strip().lower()


def _audio_endpoint(base_url: str, endpoint: str) -> str:
    return f"{base_url.rstrip('/')}/audio/{endpoint}"


def _auth_headers(api_key: str | None) -> dict[str, str]:
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def _coerce_audio_output(value: str | AudioCapabilityOutput, *, task: str) -> AudioCapabilityOutput:
    if isinstance(value, AudioCapabilityOutput):
        return (
            value
            if value.task is not None
            else AudioCapabilityOutput(
                text=value.text,
                segments=value.segments,
                language=value.language,
                task=task,
            )
        )
    return AudioCapabilityOutput(text=value, task=task)


def _audio_task(capability: AICapability) -> str:
    if capability == AICapability.AUDIO_TRANSLATE:
        return "translate"
    return "transcribe"


def _extract_text(payload: Any) -> str:
    return _extract_audio_output(payload, task="transcribe").text


def _extract_audio_output(payload: Any, *, task: str) -> AudioCapabilityOutput:
    if not isinstance(payload, dict):
        raise RuntimeError("OpenAI-compatible audio response was not a JSON object")
    text = payload.get("text")
    if not isinstance(text, str):
        raise RuntimeError("OpenAI-compatible audio response did not include text")
    return AudioCapabilityOutput(
        text=text,
        segments=_extract_segments(payload.get("segments")),
        language=_optional_str(payload.get("language")),
        task=_optional_str(payload.get("task")) or task,
    )


def _extract_segments(value: Any) -> tuple[AudioSegment, ...]:
    if not isinstance(value, list):
        return ()
    segments: list[AudioSegment] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        segments.append(
            AudioSegment(
                text=text,
                start=_optional_float(item.get("start")),
                end=_optional_float(item.get("end")),
            )
        )
    return tuple(segments)


def _optional_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


__all__ = [
    "AudioCapabilityAdapter",
    "AudioCapabilityOutput",
    "AudioCapabilityRequest",
    "AudioCapabilityResult",
    "AudioSegment",
    "AudioCapabilityService",
    "AudioProviderUnavailableError",
    "OpenAICompatibleAudioAdapter",
    "WhisperAudioAdapter",
    "build_daemon_audio_service",
]

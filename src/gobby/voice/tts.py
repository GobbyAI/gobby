"""Text-to-speech abstractions plus the Kokoro provider.

Lazy-loads model implementations on first synthesis to avoid slowing daemon
boot. Providers expose a common lifecycle and status surface so the voice
stack does not need provider-specific branching.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

import numpy as np

if TYPE_CHECKING:
    from gobby.config.voice import VoiceConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TTSProviderCapabilities:
    """Stable provider capability flags exposed through status responses."""

    supports_reference_audio: bool = False
    supports_reference_text: bool = False
    supports_streaming: bool = False
    supports_voice_cloning: bool = False


@dataclass(frozen=True, slots=True)
class TTSProviderStatus:
    """Availability and metadata for a provider instance."""

    provider: str
    available: bool
    reason: str = ""
    backend_kind: Literal["embedded", "external"] = "embedded"
    capabilities: TTSProviderCapabilities = field(default_factory=TTSProviderCapabilities)
    details: dict[str, Any] = field(default_factory=dict)

    def as_status_fields(self) -> dict[str, Any]:
        """Convert provider status to the public websocket/HTTP status shape."""
        fields: dict[str, Any] = {
            "tts_provider": self.provider,
            "tts_available": self.available,
            "tts_reason": self.reason,
            "tts_backend_kind": self.backend_kind,
            "tts_capabilities": asdict(self.capabilities),
        }
        fields.update(self.details)
        return fields


@runtime_checkable
class TTSProvider(Protocol):
    """Protocol for pluggable TTS engines."""

    provider_name: str
    backend_kind: Literal["embedded", "external"]
    capabilities: TTSProviderCapabilities

    async def warmup(self) -> None:
        """Preload model state needed for synthesis."""
        ...  # pragma: no cover

    def unload(self) -> None:
        """Release provider state and reclaim memory."""
        ...  # pragma: no cover

    def synthesize_stream(self, text: str) -> AsyncIterator[tuple[bytes, int]]:
        """Yield ``(pcm_int16_bytes, sample_rate)`` chunks as they are generated."""
        ...  # pragma: no cover

    @property
    def is_available(self) -> bool:
        """Check if the provider is installed and ready to initialize."""
        ...  # pragma: no cover

    @property
    def sample_rate(self) -> int:
        """Output sample rate in Hz."""
        ...  # pragma: no cover

    def get_status(self) -> TTSProviderStatus:
        """Return availability, capabilities, and provider-specific details."""
        ...  # pragma: no cover


class BaseTTSProvider(ABC):
    """Shared provider lifecycle and status helpers."""

    provider_name = "unknown"
    backend_kind: Literal["embedded", "external"] = "embedded"
    capabilities = TTSProviderCapabilities()

    def __init__(self, config: VoiceConfig) -> None:
        self._config = config

    @property
    def is_available(self) -> bool:
        available, _reason = self._availability()
        return available

    def get_status(self) -> TTSProviderStatus:
        available, reason = self._availability()
        return TTSProviderStatus(
            provider=self.provider_name,
            available=available,
            reason=reason,
            backend_kind=self.backend_kind,
            capabilities=self.capabilities,
            details=self._status_details(),
        )

    def _status_details(self) -> dict[str, Any]:
        return {}

    @abstractmethod
    async def warmup(self) -> None:
        """Preload model state needed for synthesis."""
        raise NotImplementedError

    @abstractmethod
    def unload(self) -> None:
        """Release provider state and reclaim memory."""
        raise NotImplementedError

    @abstractmethod
    def synthesize_stream(self, text: str) -> AsyncIterator[tuple[bytes, int]]:
        """Yield ``(pcm_int16_bytes, sample_rate)`` chunks as they are generated."""
        raise NotImplementedError

    @property
    @abstractmethod
    def sample_rate(self) -> int:
        """Output sample rate in Hz."""
        raise NotImplementedError

    @abstractmethod
    def _availability(self) -> tuple[bool, str]:
        """Return ``(available, reason)`` for the provider."""
        raise NotImplementedError


class KokoroTTS(BaseTTSProvider):
    """Local TTS via kokoro-onnx. Lazy-loads model on first use."""

    provider_name = "kokoro"
    capabilities = TTSProviderCapabilities(
        supports_reference_audio=False,
        supports_reference_text=False,
        supports_streaming=True,
        supports_voice_cloning=False,
    )

    def __init__(self, config: VoiceConfig) -> None:
        super().__init__(config)
        self._model: Any | None = None
        # Initialize the lock eagerly so two coroutines arriving in
        # _ensure_model concurrently cannot create separate locks.
        self._load_lock: asyncio.Lock = asyncio.Lock()
        self._sample_rate = 24000  # Kokoro outputs 24kHz

    async def warmup(self) -> None:
        """Public entry point for preloading the TTS model."""
        await self._ensure_model()

    def unload(self) -> None:
        """Release the model to reclaim memory."""
        self._model = None

    def _status_details(self) -> dict[str, Any]:
        return {
            "tts_voice": self._config.tts_voice,
        }

    def _availability(self) -> tuple[bool, str]:
        """Check if kokoro-onnx is installed and model files exist."""
        try:
            import kokoro_onnx  # noqa: F401
        except ImportError:
            return False, "kokoro-onnx not installed (uv sync --extra voice)"

        model_path = Path(self._config.tts_model_path).expanduser()
        voices_path = Path(self._config.tts_voices_path).expanduser()
        if model_path.exists() and voices_path.exists():
            return True, ""
        return False, "Kokoro model files not found"

    async def _ensure_model(self) -> Any:
        """Lazy-load the Kokoro model (thread-safe, async)."""
        if self._model is not None:
            return self._model

        async with self._load_lock:
            if self._model is not None:
                return self._model

            logger.info(
                "Loading Kokoro TTS model (voice=%s, lang=%s)",
                self._config.tts_voice,
                self._config.tts_language,
            )

            def _load() -> Any:
                from kokoro_onnx import Kokoro

                model_path = str(Path(self._config.tts_model_path).expanduser())
                voices_path = str(Path(self._config.tts_voices_path).expanduser())
                return Kokoro(model_path, voices_path)

            self._model = await asyncio.to_thread(_load)
            logger.info("Kokoro TTS model loaded successfully")
            return self._model

    async def synthesize_stream(self, text: str) -> AsyncIterator[tuple[bytes, int]]:
        """Yield ``(pcm_int16_bytes, sample_rate)`` chunks for the given text."""
        try:
            model = await self._ensure_model()
        except Exception:
            logger.error("Failed to load Kokoro TTS model", exc_info=True)
            return

        try:
            stream = model.create_stream(
                text,
                voice=self._config.tts_voice,
                speed=self._config.tts_speed,
                lang=self._config.tts_language,
            )

            async for samples, sr in stream:
                try:
                    pcm_int16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
                    yield pcm_int16.tobytes(), sr
                except Exception:
                    logger.error("Failed to encode TTS audio chunk", exc_info=True)
                    continue

        except asyncio.CancelledError:
            logger.debug("TTS synthesis cancelled")
            raise
        except Exception:
            logger.error("TTS synthesis failed", exc_info=True)

    @property
    def sample_rate(self) -> int:
        """Output sample rate in Hz (24kHz)."""
        return self._sample_rate

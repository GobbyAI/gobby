"""Text-to-speech service using Kokoro ONNX (local inference).

Lazy-loads the model on first synthesis to avoid slowing daemon boot.
All inference runs async — kokoro-onnx provides native async streaming.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import numpy as np

if TYPE_CHECKING:
    from gobby.config.voice import VoiceConfig

logger = logging.getLogger(__name__)


@runtime_checkable
class TTSProvider(Protocol):
    """Protocol for TTS engines — extensible for future providers."""

    async def synthesize_stream(self, text: str) -> AsyncIterator[tuple[bytes, int]]:
        """Yield (pcm_int16_bytes, sample_rate) chunks as they're generated."""
        ...  # pragma: no cover

    @property
    def is_available(self) -> bool:
        """Check if the TTS engine is installed and ready."""
        ...  # pragma: no cover

    @property
    def sample_rate(self) -> int:
        """Output sample rate in Hz."""
        ...  # pragma: no cover


class KokoroTTS:
    """Local TTS via kokoro-onnx. Lazy-loads model on first use.

    Follows the same pattern as WhisperSTT: lazy loading, async, thread-safe.
    """

    def __init__(self, config: VoiceConfig) -> None:
        self._config = config
        self._model: Any | None = None
        # Initialize the lock eagerly so two coroutines arriving in
        # _ensure_model concurrently cannot create separate locks.
        self._load_lock: asyncio.Lock = asyncio.Lock()
        self._sample_rate = 24000  # Kokoro outputs 24kHz

    async def warmup(self) -> None:
        """Public entry point for preloading the TTS model."""
        await self._ensure_model()

    def unload(self) -> None:
        """Release the model to reclaim memory.

        Safe to call from sync contexts: ``synthesize_stream`` captures the
        model in a local variable, so clearing ``self._model`` cannot affect
        an in-flight synthesis. Python attribute assignment is GIL-atomic.
        """
        self._model = None

    async def _ensure_model(self) -> Any:
        """Lazy-load the Kokoro model (thread-safe, async)."""
        if self._model is not None:
            return self._model

        async with self._load_lock:
            # Double-check after acquiring lock
            if self._model is not None:
                return self._model

            logger.info(
                f"Loading Kokoro TTS model (voice={self._config.tts_voice}, "
                f"lang={self._config.tts_language})"
            )

            def _load() -> Any:
                from pathlib import Path

                from kokoro_onnx import Kokoro

                model_path = str(Path(self._config.tts_model_path).expanduser())
                voices_path = str(Path(self._config.tts_voices_path).expanduser())
                return Kokoro(model_path, voices_path)

            self._model = await asyncio.to_thread(_load)
            logger.info("Kokoro TTS model loaded successfully")
            return self._model

    async def synthesize_stream(self, text: str) -> AsyncIterator[tuple[bytes, int]]:
        """Yield (pcm_int16_bytes, sample_rate) chunks for the given text.

        Each chunk is a complete sentence's worth of audio, encoded as
        16-bit signed integer PCM at 24kHz mono.

        Raises no exceptions to callers — errors are logged and the
        iterator simply ends.
        """
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
                    # Convert float32 samples to int16 PCM bytes
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
    def is_available(self) -> bool:
        """Check if kokoro-onnx is installed and model files exist."""
        try:
            import kokoro_onnx  # noqa: F401
        except ImportError:
            return False

        from pathlib import Path

        model_path = Path(self._config.tts_model_path).expanduser()
        voices_path = Path(self._config.tts_voices_path).expanduser()
        return model_path.exists() and voices_path.exists()

    @property
    def sample_rate(self) -> int:
        """Output sample rate in Hz (24kHz)."""
        return self._sample_rate

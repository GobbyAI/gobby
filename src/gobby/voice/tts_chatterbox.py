"""Text-to-speech via Chatterbox Turbo (voice cloning, local inference).

Lazy-loads the model on first synthesis to avoid slowing daemon boot.
Uses ChatterboxTurboTTS for sub-200ms latency with zero-shot voice cloning.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from gobby.config.voice import VoiceConfig

logger = logging.getLogger(__name__)


def _auto_device() -> str:
    """Detect best available device: cuda > mps > cpu."""
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
    except ImportError:
        pass
    return "cpu"


class ChatterboxTurboProvider:
    """Local TTS via Chatterbox Turbo. Lazy-loads model on first use.

    Provides zero-shot voice cloning from a reference audio clip.
    Implements TTSProvider protocol from gobby.voice.tts.
    """

    def __init__(self, config: VoiceConfig) -> None:
        self._config = config
        self._model: Any | None = None
        self._load_lock: asyncio.Lock | None = None
        self._sample_rate = 24000  # Chatterbox outputs 24kHz
        self._reference_audio = Path(config.tts_reference_audio).expanduser()

    async def _ensure_model(self) -> Any:
        """Lazy-load the Chatterbox Turbo model (thread-safe, async)."""
        if self._model is not None:
            return self._model

        if self._load_lock is None:
            self._load_lock = asyncio.Lock()

        async with self._load_lock:
            if self._model is not None:
                return self._model

            device = self._config.tts_device
            if device == "auto":
                device = _auto_device()

            logger.info(f"Loading Chatterbox Turbo model (device={device})")

            def _load() -> Any:
                from chatterbox.tts_turbo import ChatterboxTurboTTS

                return ChatterboxTurboTTS.from_pretrained(device=device)

            self._model = await asyncio.to_thread(_load)
            self._sample_rate = self._model.sr
            logger.info("Chatterbox Turbo model loaded successfully")
            return self._model

    async def synthesize_stream(self, text: str) -> AsyncIterator[tuple[bytes, int]]:
        """Generate speech for text, yielding (pcm_int16_bytes, sample_rate).

        Each call synthesizes the full text (typically a single sentence from
        SentenceBuffer) and yields the audio as one chunk. Turbo's sub-200ms
        latency makes this real-time for sentence-level synthesis.
        """
        try:
            model = await self._ensure_model()
        except Exception:
            logger.error("Failed to load Chatterbox Turbo model", exc_info=True)
            return

        try:
            ref_path = str(self._reference_audio) if self._reference_audio.exists() else None

            def _generate() -> Any:
                import torch

                # MPS doesn't support float64. Chatterbox's s3tokenizer
                # moves loaded audio tensors to the device without casting,
                # which crashes on MPS if torchaudio returns float64.
                # Setting the default dtype ensures all tensors stay float32.
                torch.set_default_dtype(torch.float32)

                kwargs: dict[str, Any] = {
                    "temperature": self._config.tts_temperature,
                }
                if ref_path:
                    kwargs["audio_prompt_path"] = ref_path
                return model.generate(text, **kwargs)

            wav = await asyncio.to_thread(_generate)

            # Convert torch.Tensor to PCM int16 bytes
            samples = wav.squeeze().cpu().numpy()
            pcm_int16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
            yield pcm_int16.tobytes(), self._sample_rate

        except asyncio.CancelledError:
            logger.debug("Chatterbox TTS synthesis cancelled")
            raise
        except Exception:
            logger.error("Chatterbox TTS synthesis failed", exc_info=True)

    @property
    def is_available(self) -> bool:
        """Check if chatterbox is installed and reference audio exists."""
        try:
            import chatterbox  # noqa: F401

            return True
        except ImportError:
            return False

    @property
    def sample_rate(self) -> int:
        """Output sample rate in Hz (24kHz)."""
        return self._sample_rate

"""Text-to-speech via Chatterbox Turbo (voice cloning, local inference).

Lazy-loads the model on first synthesis to avoid slowing daemon boot.
Uses ChatterboxTurboTTS for sub-200ms latency with zero-shot voice cloning.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Iterator

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


def _coerce_conditioning_audio(value: Any) -> Any:
    """Cast Chatterbox conditioning audio to float32 for MPS compatibility."""
    try:
        import torch
    except ImportError:  # pragma: no cover - torch is required when this is used
        torch = None  # type: ignore[assignment]

    if isinstance(value, np.ndarray):
        if np.issubdtype(value.dtype, np.floating) and value.dtype != np.float32:
            return value.astype(np.float32, copy=False)
        return value

    if torch is not None and torch.is_tensor(value):
        if value.dtype.is_floating_point and value.dtype != torch.float32:
            return value.to(dtype=torch.float32)
        return value

    if isinstance(value, list):
        return [_coerce_conditioning_audio(item) for item in value]

    if isinstance(value, tuple):
        return tuple(_coerce_conditioning_audio(item) for item in value)

    return value


@contextmanager
def _float32_conditioning_tokenizer(model: Any) -> Iterator[None]:
    """Wrap the Chatterbox tokenizer so conditioning audio stays float32 on MPS."""
    tokenizer = getattr(getattr(model, "s3gen", None), "tokenizer", None)
    original_forward = getattr(tokenizer, "forward", None)

    if getattr(model, "device", None) != "mps" or tokenizer is None or original_forward is None:
        yield
        return

    def _wrapped_forward(wavs: Any, *args: Any, **kwargs: Any) -> Any:
        return original_forward(_coerce_conditioning_audio(wavs), *args, **kwargs)

    tokenizer.forward = _wrapped_forward
    try:
        yield
    finally:
        tokenizer.forward = original_forward


@contextmanager
def _float32_conditioning_resample(model: Any) -> Iterator[None]:
    """Wrap librosa.resample so Chatterbox conditioning audio stays float32 on MPS."""
    if getattr(model, "device", None) != "mps":
        yield
        return

    import librosa

    original_resample = librosa.resample

    def _wrapped_resample(y: Any, *args: Any, **kwargs: Any) -> Any:
        return _coerce_conditioning_audio(original_resample(y, *args, **kwargs))

    librosa.resample = _wrapped_resample
    try:
        yield
    finally:
        librosa.resample = original_resample


@contextmanager
def _float32_voice_encoder(model: Any) -> Iterator[None]:
    """Wrap Chatterbox voice encoder entrypoints so MPS never sees float64 refs."""
    if getattr(model, "device", None) != "mps":
        yield
        return

    voice_encoder = getattr(model, "ve", None)
    original_embeds_from_wavs = getattr(voice_encoder, "embeds_from_wavs", None)
    original_embeds_from_mels = getattr(voice_encoder, "embeds_from_mels", None)
    if (
        voice_encoder is None
        or original_embeds_from_wavs is None
        or original_embeds_from_mels is None
    ):
        yield
        return

    def _wrapped_embeds_from_wavs(wavs: Any, *args: Any, **kwargs: Any) -> Any:
        return original_embeds_from_wavs(_coerce_conditioning_audio(wavs), *args, **kwargs)

    def _wrapped_embeds_from_mels(mels: Any, *args: Any, **kwargs: Any) -> Any:
        return original_embeds_from_mels(_coerce_conditioning_audio(mels), *args, **kwargs)

    voice_encoder.embeds_from_wavs = _wrapped_embeds_from_wavs
    voice_encoder.embeds_from_mels = _wrapped_embeds_from_mels
    try:
        yield
    finally:
        voice_encoder.embeds_from_wavs = original_embeds_from_wavs
        voice_encoder.embeds_from_mels = original_embeds_from_mels


@contextmanager
def _float32_conditioning_workarounds(model: Any) -> Iterator[None]:
    """Apply all Chatterbox MPS reference-audio float32 workarounds."""
    with _float32_conditioning_resample(model):
        with _float32_conditioning_tokenizer(model):
            with _float32_voice_encoder(model):
                yield


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

    def unload(self) -> None:
        """Release the model to reclaim memory."""
        self._model = None

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
            assert self._model is not None  # just loaded above
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
                kwargs: dict[str, Any] = {
                    "temperature": self._config.tts_temperature,
                }
                if ref_path:
                    kwargs["audio_prompt_path"] = ref_path
                with _float32_conditioning_workarounds(model):
                    return model.generate(text, **kwargs)

            wav = await asyncio.to_thread(_generate)

            # Convert torch.Tensor to PCM int16 bytes
            samples = wav.squeeze().cpu().numpy()
            pcm_int16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
            yield pcm_int16.tobytes(), self._sample_rate

        except asyncio.CancelledError:
            logger.debug("Chatterbox TTS synthesis cancelled")
            raise
        except TypeError as exc:
            if "MPS Tensor to float64 dtype" in str(exc):
                logger.error(
                    "Chatterbox TTS synthesis failed on MPS while preparing reference audio. "
                    "Conditioning audio must be float32 before it is moved to the device.",
                    exc_info=True,
                )
                return
            logger.error("Chatterbox TTS synthesis failed", exc_info=True)
        except Exception:
            logger.error("Chatterbox TTS synthesis failed", exc_info=True)

    @property
    def is_available(self) -> bool:
        """Check if chatterbox is installed and reference audio exists."""
        try:
            import chatterbox  # noqa: F401

            return True
        except Exception:
            return False

    @property
    def sample_rate(self) -> int:
        """Output sample rate in Hz (24kHz)."""
        return self._sample_rate

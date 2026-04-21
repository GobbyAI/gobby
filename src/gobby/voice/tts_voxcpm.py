"""Text-to-speech via VoxCPM (voice cloning, local inference)."""

from __future__ import annotations

import asyncio
import logging
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from gobby.voice.tts import BaseTTSProvider, TTSProviderCapabilities, _module_is_available

if TYPE_CHECKING:
    from gobby.config.voice import VoiceConfig

logger = logging.getLogger(__name__)


def _maybe_local_model_path(model_ref: str) -> Path | None:
    """Return a local path when the model ref looks path-like."""
    expanded = Path(model_ref).expanduser()
    if model_ref.startswith(("~", ".", "/")):
        return expanded
    if expanded.exists():
        return expanded
    return None


def _should_warn_runtime_device_fallback(requested_device: str, runtime_device: str | None) -> bool:
    """Warn only when VoxCPM falls back to CPU despite a stronger explicit request."""
    return (
        runtime_device == "cpu"
        and requested_device not in {"auto", "cpu"}
        and requested_device != runtime_device
    )


def _reference_text_candidates(reference_audio: Path) -> tuple[Path, ...]:
    """Return supported sidecar transcript locations for a reference clip."""
    return (reference_audio.with_suffix(".txt"), Path(f"{reference_audio}.txt"))


def _load_reference_text(
    configured_text: str | None, reference_audio: Path
) -> tuple[str | None, str | None]:
    """Resolve optional reference text from config first, then a sidecar transcript."""
    reference_text = (configured_text or "").strip()
    if reference_text:
        return reference_text, "config"

    for candidate in _reference_text_candidates(reference_audio):
        try:
            text = candidate.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        if text:
            return text, "sidecar"
    return None, None


def _inspect_reference_audio(reference_audio: Path) -> dict[str, Any]:
    """Return lightweight reference-clip metadata and quality warnings."""
    details: dict[str, Any] = {
        "tts_reference_audio_channels": None,
        "tts_reference_audio_duration_seconds": None,
        "tts_reference_audio_warnings": [],
    }
    if not reference_audio.exists():
        return details

    try:
        with wave.open(str(reference_audio), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_rate = wav_file.getframerate()
            frames = wav_file.getnframes()
    except (wave.Error, EOFError, OSError):
        return details

    duration_seconds = 0.0
    if sample_rate > 0:
        duration_seconds = frames / sample_rate

    warnings: list[str] = []
    if channels > 1:
        warnings.append("Reference clip is stereo; VoxCPM downmixes to mono internally.")
    if duration_seconds > 20:
        warnings.append(
            "Reference clip is longer than 20s; VoxCPM usually clones better from 8-15s."
        )

    details["tts_reference_audio_channels"] = channels
    details["tts_reference_audio_duration_seconds"] = round(duration_seconds, 2)
    details["tts_reference_audio_warnings"] = warnings
    return details


class VoxCPMProvider(BaseTTSProvider):
    """Local TTS via VoxCPM with optional higher-fidelity reference text."""

    provider_name = "voxcpm"
    capabilities = TTSProviderCapabilities(
        supports_reference_audio=True,
        supports_reference_text=True,
        supports_streaming=False,
        supports_voice_cloning=True,
    )

    def __init__(self, config: VoiceConfig) -> None:
        super().__init__(config)
        self._model: Any | None = None
        self._load_lock: asyncio.Lock = asyncio.Lock()
        self._sample_rate = 48000
        self._reference_audio = Path(config.tts_reference_audio).expanduser()

    def _availability(self) -> tuple[bool, str]:
        if not _module_is_available("voxcpm"):
            return (
                False,
                "voxcpm not installed in this Python runtime "
                "(embedded VoxCPM may need a separate Python <3.13 env)",
            )

        local_model_path = _maybe_local_model_path(self._config.tts_voxcpm_model)
        if local_model_path is not None and not local_model_path.exists():
            return False, f"Configured VoxCPM model path not found: {local_model_path}"
        return True, ""

    def _status_details(self) -> dict[str, Any]:
        reference_text, reference_text_source = _load_reference_text(
            self._config.tts_reference_text, self._reference_audio
        )
        runtime_device = getattr(getattr(self._model, "tts_model", None), "device", None)
        details = {
            "tts_reference_audio": str(self._reference_audio),
            "tts_reference_audio_exists": self._reference_audio.exists(),
            "tts_reference_text_configured": bool(reference_text),
            "tts_reference_text_source": reference_text_source,
            "tts_device": self._config.tts_device,
            "tts_runtime_device": runtime_device,
            "tts_voxcpm_model": self._config.tts_voxcpm_model,
        }
        details.update(_inspect_reference_audio(self._reference_audio))
        return details

    async def warmup(self) -> None:
        """Public entry point for preloading the VoxCPM model."""
        await self._ensure_model()

    def unload(self) -> None:
        """Release the model to reclaim memory."""
        self._model = None

    async def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model

        async with self._load_lock:
            if self._model is not None:
                return self._model

            logger.info(
                "Loading VoxCPM model (model=%s, device=%s)",
                self._config.tts_voxcpm_model,
                self._config.tts_device,
            )

            def _load() -> Any:
                from voxcpm import VoxCPM

                kwargs: dict[str, Any] = {
                    "hf_model_id": self._config.tts_voxcpm_model,
                    "load_denoiser": self._config.tts_voxcpm_load_denoiser,
                    "local_files_only": self._config.tts_voxcpm_local_files_only,
                    "optimize": self._config.tts_voxcpm_optimize,
                }
                return VoxCPM.from_pretrained(**kwargs)

            self._model = await asyncio.to_thread(_load)
            sample_rate = getattr(getattr(self._model, "tts_model", None), "sample_rate", None)
            runtime_device = getattr(getattr(self._model, "tts_model", None), "device", None)
            if isinstance(sample_rate, int):
                self._sample_rate = sample_rate
            requested_device = self._config.tts_device
            if _should_warn_runtime_device_fallback(requested_device, runtime_device):
                logger.warning(
                    "Embedded VoxCPM fell back from requested tts_device=%s to %s",
                    requested_device,
                    runtime_device,
                )
            logger.info(
                "VoxCPM model loaded successfully (runtime_device=%s)",
                runtime_device or "unknown",
            )
            return self._model

    async def synthesize_stream(self, text: str) -> AsyncIterator[tuple[bytes, int]]:
        """Generate speech for text, yielding a single PCM chunk per sentence."""
        model = await self._ensure_model()

        try:
            ref_path = str(self._reference_audio) if self._reference_audio.exists() else None
            reference_text, _reference_text_source = _load_reference_text(
                self._config.tts_reference_text, self._reference_audio
            )

            def _generate() -> Any:
                kwargs: dict[str, Any] = {
                    "text": text,
                    "cfg_value": self._config.tts_voxcpm_cfg_value,
                    "inference_timesteps": self._config.tts_voxcpm_inference_timesteps,
                    "denoise": self._config.tts_voxcpm_denoise,
                }
                if ref_path:
                    kwargs["reference_wav_path"] = ref_path
                if ref_path and reference_text:
                    kwargs["prompt_wav_path"] = ref_path
                    kwargs["prompt_text"] = reference_text
                return model.generate(**kwargs)

            wav = await asyncio.to_thread(_generate)
            samples = np.asarray(wav, dtype=np.float32).squeeze()
            pcm_int16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
            yield pcm_int16.tobytes(), self._sample_rate

        except asyncio.CancelledError:
            logger.debug("VoxCPM TTS synthesis cancelled")
            raise
        except Exception:
            logger.error("VoxCPM TTS synthesis failed", exc_info=True)
            raise

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

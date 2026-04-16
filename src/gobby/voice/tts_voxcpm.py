"""Text-to-speech via VoxCPM (voice cloning, local inference)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from gobby.voice.tts import BaseTTSProvider, TTSProviderCapabilities

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
        try:
            import voxcpm  # noqa: F401
        except Exception:
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
        reference_text = (self._config.tts_reference_text or "").strip()
        runtime_device = getattr(getattr(self._model, "tts_model", None), "device", None)
        return {
            "tts_reference_audio": str(self._reference_audio),
            "tts_reference_audio_exists": self._reference_audio.exists(),
            "tts_reference_text_configured": bool(reference_text),
            "tts_device": self._config.tts_device,
            "tts_runtime_device": runtime_device,
            "tts_voxcpm_model": self._config.tts_voxcpm_model,
        }

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
            if (
                isinstance(runtime_device, str)
                and requested_device != "auto"
                and requested_device != runtime_device
            ):
                logger.warning(
                    "Embedded VoxCPM ignored requested tts_device=%s and loaded on %s",
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
            reference_text = (self._config.tts_reference_text or "").strip() or None

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

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

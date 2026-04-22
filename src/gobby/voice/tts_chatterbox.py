"""Text-to-speech via Chatterbox Turbo (voice cloning, local inference).

Lazy-loads the model on first synthesis to avoid slowing daemon boot.
Uses ChatterboxTurboTTS for sub-200ms latency with zero-shot voice cloning.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import numpy as np

from gobby.config.voice import VoiceConfig
from gobby.voice.tts import BaseTTSProvider, TTSProviderCapabilities, _module_is_available

logger = logging.getLogger(__name__)

_WARMUP_PRIME_TEXT = "warm up"
_WARMUP_PRIME_MAX_GENERATION_TOKENS = 8


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
    torch_module: Any | None = None
    try:
        import torch
    except ImportError:  # pragma: no cover - torch is required when this is used
        pass
    else:
        torch_module = torch

    if isinstance(value, np.ndarray):
        if np.issubdtype(value.dtype, np.floating) and value.dtype != np.float32:
            return value.astype(np.float32, copy=False)
        return value

    if torch_module is not None and torch_module.is_tensor(value):
        if value.dtype.is_floating_point and value.dtype != torch_module.float32:
            return value.to(dtype=torch_module.float32)
        return value

    if isinstance(value, list):
        return [_coerce_conditioning_audio(item) for item in value]

    if isinstance(value, tuple):
        return tuple(_coerce_conditioning_audio(item) for item in value)

    return value


def _reference_availability_error(reference_audio: Path) -> str | None:
    if not reference_audio.exists():
        return f"Chatterbox reference audio not found: {reference_audio}"
    if not reference_audio.is_file():
        return f"Chatterbox reference audio is not a file: {reference_audio}"
    return None


def _prepare_turbo_conditionals(
    model: Any,
    reference_audio: Path,
    *,
    exaggeration: float = 0.0,
    norm_loudness: bool = True,
) -> None:
    """Prepare Chatterbox Turbo conditionals with float32-safe reference audio handling."""
    import librosa
    import torch
    from chatterbox import tts_turbo as chatterbox_turbo

    s3gen_sr = getattr(chatterbox_turbo, "S3GEN_SR", getattr(model, "sr", 24000))
    s3_sr = getattr(chatterbox_turbo, "S3_SR", 16000)

    s3gen_ref_wav, sample_rate = librosa.load(str(reference_audio), sr=s3gen_sr)
    s3gen_ref_wav = _coerce_conditioning_audio(s3gen_ref_wav)

    if len(s3gen_ref_wav) / sample_rate <= 5.0:
        raise ValueError("Audio prompt must be longer than 5 seconds!")

    if norm_loudness:
        s3gen_ref_wav = _coerce_conditioning_audio(model.norm_loudness(s3gen_ref_wav, sample_rate))

    ref_16k_wav = librosa.resample(s3gen_ref_wav, orig_sr=s3gen_sr, target_sr=s3_sr)
    ref_16k_wav = _coerce_conditioning_audio(ref_16k_wav)
    s3gen_ref_wav = _coerce_conditioning_audio(s3gen_ref_wav[: model.DEC_COND_LEN])

    s3gen_ref_dict = model.s3gen.embed_ref(s3gen_ref_wav, s3gen_sr, device=model.device)

    t3_cond_prompt_tokens = None
    if plen := model.t3.hp.speech_cond_prompt_len:
        tokenizer = model.s3gen.tokenizer
        t3_cond_prompt_tokens, _ = tokenizer.forward(
            [ref_16k_wav[: model.ENC_COND_LEN]], max_len=plen
        )
        t3_cond_prompt_tokens = torch.atleast_2d(t3_cond_prompt_tokens).to(model.device)  # type: ignore[no-untyped-call]

    ve_embed_array = np.asarray(
        _coerce_conditioning_audio(model.ve.embeds_from_wavs([ref_16k_wav], sample_rate=s3_sr))
    )
    ve_embed = torch.from_numpy(ve_embed_array).mean(axis=0, keepdim=True).to(model.device)  # type: ignore[call-overload]

    t3_cond = chatterbox_turbo.T3Cond(
        speaker_emb=ve_embed,
        cond_prompt_speech_tokens=t3_cond_prompt_tokens,
        emotion_adv=exaggeration * torch.ones(1, 1, 1),
    ).to(device=model.device)
    model.conds = chatterbox_turbo.Conditionals(t3_cond, s3gen_ref_dict).to(device=model.device)


class ChatterboxTurboProvider(BaseTTSProvider):
    """Local TTS via Chatterbox Turbo. Lazy-loads model on first use.

    Provides zero-shot voice cloning from a reference audio clip.
    Implements TTSProvider protocol from gobby.voice.tts.
    """

    provider_name = "chatterbox"
    capabilities = TTSProviderCapabilities(
        supports_reference_audio=True,
        supports_reference_text=False,
        supports_streaming=False,
        supports_voice_cloning=True,
    )

    def __init__(self, config: VoiceConfig) -> None:
        super().__init__(config)
        self._model: Any | None = None
        # Initialize the lock eagerly so two coroutines arriving in
        # _ensure_model concurrently cannot create separate locks.
        self._load_lock: asyncio.Lock = asyncio.Lock()
        self._synthesis_lock: asyncio.Lock = asyncio.Lock()
        self._sample_rate = 24000  # Chatterbox outputs 24kHz
        self._reference_audio = Path(config.tts_reference_audio).expanduser()
        self._conditioning_ready = False
        self._runtime_primed = False

    def _availability(self) -> tuple[bool, str]:
        if not _module_is_available("chatterbox"):
            return False, "chatterbox not installed (uv sync --extra voice)"

        reference_error = _reference_availability_error(self._reference_audio)
        if reference_error is not None:
            return False, reference_error
        return True, ""

    def _status_details(self) -> dict[str, Any]:
        return {
            "tts_reference_audio": str(self._reference_audio),
            "tts_reference_audio_exists": self._reference_audio.exists(),
            "tts_reference_audio_conditioned": self._conditioning_ready,
            "tts_runtime_primed": self._runtime_primed,
            "tts_device": self._config.tts_device,
            "tts_chatterbox_max_generation_tokens": (
                self._config.tts_chatterbox_max_generation_tokens
            ),
        }

    async def warmup(self) -> None:
        """Public entry point for preloading the TTS model."""
        model = await self._ensure_model()
        if self._runtime_primed:
            return

        async with self._synthesis_lock:
            if self._runtime_primed:
                return
            logger.info("Priming Chatterbox Turbo synthesis runtime")
            try:
                await asyncio.to_thread(self._prime_synthesis_runtime, model)
            except Exception as exc:
                self._runtime_primed = False
                raise RuntimeError(self._format_synthesis_error(exc)) from exc
            self._runtime_primed = True
            logger.info("Chatterbox Turbo synthesis runtime primed successfully")

    def unload(self) -> None:
        """Release the model to reclaim memory.

        Safe to call from sync contexts: ``synthesize_stream`` captures the
        model in a local variable, so clearing ``self._model`` cannot affect
        an in-flight synthesis. Python attribute assignment is GIL-atomic.
        """
        if self._model is not None and hasattr(self._model, "conds"):
            self._model.conds = None
        self._model = None
        self._conditioning_ready = False
        self._runtime_primed = False

    def _prepare_reference_conditioning(self, model: Any) -> None:
        _prepare_turbo_conditionals(
            model,
            self._reference_audio,
            exaggeration=0.0,
            norm_loudness=True,
        )

    def _format_conditioning_error(self, exc: Exception) -> str:
        if isinstance(exc, FileNotFoundError):
            return f"Chatterbox reference audio not found: {self._reference_audio}"
        if isinstance(exc, PermissionError):
            return f"Chatterbox reference audio is not readable: {self._reference_audio}"
        message = str(exc).strip()
        if message:
            return f"Chatterbox reference audio is invalid: {message}"
        return f"Failed to prepare Chatterbox reference audio: {self._reference_audio}"

    def _format_synthesis_error(self, exc: Exception) -> str:
        message = str(exc).strip()
        if message:
            return f"Chatterbox TTS synthesis failed: {message}"
        return "Chatterbox TTS synthesis failed"

    def _generate_with_token_cap(
        self,
        model: Any,
        text: str,
        *,
        max_generation_tokens: int | None = None,
    ) -> Any:
        turbo_decoder = getattr(model, "t3", None)
        if turbo_decoder is None:
            return model.generate(
                text,
                temperature=self._config.tts_temperature,
            )

        original_inference_turbo = getattr(turbo_decoder, "inference_turbo", None)
        token_cap = max_generation_tokens or self._config.tts_chatterbox_max_generation_tokens

        if not callable(original_inference_turbo):
            return model.generate(
                text,
                temperature=self._config.tts_temperature,
            )

        def _capped_inference_turbo(*args: Any, **kwargs: Any) -> Any:
            kwargs["max_gen_len"] = token_cap
            return original_inference_turbo(*args, **kwargs)

        turbo_decoder.inference_turbo = _capped_inference_turbo
        try:
            return model.generate(
                text,
                temperature=self._config.tts_temperature,
            )
        finally:
            turbo_decoder.inference_turbo = original_inference_turbo

    def _prime_synthesis_runtime(self, model: Any) -> None:
        self._generate_with_token_cap(
            model,
            _WARMUP_PRIME_TEXT,
            max_generation_tokens=min(
                self._config.tts_chatterbox_max_generation_tokens,
                _WARMUP_PRIME_MAX_GENERATION_TOKENS,
            ),
        )

    async def _ensure_model(self) -> Any:
        """Lazy-load the Chatterbox Turbo model (thread-safe, async)."""
        if self._model is not None and self._conditioning_ready:
            return self._model

        async with self._load_lock:
            if self._model is not None and self._conditioning_ready:
                return self._model

            device = self._config.tts_device
            if device == "auto":
                device = _auto_device()

            if self._model is None:
                logger.info(f"Loading Chatterbox Turbo model (device={device})")

                def _load() -> Any:
                    from chatterbox.tts_turbo import ChatterboxTurboTTS

                    return ChatterboxTurboTTS.from_pretrained(device=device)

                self._model = await asyncio.to_thread(_load)
                assert self._model is not None  # just loaded above
                self._sample_rate = self._model.sr
                logger.info("Chatterbox Turbo model loaded successfully")

            reference_error = _reference_availability_error(self._reference_audio)
            if reference_error is not None:
                raise RuntimeError(reference_error)

            if not self._conditioning_ready:
                logger.info(
                    "Preparing Chatterbox Turbo reference conditioning from %s",
                    self._reference_audio,
                )
                try:
                    await asyncio.to_thread(self._prepare_reference_conditioning, self._model)
                except Exception as exc:
                    if hasattr(self._model, "conds"):
                        self._model.conds = None
                    self._conditioning_ready = False
                    raise RuntimeError(self._format_conditioning_error(exc)) from exc
                self._conditioning_ready = True
                logger.info("Chatterbox Turbo reference conditioning prepared successfully")
            return self._model

    async def synthesize_stream(self, text: str) -> AsyncIterator[tuple[bytes, int]]:
        """Generate speech for text, yielding (pcm_int16_bytes, sample_rate).

        Each call synthesizes the full text (typically a single sentence from
        SentenceBuffer) and yields the audio as one chunk. Turbo's sub-200ms
        latency makes this real-time for sentence-level synthesis.
        """
        # Re-raise model-load failures so callers see them instead of getting
        # an empty iterator with no signal of what went wrong. The websocket
        # warmup task and synthesize_stream consumers both need to know.
        model = await self._ensure_model()

        try:
            async with self._synthesis_lock:
                wav = await asyncio.to_thread(self._generate_with_token_cap, model, text)
                self._runtime_primed = True

            # Convert torch.Tensor to PCM int16 bytes
            samples = wav.squeeze().cpu().numpy()
            pcm_int16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
            yield pcm_int16.tobytes(), self._sample_rate

        except asyncio.CancelledError:
            logger.debug("Chatterbox TTS synthesis cancelled")
            raise
        except Exception as exc:
            logger.error("Chatterbox TTS synthesis failed", exc_info=True)
            raise RuntimeError(self._format_synthesis_error(exc)) from exc

    @property
    def sample_rate(self) -> int:
        """Output sample rate in Hz (24kHz)."""
        return self._sample_rate

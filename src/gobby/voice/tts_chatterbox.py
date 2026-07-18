"""Text-to-speech via Chatterbox Turbo (voice cloning, local inference).

Lazy-loads the model on first synthesis to avoid slowing daemon boot.
Uses ChatterboxTurboTTS for sub-200ms latency with zero-shot voice cloning.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from gobby.config.voice import VoiceConfig
from gobby.voice._warnings import suppress_perth_pkg_resources_warning
from gobby.voice.load_guard import ModelLoadGuard, default_tts_load_guard_path
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
    import numpy as np

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


def _runtime_import_error() -> str | None:
    if not _module_is_available("chatterbox"):
        return "daemon environment is missing required package chatterbox-tts; run uv sync"
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
    import numpy as np
    import torch

    with suppress_perth_pkg_resources_warning():
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

    t3_cond_prompt_tokens: torch.Tensor | None = None
    if plen := model.t3.hp.speech_cond_prompt_len:
        tokenizer = model.s3gen.tokenizer
        t3_cond_prompt_tokens, _ = tokenizer.forward(
            [ref_16k_wav[: model.ENC_COND_LEN]], max_len=plen
        )
        t3_cond_prompt_tokens = torch.as_tensor(t3_cond_prompt_tokens, device=model.device)
        if t3_cond_prompt_tokens.ndim < 2:
            t3_cond_prompt_tokens = t3_cond_prompt_tokens.reshape(1, -1)

    ve_embed_array = np.asarray(
        _coerce_conditioning_audio(model.ve.embeds_from_wavs([ref_16k_wav], sample_rate=s3_sr))
    )
    ve_embed = torch.from_numpy(ve_embed_array).mean(dim=0, keepdim=True).to(model.device)

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
        # Cancellation releases the async lock without stopping asyncio.to_thread.
        # The worker-owned lock therefore remains held until model.generate returns.
        self._inference_lock = threading.Lock()
        self._active_token_cap: int | None = None
        self._token_cap_decoder: Any | None = None
        self._token_cap_inference: Any | None = None
        self._sample_rate = 24000  # Chatterbox outputs 24kHz
        self._reference_audio = Path(config.tts_reference_audio).expanduser()
        self._conditioning_ready = False
        self._runtime_primed = False
        self._inflight_load: asyncio.Future[Any] | None = None
        self._mps_used = False
        self._load_guard = ModelLoadGuard(default_tts_load_guard_path())

    def _availability(self) -> tuple[bool, str]:
        reference_error = _reference_availability_error(self._reference_audio)
        if reference_error is not None:
            return False, reference_error
        runtime_error = _runtime_import_error()
        if runtime_error is not None:
            return False, runtime_error
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

    async def unload(self) -> None:
        """Release the model after active loading and synthesis complete."""
        inflight = self._inflight_load
        if inflight is not None and not inflight.done():
            # A cancelled warmup releases _load_lock while the load thread is
            # still initializing MPS; wait for the actual thread to finish
            # before touching the allocator (incident #18196 SIGSEGV).
            await asyncio.wait([inflight])
        if inflight is not None and inflight.done() and not inflight.cancelled():
            inflight.exception()  # consume, avoid never-retrieved warning
        self._inflight_load = None
        async with self._load_lock:
            async with self._synthesis_lock:
                had_model = self._model is not None
                self._model = None
                self._token_cap_decoder = None
                self._token_cap_inference = None
                self._conditioning_ready = False
                self._runtime_primed = False
        if had_model or self._mps_used:
            await asyncio.to_thread(self._release_mps_cache)

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
        with self._inference_lock:
            token_cap_enabled = self._install_token_cap(model)
            if not token_cap_enabled:
                # Fail closed: the token cap is the only bound on MPS
                # generation memory; unbounded generation is incident
                # #18196's failure class.
                raise RuntimeError(
                    "Chatterbox inference API drift: generation token cap could not "
                    "be installed; synthesis disabled for memory safety — update "
                    "gobby's chatterbox pin"
                )
            token_cap = max_generation_tokens or self._config.tts_chatterbox_max_generation_tokens
            self._active_token_cap = token_cap
            try:
                return model.generate(
                    text,
                    temperature=self._config.tts_temperature,
                )
            finally:
                self._active_token_cap = None

    def _install_token_cap(self, model: Any) -> bool:
        turbo_decoder = getattr(model, "t3", None)
        if turbo_decoder is None:
            logger.warning("Chatterbox token cap disabled: model.t3 is missing")
            return False

        if (
            turbo_decoder is self._token_cap_decoder
            and turbo_decoder.inference_turbo is self._token_cap_inference
        ):
            return True

        original_inference_turbo = getattr(turbo_decoder, "inference_turbo", None)
        if not callable(original_inference_turbo):
            logger.warning(
                "Chatterbox token cap disabled: model.t3.inference_turbo is not callable"
            )
            return False

        def _capped_inference_turbo(*args: Any, **kwargs: Any) -> Any:
            if self._active_token_cap is not None:
                kwargs["max_gen_len"] = self._active_token_cap
            return original_inference_turbo(*args, **kwargs)

        # Install one stable wrapper for the model lifetime. Per-call replacement can
        # nest wrappers when an awaiting coroutine is cancelled mid-inference.
        turbo_decoder.inference_turbo = _capped_inference_turbo
        self._token_cap_decoder = turbo_decoder
        self._token_cap_inference = _capped_inference_turbo
        return True

    def _apply_mps_memory_cap(self) -> None:
        """Cap MPS unified-memory use before the first allocator touch (fail-open).

        Uncapped, torch MPS can balloon far past physical RAM on unified
        memory. The absolute limit converts to an allocator fraction of
        Metal's recommended working set; breaching it raises a torch OOM
        instead of OOM-crashing the host.
        """
        try:
            import torch

            mps = getattr(torch, "mps", None)
            if mps is None or not hasattr(mps, "set_per_process_memory_fraction"):
                return
            recommended = getattr(mps, "recommended_max_memory", None)
            if not callable(recommended):
                return
            total = float(recommended())
            if total <= 0:
                return
            limit_bytes = self._config.tts_mps_memory_limit_gb * 1024**3
            fraction = min(1.0, limit_bytes / total)
            mps.set_per_process_memory_fraction(fraction)
            logger.info(
                "MPS memory cap applied: %.1fGB (fraction %.3f of recommended max)",
                self._config.tts_mps_memory_limit_gb,
                fraction,
            )
        except Exception:
            logger.warning("Failed to apply MPS memory cap", exc_info=True)

    def _release_mps_cache(self) -> None:
        """Release cached MPS memory after a real unload.

        Never the first MPS touch: empty_cache() bootstraps the entire MPS
        allocator when nothing was loaded — the exact native-crash surface
        from incident #18196 — so this only runs when this provider actually
        used MPS and torch is already imported.
        """
        if not self._mps_used or "torch" not in sys.modules:
            return
        try:
            import gc

            import torch

            gc.collect()
            mps = getattr(torch, "mps", None)
            if mps is not None and hasattr(mps, "empty_cache"):
                mps.empty_cache()
        except Exception:
            logger.debug("MPS cache release skipped", exc_info=True)

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

            if self._model is None:
                configured_device = self._config.tts_device
                logger.info("Loading Chatterbox Turbo model (device=%s)", configured_device)

                guard_reason = self._load_guard.check()
                if guard_reason is not None:
                    raise RuntimeError(guard_reason)

                def _load() -> Any:
                    device = _auto_device() if configured_device == "auto" else configured_device
                    if device == "mps":
                        self._mps_used = True
                        self._apply_mps_memory_cap()
                    with suppress_perth_pkg_resources_warning():
                        from chatterbox.tts_turbo import ChatterboxTurboTTS

                    model = ChatterboxTurboTTS.from_pretrained(device=device)
                    with self._inference_lock:
                        self._install_token_cap(model)
                    return model

                # A native torch/MPS crash kills the process mid-load; the
                # fsynced marker (cleared only on success) is what survives.
                self._load_guard.record_attempt()
                # run_in_executor (not to_thread): the returned future survives
                # caller cancellation, so unload() can await true thread
                # completion instead of racing a detached load thread.
                loop = asyncio.get_running_loop()
                self._inflight_load = loop.run_in_executor(None, _load)
                try:
                    self._model = await self._inflight_load
                finally:
                    if self._inflight_load is not None and self._inflight_load.done():
                        self._inflight_load = None
                self._load_guard.record_success()
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

            def _generate_pcm() -> bytes:
                wav = self._generate_with_token_cap(model, text)

                import numpy as np

                samples = wav.squeeze().cpu().numpy()
                pcm_int16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
                return bytes(pcm_int16.tobytes())

            async with self._synthesis_lock:
                pcm_bytes = await asyncio.to_thread(_generate_pcm)
                self._runtime_primed = True

            if pcm_bytes:
                yield pcm_bytes, self._sample_rate

        except asyncio.CancelledError:
            logger.debug("Chatterbox TTS synthesis cancelled")
            raise
        except Exception as exc:
            logger.exception("Chatterbox TTS synthesis failed")
            raise RuntimeError(self._format_synthesis_error(exc)) from exc

    @property
    def sample_rate(self) -> int:
        """Output sample rate in Hz (24kHz)."""
        return self._sample_rate

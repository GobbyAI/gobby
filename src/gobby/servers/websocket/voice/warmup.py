"""Voice provider lookup, warmup, and unload lifecycle."""

from __future__ import annotations

import asyncio
import gc
import importlib
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.config.voice import VoiceConfig
    from gobby.voice.tts import TTSProvider

logger = logging.getLogger("gobby.servers.websocket.voice")

_WARMUP_IDLE = "idle"
_WARMUP_LOADING = "loading"
_WARMUP_READY = "ready"
_WARMUP_ERROR = "error"


class VoiceWarmupMixin:
    """Provider lookup, dependency checks, warmup status, and model unloading."""

    daemon_config: Any
    _background_tasks: set[asyncio.Task[Any]]
    _chat_sessions: dict[str, Any]
    _stt_deps_checked: bool
    _stt_warmup_error: str
    _stt_warmup_status: str
    _tts_deps_checked: bool
    _tts_provider: TTSProvider | None
    _tts_warmup_error: str
    _tts_warmup_status: str
    _voice_warmup_task: asyncio.Task[None] | None
    _whisper_stt: Any

    def _spawn_background_task(self, coro: Any, name: str | None = None) -> asyncio.Task[Any]:
        """Schedule a fire-and-forget task and retain a reference to it.

        asyncio holds only weak references to tasks, so unstored tasks can
        be garbage-collected mid-execution. We add the task to a set and
        remove it on completion via add_done_callback.
        """
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _get_voice_config(self) -> VoiceConfig | None:
        """Get voice config from daemon_config if available."""
        config = getattr(self, "daemon_config", None)
        if config and hasattr(config, "voice"):
            voice: VoiceConfig | None = config.voice
            return voice
        return None

    def _get_stt(self) -> Any:
        """Get or create the WhisperSTT singleton."""
        if self._whisper_stt is not None:
            return self._whisper_stt

        voice_config = self._get_voice_config()
        if not voice_config or not voice_config.enabled or not voice_config.stt_enabled:
            return None

        # Auto-install STT deps if missing (fire-and-forget on first call)
        if not self._stt_deps_checked:
            self._stt_deps_checked = True
            self._spawn_background_task(self._ensure_stt_deps(voice_config), name="ensure-stt-deps")

        from gobby.voice.stt import WhisperSTT

        self._whisper_stt = WhisperSTT(voice_config)
        return self._whisper_stt

    def _get_stt_availability(self) -> tuple[bool, str]:
        """Return package-level STT availability and reason."""
        voice_config = self._get_voice_config()
        if not voice_config or not voice_config.enabled:
            return False, "Voice not enabled in config"
        if not voice_config.stt_enabled:
            return False, "STT disabled in config"
        try:
            importlib.import_module("faster_whisper")
            return True, ""
        except ImportError:
            return False, "faster-whisper not installed (uv sync --extra voice)"

    def _get_tts(self) -> TTSProvider | None:
        """Get or create the TTS singleton (routes by provider config)."""
        if self._tts_provider is not None:
            return self._tts_provider

        voice_config = self._get_voice_config()
        if not voice_config or not voice_config.enabled or not voice_config.tts_enabled:
            return None

        # Auto-install TTS deps if missing
        if not self._tts_deps_checked:
            self._tts_deps_checked = True
            self._spawn_background_task(self._ensure_tts_deps(voice_config), name="ensure-tts-deps")

        from gobby.voice.providers import create_tts_provider

        tts = create_tts_provider(voice_config)
        if tts is None:
            return None

        if not tts.is_available:
            return None

        self._tts_provider = tts
        return self._tts_provider

    def _get_tts_availability(self) -> tuple[bool, str]:
        """Return package-level TTS availability and reason."""
        voice_config = self._get_voice_config()
        if not voice_config or not voice_config.enabled:
            return False, "Voice not enabled in config"
        if not voice_config.tts_enabled:
            return False, "TTS disabled in config"

        from gobby.voice.providers import get_tts_status_for_config

        status = get_tts_status_for_config(voice_config)
        return status.available, status.reason

    def _resolve_voice_warmup_targets(
        self,
        voice_config: VoiceConfig,
        *,
        want_stt: bool | None = None,
        want_tts: bool | None = None,
    ) -> tuple[bool, bool]:
        """Resolve requested voice targets against daemon config.

        ``None`` preserves historical config-enabled behavior for compatibility.
        """
        return (
            voice_config.stt_enabled
            if want_stt is None
            else bool(want_stt and voice_config.stt_enabled),
            voice_config.tts_enabled
            if want_tts is None
            else bool(want_tts and voice_config.tts_enabled),
        )

    def start_voice_warmup(
        self,
        *,
        want_stt: bool | None = None,
        want_tts: bool | None = None,
    ) -> bool:
        """Begin best-effort background warmup for requested voice models."""
        voice_config = self._get_voice_config()
        if not voice_config or not voice_config.enabled:
            return False

        warm_stt, warm_tts = self._resolve_voice_warmup_targets(
            voice_config,
            want_stt=want_stt,
            want_tts=want_tts,
        )
        should_warm = False
        if warm_stt and self._stt_warmup_status != _WARMUP_READY:
            self._stt_warmup_status = _WARMUP_LOADING
            self._stt_warmup_error = ""
            should_warm = True
        if warm_tts and self._tts_warmup_status != _WARMUP_READY:
            self._tts_warmup_status = _WARMUP_LOADING
            self._tts_warmup_error = ""
            should_warm = True

        if not should_warm:
            return False

        if self._voice_warmup_task is not None and not self._voice_warmup_task.done():
            return False

        self._voice_warmup_task = asyncio.create_task(
            self._warm_voice_models(),
            name="voice-model-warmup",
        )
        self._voice_warmup_task.add_done_callback(self._on_voice_warmup_done)
        return True

    async def stop_voice_warmup(self) -> None:
        """Cancel background voice warmup if it is still in progress."""
        task = self._voice_warmup_task
        if task is None:
            return

        self._voice_warmup_task = None
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _reset_voice_warmup_state(self) -> None:
        """Reset warmup bookkeeping so the next prepare request starts fresh."""
        self._stt_warmup_status = _WARMUP_IDLE
        self._tts_warmup_status = _WARMUP_IDLE
        self._stt_warmup_error = ""
        self._tts_warmup_error = ""

    def get_voice_status(
        self,
        *,
        want_stt: bool | None = None,
        want_tts: bool | None = None,
    ) -> dict[str, Any]:
        """Return voice feature availability and warmup state."""
        voice_config = self._get_voice_config()
        if voice_config is None:
            return {
                "enabled": False,
                "stt_available": False,
                "reason": "Voice config not found",
                "voice_ready": False,
                "voice_loading": False,
                "stt_warmup_status": _WARMUP_IDLE,
                "tts_warmup_status": _WARMUP_IDLE,
                "stt_warmup_error": "",
                "tts_warmup_error": "",
            }

        stt_available, stt_reason = self._get_stt_availability()
        from gobby.voice.providers import get_tts_status_for_config

        scope_stt, scope_tts = self._resolve_voice_warmup_targets(
            voice_config,
            want_stt=want_stt,
            want_tts=want_tts,
        )
        stt_warmup_status = self._stt_warmup_status if voice_config.stt_enabled else _WARMUP_IDLE
        tts_warmup_status = self._tts_warmup_status if voice_config.tts_enabled else _WARMUP_IDLE

        voice_ready = (
            voice_config.enabled
            and (scope_stt or scope_tts)
            and (not scope_stt or stt_warmup_status == _WARMUP_READY)
            and (not scope_tts or tts_warmup_status == _WARMUP_READY)
        )
        voice_loading = (scope_stt and stt_warmup_status == _WARMUP_LOADING) or (
            scope_tts and tts_warmup_status == _WARMUP_LOADING
        )

        result: dict[str, Any] = {
            "enabled": voice_config.enabled,
            "stt_enabled": voice_config.stt_enabled,
            "stt_available": stt_available,
            "stt_reason": stt_reason,
            "whisper_model": voice_config.whisper_model_size,
            "stt_warmup_status": stt_warmup_status,
            "stt_warmup_error": self._stt_warmup_error if scope_stt else "",
            "tts_enabled": voice_config.tts_enabled,
            "tts_warmup_status": tts_warmup_status,
            "tts_warmup_error": self._tts_warmup_error if scope_tts else "",
            "voice_ready": voice_ready,
            "voice_loading": voice_loading,
        }

        tts_status = (
            self._tts_provider.get_status()
            if self._tts_provider is not None
            else get_tts_status_for_config(voice_config)
        )
        result.update(tts_status.as_status_fields())

        return result

    async def _warm_voice_models(self) -> None:
        """Warm requested voice models without blocking daemon startup."""
        voice_config = self._get_voice_config()
        if not voice_config or not voice_config.enabled:
            return

        while True:
            warmups: list[asyncio.Task[None]] = []
            if voice_config.stt_enabled and self._stt_warmup_status == _WARMUP_LOADING:
                warmups.append(asyncio.create_task(self._warm_stt_model(), name="warm-stt"))
            if voice_config.tts_enabled and self._tts_warmup_status == _WARMUP_LOADING:
                warmups.append(asyncio.create_task(self._warm_tts_model(), name="warm-tts"))
            if not warmups:
                return
            await asyncio.gather(*warmups)

    def _on_voice_warmup_done(self, task: asyncio.Task[None]) -> None:
        """Log unexpected warmup task failures."""
        if self._voice_warmup_task is task:
            self._voice_warmup_task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Voice warmup task failed", exc_info=exc)
        if self._voice_warmup_task is None and (
            self._stt_warmup_status == _WARMUP_LOADING or self._tts_warmup_status == _WARMUP_LOADING
        ):
            self._voice_warmup_task = asyncio.create_task(
                self._warm_voice_models(),
                name="voice-model-warmup",
            )
            self._voice_warmup_task.add_done_callback(self._on_voice_warmup_done)

    async def _warm_stt_model(self) -> None:
        """Warm the Whisper STT model."""
        started_at = time.perf_counter()
        try:
            voice_config = self._get_voice_config()
            if voice_config is None:
                raise RuntimeError("Voice config not found")
            deps_ready = await self._ensure_stt_deps(voice_config)
            self._stt_deps_checked = True
            if not deps_ready:
                available, reason = self._get_stt_availability()
                raise RuntimeError(reason if not available else "STT dependency setup failed")

            stt = self._get_stt()
            if stt is None:
                available, reason = self._get_stt_availability()
                raise RuntimeError(reason if not available else "STT is not configured")
            if not stt.is_available:
                raise RuntimeError("faster-whisper not installed (uv sync --extra voice)")
            logger.info("Starting Whisper STT warmup")
            await stt.warmup()
            self._stt_warmup_status = _WARMUP_READY
            self._stt_warmup_error = ""
            logger.info(f"Whisper STT warmup complete in {time.perf_counter() - started_at:.2f}s")
        except Exception as exc:
            self._stt_warmup_status = _WARMUP_ERROR
            self._stt_warmup_error = str(exc)
            logger.error("Whisper STT warmup failed", exc_info=True)

    async def _warm_tts_model(self) -> None:
        """Warm the configured TTS model."""
        started_at = time.perf_counter()
        try:
            voice_config = self._get_voice_config()
            if voice_config is None:
                raise RuntimeError("Voice config not found")
            deps_ready = await self._ensure_tts_deps(voice_config)
            self._tts_deps_checked = True
            if not deps_ready:
                available, reason = self._get_tts_availability()
                raise RuntimeError(reason if not available else "TTS dependency setup failed")

            tts = self._get_tts()
            if tts is None:
                available, reason = self._get_tts_availability()
                raise RuntimeError(reason if not available else "TTS is not configured")
            logger.info("Starting TTS warmup")
            await tts.warmup()
            self._tts_warmup_status = _WARMUP_READY
            self._tts_warmup_error = ""
            logger.info(f"TTS warmup complete in {time.perf_counter() - started_at:.2f}s")
        except Exception as exc:
            self._tts_warmup_status = _WARMUP_ERROR
            self._tts_warmup_error = str(exc)
            logger.error("TTS warmup failed", exc_info=True)

    async def _ensure_stt_deps(self, voice_config: VoiceConfig) -> bool:
        """Auto-install STT dependencies if missing."""
        try:
            from gobby.voice.dep_check import ensure_stt_deps

            return await ensure_stt_deps(voice_config)
        except (ImportError, RuntimeError, OSError):
            logger.debug("STT dep check failed", exc_info=True)
            return False

    async def _ensure_tts_deps(self, voice_config: VoiceConfig) -> bool:
        """Auto-install TTS dependencies if missing."""
        try:
            from gobby.voice.dep_check import ensure_tts_deps

            return await ensure_tts_deps(voice_config)
        except (ImportError, RuntimeError, OSError):
            logger.debug("TTS dep check failed", exc_info=True)
            return False

    async def _unload_voice_models(self) -> None:
        """Release voice models to reclaim memory.

        Called when the last web chat session is removed.
        """
        task = self._voice_warmup_task
        self._voice_warmup_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        unloaded: list[str] = []

        if self._whisper_stt is not None:
            self._whisper_stt.unload()
            self._whisper_stt = None
            unloaded.append("STT")

        if self._tts_provider is not None:
            self._tts_provider.unload()
            self._tts_provider = None
            unloaded.append("TTS")

        self._reset_voice_warmup_state()

        if not unloaded:
            return

        # Reclaim memory
        gc.collect()
        try:
            import torch

            if hasattr(torch, "mps") and hasattr(torch.mps, "empty_cache"):
                torch.mps.empty_cache()
        except (ImportError, OSError, RuntimeError):
            logger.debug("Torch cache cleanup skipped", exc_info=True)

        logger.info(f"Voice models unloaded ({', '.join(unloaded)}) — memory reclaimed")

    async def _check_voice_idle(self) -> None:
        """Unload voice models if no web chat sessions remain."""
        chat_sessions: dict[str, Any] = getattr(self, "_chat_sessions", {})
        if len(chat_sessions) > 0:
            return

        models_loaded = (
            self._stt_warmup_status == _WARMUP_READY or self._tts_warmup_status == _WARMUP_READY
        )
        warmup_in_flight = (
            self._voice_warmup_task is not None and not self._voice_warmup_task.done()
        )
        if models_loaded or warmup_in_flight:
            await self._unload_voice_models()

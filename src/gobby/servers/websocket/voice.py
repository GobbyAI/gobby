"""WebSocket voice chat handling.

VoiceMixin provides STT + TTS integration for WebSocketServer.
Voice layers on top of the existing chat pipeline — transcribed audio
becomes a normal chat_message, and streamed assistant text feeds TTS.
"""

from __future__ import annotations

import asyncio
import base64
import gc
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from websockets.exceptions import ConnectionClosed, ConnectionClosedError

logger = logging.getLogger(__name__)

_WARMUP_IDLE = "idle"
_WARMUP_LOADING = "loading"
_WARMUP_READY = "ready"
_WARMUP_ERROR = "error"


async def _broadcast_tts_status(
    clients: dict[Any, dict[str, Any]],
    conversation_id: str,
    status: str,
    *,
    error: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "type": "tts_status",
        "conversation_id": conversation_id,
        "status": status,
    }
    if error:
        payload["error"] = error

    message = json.dumps(payload)
    for ws, meta in list(clients.items()):
        cid = meta.get("conversation_id") if meta else None
        if cid is not None and cid != conversation_id:
            continue
        try:
            await ws.send(message)
        except (ConnectionClosed, ConnectionClosedError):
            pass


if TYPE_CHECKING:
    from gobby.config.voice import VoiceConfig
    from gobby.voice.tts import TTSProvider


class TTSPipeline:
    """Manages TTS state for a single conversation's response stream.

    Created per-response, feeds text chunks through a sentence buffer,
    synthesizes complete sentences, and streams audio to WebSocket clients.
    """

    def __init__(
        self,
        tts: TTSProvider,
        conversation_id: str,
        clients: dict[Any, dict[str, Any]],
    ) -> None:
        from gobby.voice.sentence_buffer import SentenceBuffer

        self.tts = tts
        self.conversation_id = conversation_id
        self.clients = clients
        self.sentence_buffer = SentenceBuffer()
        self._chunk_index = 0
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._flush_called = False
        self._failed = False
        self._worker_task: asyncio.Task[None] = asyncio.create_task(
            self._run_worker(),
            name=f"tts-pipeline-{conversation_id[:8]}",
        )

    def feed_text(self, chunk: str) -> None:
        """Feed a text chunk from the LLM stream and enqueue complete sentences."""
        sentences = self.sentence_buffer.feed(chunk)
        for sentence in sentences:
            self._queue.put_nowait(sentence)

    async def flush(self) -> None:
        """Flush remaining buffer at end of stream in FIFO order.

        Idempotent: a second call is a no-op so we never enqueue the sentinel
        twice (which would leave the second sentinel hanging in the queue
        after the worker has already exited on the first one).
        """
        if self._flush_called:
            return
        self._flush_called = True
        remaining = self.sentence_buffer.flush()
        if remaining:
            await self._queue.put(remaining)
        await self._queue.join()
        # Send sentinel so the worker task exits cleanly instead of
        # hanging forever on _queue.get() after all work is done.
        await self._queue.put(None)

    async def cancel(self) -> None:
        """Cancel queued and active TTS work."""
        self.sentence_buffer.clear()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break

        if not self._worker_task.done():
            self._worker_task.cancel()
        await asyncio.gather(self._worker_task, return_exceptions=True)

    async def _run_worker(self) -> None:
        """Serialize sentence synthesis so later sentences cannot overtake earlier ones."""
        try:
            while True:
                text = await self._queue.get()
                try:
                    if text is None:
                        return
                    if self._failed:
                        continue
                    await self._synthesize_and_send(text)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise

    async def _synthesize_and_send(self, text: str) -> None:
        """Synthesize a sentence and send audio chunks to all conversation clients."""
        try:
            async for pcm_bytes, sample_rate in self.tts.synthesize_stream(text):
                # Send metadata frame (JSON)
                meta = json.dumps(
                    {
                        "type": "tts_audio",
                        "conversation_id": self.conversation_id,
                        "sample_rate": sample_rate,
                        "format": "pcm_s16le",
                        "chunk_index": self._chunk_index,
                    }
                )
                self._chunk_index += 1

                # Broadcast to all clients in this conversation
                for ws, ws_meta in list(self.clients.items()):
                    cid = ws_meta.get("conversation_id") if ws_meta else None
                    if cid is not None and cid != self.conversation_id:
                        continue
                    try:
                        await ws.send(meta)
                        await ws.send(pcm_bytes)
                    except (ConnectionClosed, ConnectionClosedError):
                        pass

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed = True
            logger.error("TTS synthesis/send failed", exc_info=True)
            error_message = str(exc).strip() or "TTS synthesis failed"
            await _broadcast_tts_status(
                self.clients,
                self.conversation_id,
                "error",
                error=error_message,
            )


class VoiceMixin:
    """Mixin providing voice chat methods for WebSocketServer.

    Requires on the host class:
    - ``self.daemon_config`` with a ``.voice`` attribute
    - ``self._handle_chat_message(ws, data)`` (from ChatMixin)
    - ``self.clients: dict[Any, dict[str, Any]]``
    """

    # Declare expected attributes for type checking
    clients: dict[Any, dict[str, Any]]

    if TYPE_CHECKING:

        async def _handle_chat_message(self, websocket: Any, data: dict[str, Any]) -> None: ...

    def _init_voice(self) -> None:
        """Initialize voice subsystem state. Called from __init__."""
        # Per-conversation voice mode tracking
        self._voice_enabled: dict[str, bool] = {}

        # Lazy singletons
        self._whisper_stt: Any = None
        self._tts_provider: TTSProvider | None = None

        # Dep auto-install tracking (run once per daemon lifecycle)
        self._stt_deps_checked = False
        self._tts_deps_checked = False

        # Active TTS pipelines per conversation (for cancellation)
        self._active_tts_pipelines: dict[str, TTSPipeline] = {}

        # Background preload state for startup warmup
        self._voice_warmup_task: asyncio.Task[None] | None = None
        self._stt_warmup_status = _WARMUP_IDLE
        self._tts_warmup_status = _WARMUP_IDLE
        self._stt_warmup_error = ""
        self._tts_warmup_error = ""

        # Long-lived references to fire-and-forget background tasks so the
        # GC doesn't reap them mid-flight. Tasks remove themselves on done.
        self._background_tasks: set[asyncio.Task[Any]] = set()

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
            import faster_whisper  # noqa: F401

            return True, ""
        except Exception:
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

    def start_voice_warmup(self) -> None:
        """Begin best-effort background warmup for enabled voice models."""
        if self._voice_warmup_task is not None and not self._voice_warmup_task.done():
            return

        voice_config = self._get_voice_config()
        if not voice_config or not voice_config.enabled:
            return

        should_warm = False
        if voice_config.stt_enabled and self._stt_warmup_status != _WARMUP_READY:
            self._stt_warmup_status = _WARMUP_LOADING
            self._stt_warmup_error = ""
            should_warm = True
        if voice_config.tts_enabled and self._tts_warmup_status != _WARMUP_READY:
            self._tts_warmup_status = _WARMUP_LOADING
            self._tts_warmup_error = ""
            should_warm = True

        if not should_warm:
            return

        self._voice_warmup_task = asyncio.create_task(
            self._warm_voice_models(),
            name="voice-model-warmup",
        )
        self._voice_warmup_task.add_done_callback(self._on_voice_warmup_done)

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

    def get_voice_status(self) -> dict[str, Any]:
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

        stt_warmup_status = self._stt_warmup_status if voice_config.stt_enabled else _WARMUP_IDLE
        tts_warmup_status = self._tts_warmup_status if voice_config.tts_enabled else _WARMUP_IDLE

        voice_ready = (
            voice_config.enabled
            and (not voice_config.stt_enabled or stt_warmup_status == _WARMUP_READY)
            and (not voice_config.tts_enabled or tts_warmup_status == _WARMUP_READY)
        )
        voice_loading = (voice_config.stt_enabled and stt_warmup_status == _WARMUP_LOADING) or (
            voice_config.tts_enabled and tts_warmup_status == _WARMUP_LOADING
        )

        result: dict[str, Any] = {
            "enabled": voice_config.enabled,
            "stt_enabled": voice_config.stt_enabled,
            "stt_available": stt_available,
            "stt_reason": stt_reason,
            "whisper_model": voice_config.whisper_model_size,
            "stt_warmup_status": stt_warmup_status,
            "stt_warmup_error": self._stt_warmup_error,
            "tts_enabled": voice_config.tts_enabled,
            "tts_warmup_status": tts_warmup_status,
            "tts_warmup_error": self._tts_warmup_error,
            "voice_ready": voice_ready,
            "voice_loading": voice_loading,
        }

        tts_status = get_tts_status_for_config(voice_config)
        result.update(tts_status.as_status_fields())

        return result

    async def _warm_voice_models(self) -> None:
        """Warm enabled voice models without blocking daemon startup."""
        voice_config = self._get_voice_config()
        if not voice_config or not voice_config.enabled:
            return

        warmups: list[asyncio.Task[None]] = []
        if voice_config.stt_enabled:
            warmups.append(asyncio.create_task(self._warm_stt_model(), name="warm-stt"))
        if voice_config.tts_enabled:
            warmups.append(asyncio.create_task(self._warm_tts_model(), name="warm-tts"))
        if warmups:
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

    async def _warm_stt_model(self) -> None:
        """Warm the Whisper STT model."""
        started_at = time.perf_counter()
        try:
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

    async def _ensure_stt_deps(self, voice_config: VoiceConfig) -> None:
        """Auto-install STT dependencies if missing."""
        try:
            from gobby.voice.dep_check import ensure_stt_deps

            await ensure_stt_deps(voice_config)
        except Exception:
            logger.debug("STT dep check failed", exc_info=True)

    async def _ensure_tts_deps(self, voice_config: VoiceConfig) -> None:
        """Auto-install TTS dependencies if missing."""
        try:
            from gobby.voice.dep_check import ensure_tts_deps

            await ensure_tts_deps(voice_config)
        except Exception:
            logger.debug("TTS dep check failed", exc_info=True)

    def _is_voice_mode(self, conversation_id: str) -> bool:
        """Check if voice mode is active for a conversation."""
        return self._voice_enabled.get(conversation_id, False)

    def _create_tts_pipeline(self, conversation_id: str) -> TTSPipeline | None:
        """Create a TTS pipeline for a conversation if voice mode + TTS are active.

        Cancels any existing pipeline for the conversation first.
        Returns None if TTS is not available or voice mode is off.
        """
        if not self._is_voice_mode(conversation_id):
            return None

        tts = self._get_tts()
        if not tts:
            return None

        # Cancel existing pipeline if any
        existing = self._active_tts_pipelines.pop(conversation_id, None)
        if existing:
            asyncio.create_task(existing.cancel())

        pipeline = TTSPipeline(tts, conversation_id, self.clients)
        self._active_tts_pipelines[conversation_id] = pipeline
        return pipeline

    async def _cancel_tts(self, conversation_id: str) -> None:
        """Cancel active TTS for a conversation. Called on barge-in/interruption."""
        pipeline = self._active_tts_pipelines.pop(conversation_id, None)
        if pipeline:
            await pipeline.cancel()
            logger.debug(f"TTS cancelled for {conversation_id[:8]}")

        await _broadcast_tts_status(self.clients, conversation_id, "idle")

    async def _handle_tts_stop(self, websocket: Any, data: dict[str, Any]) -> None:
        """Handle client-requested TTS stop (barge-in from VAD).

        Message format:
        {
            "type": "tts_stop",
            "conversation_id": "stable-id"
        }
        """
        conversation_id = data.get("conversation_id", "")
        logger.debug(f"TTS stop requested for {conversation_id[:8]}")
        await self._cancel_tts(conversation_id)

    async def _handle_voice_audio(self, websocket: Any, data: dict[str, Any]) -> None:
        """Handle incoming voice audio from client.

        Transcribes audio via Whisper, sends transcription back,
        then forwards as a normal chat_message.

        Message format:
        {
            "type": "voice_audio",
            "conversation_id": "stable-id",
            "audio_data": "<base64-encoded-audio>",
            "mime_type": "audio/webm;codecs=opus",
            "request_id": "client-uuid"
        }
        """
        conversation_id = data.get("conversation_id", "")
        audio_data_b64 = data.get("audio_data", "")
        mime_type = data.get("mime_type", "audio/webm")
        request_id = data.get("request_id", "")

        logger.info(
            f"Voice audio received: {len(audio_data_b64)} chars b64, "
            f"mime={mime_type}, conv={conversation_id[:8]}..."
        )

        # Stop any active TTS when user starts speaking
        await self._cancel_tts(conversation_id)

        if not audio_data_b64:
            await websocket.send(
                json.dumps(
                    {
                        "type": "voice_status",
                        "conversation_id": conversation_id,
                        "status": "error",
                        "error": "No audio data provided",
                    }
                )
            )
            return

        stt = self._get_stt()
        if not stt:
            voice_config = self._get_voice_config()
            if not voice_config or not voice_config.enabled:
                error_msg = "Voice is not enabled. Enable it in Settings > Voice."
            elif not voice_config.stt_enabled:
                error_msg = "Speech-to-text is disabled in config."
            else:
                error_msg = (
                    "Speech-to-text requires the faster-whisper package. "
                    "Install it with: pip install faster-whisper"
                )
            await websocket.send(
                json.dumps(
                    {
                        "type": "voice_status",
                        "conversation_id": conversation_id,
                        "status": "error",
                        "error": error_msg,
                    }
                )
            )
            return

        # Send transcribing status
        await websocket.send(
            json.dumps(
                {
                    "type": "voice_status",
                    "conversation_id": conversation_id,
                    "request_id": request_id,
                    "status": "transcribing",
                }
            )
        )

        try:
            start = time.monotonic()
            audio_bytes = base64.b64decode(audio_data_b64)
            text = await stt.transcribe(audio_bytes, mime_type)
            duration_ms = int((time.monotonic() - start) * 1000)

            if not text.strip():
                logger.info(
                    f"Voice transcription empty for {conversation_id[:8]}... ({duration_ms}ms)"
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "voice_status",
                            "conversation_id": conversation_id,
                            "request_id": request_id,
                            "status": "empty",
                        }
                    )
                )
                return

            # Send transcription result
            await websocket.send(
                json.dumps(
                    {
                        "type": "voice_transcription",
                        "conversation_id": conversation_id,
                        "request_id": request_id,
                        "text": text,
                        "duration_ms": duration_ms,
                    }
                )
            )

            # Auto-submit as chat message through existing pipeline
            chat_data = {
                "type": "chat_message",
                "content": text,
                "conversation_id": conversation_id,
                "request_id": request_id,
            }
            await self._handle_chat_message(websocket, chat_data)

        except Exception as e:
            logger.error(f"Voice transcription error: {e}", exc_info=True)
            try:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "voice_status",
                            "conversation_id": conversation_id,
                            "request_id": request_id,
                            "status": "error",
                            "error": str(e),
                        }
                    )
                )
            except (ConnectionClosed, ConnectionClosedError):
                pass

    async def _handle_voice_mode_toggle(self, websocket: Any, data: dict[str, Any]) -> None:
        """Handle voice mode enable/disable.

        Message format:
        {
            "type": "voice_mode_toggle",
            "conversation_id": "stable-id",
            "enabled": true
        }
        """
        conversation_id = data.get("conversation_id", "")
        enabled = data.get("enabled", False)

        self._voice_enabled[conversation_id] = enabled

        if enabled:
            self.start_voice_warmup()
        else:
            await self._cancel_tts(conversation_id)

        await websocket.send(
            json.dumps(
                {
                    "type": "voice_status",
                    "conversation_id": conversation_id,
                    "status": "voice_mode_on" if enabled else "voice_mode_off",
                    **self.get_voice_status(),
                }
            )
        )

        logger.debug(f"Voice mode {'enabled' if enabled else 'disabled'} for {conversation_id[:8]}")

    async def _handle_voice_prepare(self, websocket: Any, data: dict[str, Any]) -> None:
        """Handle voice_prepare: trigger lazy model warmup on mic-button click.

        Message format:
        {
            "type": "voice_prepare",
            "conversation_id": "stable-id"
        }
        """
        conversation_id = data.get("conversation_id", "")
        if data.get("tts_enabled") is True:
            self._voice_enabled[conversation_id] = True
        self.start_voice_warmup()

        await websocket.send(
            json.dumps(
                {
                    "type": "voice_status",
                    "conversation_id": conversation_id,
                    "status": "preparing",
                    **self.get_voice_status(),
                }
            )
        )
        logger.info("Voice model warmup triggered by client")

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
        except ImportError:
            pass

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

    async def cleanup_voice(self) -> None:
        """Public voice cleanup hook for daemon and WebSocket shutdown paths."""
        await self._cleanup_voice()

    async def _cleanup_voice(self) -> None:
        """Clean up voice state. Called from stop()."""
        # Cancel all active TTS pipelines
        for conv_id in list(self._active_tts_pipelines):
            await self._cancel_tts(conv_id)

        await self.stop_voice_warmup()

        # Cancel any in-flight background tasks (dep installs, etc.)
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

        await self._unload_voice_models()
        self._voice_enabled.clear()
        logger.debug("Voice subsystem cleaned up")

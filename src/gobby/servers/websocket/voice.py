"""WebSocket voice chat handling.

VoiceMixin provides STT + TTS integration for WebSocketServer.
Voice layers on top of the existing chat pipeline — transcribed audio
becomes a normal chat_message, and streamed assistant text feeds TTS.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from websockets.exceptions import ConnectionClosed, ConnectionClosedError

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from gobby.config.voice import VoiceConfig
    from gobby.voice.tts import KokoroTTS


class TTSPipeline:
    """Manages TTS state for a single conversation's response stream.

    Created per-response, feeds text chunks through a sentence buffer,
    synthesizes complete sentences, and streams audio to WebSocket clients.
    """

    def __init__(
        self,
        tts: KokoroTTS,
        conversation_id: str,
        clients: dict[Any, dict[str, Any]],
    ) -> None:
        from gobby.voice.sentence_buffer import SentenceBuffer

        self.tts = tts
        self.conversation_id = conversation_id
        self.clients = clients
        self.sentence_buffer = SentenceBuffer()
        self._chunk_index = 0
        self._synthesis_tasks: list[asyncio.Task[None]] = []

    def feed_text(self, chunk: str) -> None:
        """Feed a text chunk from the LLM stream. Spawns TTS tasks for complete sentences."""
        sentences = self.sentence_buffer.feed(chunk)
        for sentence in sentences:
            task = asyncio.create_task(self._synthesize_and_send(sentence))
            task.add_done_callback(self._on_task_done)
            self._synthesis_tasks.append(task)

    async def flush(self) -> None:
        """Flush remaining buffer at end of stream."""
        remaining = self.sentence_buffer.flush()
        if remaining:
            await self._synthesize_and_send(remaining)

    async def cancel(self) -> None:
        """Cancel all pending synthesis tasks."""
        self.sentence_buffer.clear()
        for task in self._synthesis_tasks:
            if not task.done():
                task.cancel()
        # Wait for cancellations to settle
        if self._synthesis_tasks:
            await asyncio.gather(*self._synthesis_tasks, return_exceptions=True)
        self._synthesis_tasks.clear()

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
        except Exception:
            logger.error("TTS synthesis/send failed", exc_info=True)

    @staticmethod
    def _on_task_done(task: asyncio.Task[None]) -> None:
        """Log unhandled exceptions from TTS tasks (fire-and-forget safety)."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Unhandled exception in TTS task", exc_info=exc)


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
        self._kokoro_tts: KokoroTTS | None = None

        # Dep auto-install tracking (run once per daemon lifecycle)
        self._stt_deps_checked = False
        self._tts_deps_checked = False

        # Active TTS pipelines per conversation (for cancellation)
        self._active_tts_pipelines: dict[str, TTSPipeline] = {}

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
            asyncio.create_task(self._ensure_stt_deps(voice_config))

        from gobby.voice.stt import WhisperSTT

        self._whisper_stt = WhisperSTT(voice_config)
        return self._whisper_stt

    def _get_tts(self) -> KokoroTTS | None:
        """Get or create the TTS singleton (routes by provider config)."""
        if self._kokoro_tts is not None:
            return self._kokoro_tts

        voice_config = self._get_voice_config()
        if not voice_config or not voice_config.enabled or not voice_config.tts_enabled:
            return None

        # Auto-install TTS deps if missing
        if not self._tts_deps_checked:
            self._tts_deps_checked = True
            asyncio.create_task(self._ensure_tts_deps(voice_config))

        provider = getattr(voice_config, "tts_provider", "kokoro")
        if provider == "chatterbox":
            from gobby.voice.tts_chatterbox import ChatterboxTurboProvider

            tts = ChatterboxTurboProvider(voice_config)
        else:
            from gobby.voice.tts import KokoroTTS as _KokoroTTS

            tts = _KokoroTTS(voice_config)

        if not tts.is_available:
            return None

        self._kokoro_tts = tts
        return self._kokoro_tts

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

        # Notify clients that TTS has stopped
        status_msg = json.dumps(
            {
                "type": "tts_status",
                "conversation_id": conversation_id,
                "status": "idle",
            }
        )
        for ws, meta in list(self.clients.items()):
            cid = meta.get("conversation_id") if meta else None
            if cid is not None and cid != conversation_id:
                continue
            try:
                await ws.send(status_msg)
            except (ConnectionClosed, ConnectionClosedError):
                pass

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

        # Cancel TTS when leaving voice mode
        if not enabled:
            await self._cancel_tts(conversation_id)

        await websocket.send(
            json.dumps(
                {
                    "type": "voice_status",
                    "conversation_id": conversation_id,
                    "status": "voice_mode_on" if enabled else "voice_mode_off",
                }
            )
        )

        logger.debug(f"Voice mode {'enabled' if enabled else 'disabled'} for {conversation_id[:8]}")

    async def _cleanup_voice(self) -> None:
        """Clean up voice state. Called from stop()."""
        # Cancel all active TTS pipelines
        for conv_id in list(self._active_tts_pipelines):
            await self._cancel_tts(conv_id)
        self._voice_enabled.clear()
        logger.debug("Voice subsystem cleaned up")

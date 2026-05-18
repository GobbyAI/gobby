"""WebSocket voice message handlers."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import TYPE_CHECKING, Any, TypeGuard

from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from gobby.servers.websocket.voice.tts import (
    TTSPipeline,
    _broadcast_tts_status,
)
from gobby.servers.websocket.voice.warmup import (
    _WARMUP_IDLE,
    VoiceWarmupMixin,
)

if TYPE_CHECKING:
    from gobby.servers.websocket.session_control import SessionControlMixin
    from gobby.servers.websocket.voice_attached import AttachedVoiceServer
    from gobby.voice.tts import TTSProvider

logger = logging.getLogger("gobby.servers.websocket.voice")


def _voice_status_payload(
    conversation_id: str,
    request_id: str,
    status: str,
    *,
    error: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "voice_status",
        "conversation_id": conversation_id,
        "status": status,
    }
    if request_id:
        payload["request_id"] = request_id
    if error:
        payload["error"] = error
    return payload


def _is_attached_voice_server(value: Any) -> TypeGuard[AttachedVoiceServer]:
    return (
        isinstance(getattr(value, "_attached_tts_offsets", None), dict)
        and isinstance(getattr(value, "_active_tts_pipelines", None), dict)
        and hasattr(getattr(value, "_attached_tts_lock", None), "acquire")
        and callable(getattr(value, "_is_voice_mode", None))
        and callable(getattr(value, "_create_tts_pipeline", None))
    )


def _is_session_control_host(value: Any) -> TypeGuard[SessionControlMixin]:
    return hasattr(value, "session_manager") and callable(getattr(value, "_send_error", None))


class VoiceMixin(VoiceWarmupMixin):
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
        self._attached_tts_offsets: dict[str, int] = {}
        self._attached_tts_lock = asyncio.Lock()

        # Background preload state for startup warmup
        self._voice_warmup_task: asyncio.Task[None] | None = None
        self._stt_warmup_status = _WARMUP_IDLE
        self._tts_warmup_status = _WARMUP_IDLE
        self._stt_warmup_error = ""
        self._tts_warmup_error = ""

        # Long-lived references to fire-and-forget background tasks so the
        # GC doesn't reap them mid-flight. Tasks remove themselves on done.
        self._background_tasks: set[asyncio.Task[Any]] = set()

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
        voice_config = self._get_voice_config()
        if voice_config is None:
            return None

        # Cancel existing pipeline if any
        existing = self._active_tts_pipelines.pop(conversation_id, None)
        if existing:
            asyncio.create_task(existing.cancel())

        pipeline = TTSPipeline(
            tts,
            conversation_id,
            self.clients,
            max_chunk_chars=voice_config.tts_clause_max_chars,
        )
        self._active_tts_pipelines[conversation_id] = pipeline
        return pipeline

    async def feed_attached_session_tts(
        self, session_id: str, message: dict[str, Any], *, complete: bool = False
    ) -> None:
        from gobby.servers.websocket.voice_attached import feed_attached_session_tts

        if not _is_attached_voice_server(self):
            raise RuntimeError("Voice host is missing attached-session TTS state")
        await feed_attached_session_tts(
            self,
            session_id,
            message,
            complete=complete,
        )

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
        request_id_raw = data.get("request_id", "")
        request_id = request_id_raw if isinstance(request_id_raw, str) else ""
        project_id = data.get("project_id")
        target_session_raw = data.get("target_session_id")
        target_session_id = (
            target_session_raw.strip()
            if isinstance(target_session_raw, str) and target_session_raw.strip()
            else None
        )
        if target_session_id is None:
            try:
                client_meta = self.clients.get(websocket) or {}
            except TypeError:
                client_meta = {}
            attached = client_meta.get("attached_session_id")
            target_session_id = (
                attached
                if isinstance(attached, str) and attached and attached == conversation_id
                else None
            )

        logger.info(
            f"Voice audio received: {len(audio_data_b64)} chars b64, "
            f"mime={mime_type}, conv={conversation_id[:8]}..."
        )

        # Stop any active TTS when user starts speaking
        await self._cancel_tts(conversation_id)

        if not audio_data_b64:
            await websocket.send(
                json.dumps(
                    _voice_status_payload(
                        conversation_id,
                        request_id,
                        "error",
                        error="No audio data provided",
                    )
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
                    _voice_status_payload(
                        conversation_id,
                        request_id,
                        "error",
                        error=error_msg,
                    )
                )
            )
            return

        # Send transcribing status
        await websocket.send(
            json.dumps(_voice_status_payload(conversation_id, request_id, "transcribing"))
        )

        voice_config = self._get_voice_config()
        timeout_seconds = (
            voice_config.transcription_timeout_seconds if voice_config is not None else 120.0
        )
        try:
            start = time.monotonic()
            audio_bytes = base64.b64decode(audio_data_b64)
            text = await asyncio.wait_for(
                stt.transcribe(audio_bytes, mime_type),
                timeout=timeout_seconds,
            )
            duration_ms = int((time.monotonic() - start) * 1000)

            if not text.strip():
                logger.info(
                    f"Voice transcription empty for {conversation_id[:8]}... ({duration_ms}ms)"
                )
                await websocket.send(
                    json.dumps(_voice_status_payload(conversation_id, request_id, "empty"))
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
            if isinstance(project_id, str) and project_id.strip():
                chat_data["project_id"] = project_id
            if target_session_id:
                # Attached CLI sessions live under SessionControlMixin and have
                # no ChatSession, so bypass ChatMixin's web-chat pipeline.
                from gobby.servers.websocket.handlers.session_observe import (
                    handle_send_to_cli_session,
                )

                if not _is_session_control_host(self):
                    error = "Voice attached-session routing requires a session-control host"
                    logger.error(error)
                    await websocket.send(
                        json.dumps(
                            _voice_status_payload(
                                conversation_id,
                                request_id,
                                "error",
                                error=error,
                            )
                        )
                    )
                    return
                await handle_send_to_cli_session(
                    self,
                    websocket,
                    {
                        "session_id": target_session_id,
                        "content": text,
                        "client_message_id": request_id,
                    },
                )
                return
            await self._handle_chat_message(websocket, chat_data)

        except TimeoutError:
            logger.error(
                "Voice transcription timed out after %.1fs for %s...",
                timeout_seconds,
                conversation_id[:8],
            )
            try:
                await websocket.send(
                    json.dumps(
                        _voice_status_payload(
                            conversation_id,
                            request_id,
                            "error",
                            error="Speech-to-text timed out",
                        )
                    )
                )
            except (ConnectionClosed, ConnectionClosedError):
                pass
        except Exception as e:
            logger.error(f"Voice transcription error: {e}", exc_info=True)
            try:
                await websocket.send(
                    json.dumps(
                        _voice_status_payload(
                            conversation_id,
                            request_id,
                            "error",
                            error=str(e),
                        )
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
            self.start_voice_warmup(want_stt=False, want_tts=True)
        else:
            await self._cancel_tts(conversation_id)

        await websocket.send(
            json.dumps(
                {
                    "type": "voice_status",
                    "conversation_id": conversation_id,
                    "status": "voice_mode_on" if enabled else "voice_mode_off",
                    **self.get_voice_status(want_stt=False, want_tts=enabled),
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
        stt_enabled = data.get("stt_enabled")
        tts_enabled = data.get("tts_enabled")
        want_stt = stt_enabled if isinstance(stt_enabled, bool) else None
        want_tts = tts_enabled if isinstance(tts_enabled, bool) else None
        if want_tts is True:
            self._voice_enabled[conversation_id] = True
        warmup_started = self.start_voice_warmup(want_stt=want_stt, want_tts=want_tts)

        await websocket.send(
            json.dumps(
                {
                    "type": "voice_status",
                    "conversation_id": conversation_id,
                    "status": "preparing",
                    **self.get_voice_status(want_stt=want_stt, want_tts=want_tts),
                }
            )
        )
        if warmup_started:
            logger.info("Voice model warmup triggered by client")
        else:
            logger.debug(
                "Voice prepare did not start warmup for %s (want_stt=%s, want_tts=%s)",
                conversation_id[:8],
                want_stt,
                want_tts,
            )

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

"""WebSocket voice chat handling.

VoiceMixin provides STT integration for WebSocketServer.
Voice layers on top of the existing chat pipeline — transcribed audio
becomes a normal chat_message.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from websockets.exceptions import ConnectionClosed, ConnectionClosedError

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from gobby.config.voice import VoiceConfig


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

        # Lazy singleton
        self._whisper_stt: Any = None

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

        from gobby.voice.stt import WhisperSTT

        self._whisper_stt = WhisperSTT(voice_config)
        return self._whisper_stt

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
        self._voice_enabled.clear()
        logger.debug("Voice subsystem cleaned up")
